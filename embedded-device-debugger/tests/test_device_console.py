import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "device_console.py"
SPEC = importlib.util.spec_from_file_location("device_console", SCRIPT)
assert SPEC and SPEC.loader
device_console = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(device_console)


class FakeSerial:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, _size):
        return self.chunks.pop(0) if self.chunks else b""


class DeviceConsoleTests(unittest.TestCase):
    def test_redact_longest_secret_first(self):
        text = device_console.redact("token=abcdef and abc", ["abc", "abcdef"])
        self.assertEqual(text, "token=<REDACTED> and <REDACTED>")

    def test_validate_ssh_endpoint_rejects_user_at_host(self):
        with self.assertRaises(device_console.ToolError):
            device_console.validate_ssh_endpoint("root@example.com", None)

    def test_ssh_command_uses_strict_host_key_and_batch_mode(self):
        args = argparse.Namespace(
            host="192.0.2.2",
            user="root",
            port=22,
            identity=None,
            known_hosts=None,
            host_key_policy="strict",
            connect_timeout=7,
        )
        with mock.patch.object(device_console.shutil, "which", return_value="ssh"):
            command, destination = device_console.ssh_base_command(args, batch_mode=True)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("BatchMode=yes", command)
        self.assertEqual(destination, "root@192.0.2.2")

    def test_serial_reader_preserves_data_after_first_match(self):
        fake = FakeSerial([b"login:rest$ "])
        reader = device_console.SerialTextReader(fake, "utf-8")
        name, first = reader.read_until_any([("login", device_console.re.compile(r"login:"))], 0.1)
        self.assertEqual((name, first), ("login", "login:"))
        name, second = reader.read_until_any([("prompt", device_console.re.compile(r"\$ "))], 0.1)
        self.assertEqual((name, second), ("prompt", "rest$ "))

    def test_default_prompt_rejects_xml_like_output(self):
        prompt = device_console.re.compile(device_console.DEFAULT_PROMPT)
        self.assertIsNone(prompt.search("<status>"))
        self.assertIsNone(prompt.search("status>\r\nstill producing output"))
        for value in ("root@board:~# ", "/ # ", "router(config)#", "=> "):
            with self.subTest(value=value):
                self.assertIsNotNone(prompt.search(value))

    def test_serial_run_pins_the_observed_default_prompt(self):
        fake = FakeSerial(
            [
                b"root@board:~# ",
                b"status>",
                b"\r\nhealthy\r\nroot@board:~# ",
            ]
        )
        fake.writes = []
        fake.write = lambda value: fake.writes.append(value)
        fake.flush = lambda: None
        fake.close = lambda: None
        args = device_console.build_parser().parse_args(
            [
                "serial-run",
                "--port",
                "COM9",
                "--no-login",
                "--wake",
                "0",
                "--command",
                "show health",
                "--json",
            ]
        )
        with mock.patch.object(device_console, "open_serial", return_value=fake):
            with mock.patch.object(device_console, "emit_json") as emit_json:
                result = device_console.command_serial_run(args)
        self.assertEqual(result, 0)
        report = emit_json.call_args.args[0]
        self.assertFalse(report["results"][0]["timed_out"])
        self.assertIn("status>\r\nhealthy", report["results"][0]["output"])

    def test_posix_shell_wire_command_has_unique_exit_marker(self):
        wire, marker = device_console.wire_command("uname -a", "posix-shell")
        self.assertIn("sh -c", wire)
        self.assertIsNotNone(marker)
        self.assertIn(marker, wire)

    def test_output_requires_explicit_overwrite(self):
        path = Path(__file__).with_name("already-exists.json")
        with mock.patch.object(Path, "exists", return_value=True):
            with self.assertRaises(device_console.ToolError):
                device_console.prepare_output(str(path), force=False, append=False)

    def test_existing_output_blocks_ssh_before_process_starts(self):
        args = device_console.build_parser().parse_args(
            [
                "ssh-run",
                "--host",
                "192.0.2.2",
                "--command",
                "uname -a",
                "--output",
                "already-exists.json",
            ]
        )
        with mock.patch.object(Path, "exists", return_value=True):
            with mock.patch.object(device_console.subprocess, "run") as run:
                with self.assertRaises(device_console.ToolError):
                    device_console.command_ssh_run(args)
        run.assert_not_called()

    def test_serial_login_redacts_password_from_report(self):
        fake = FakeSerial(
            [
                b"device login:",
                b"Password:",
                b"s3cr3t\r\nroot# ",
                b"uname -a\r\nLinux test\r\nroot# ",
            ]
        )
        fake.writes = []
        fake.write = lambda value: fake.writes.append(value)
        fake.flush = lambda: None
        fake.close = lambda: None
        args = device_console.build_parser().parse_args(
            [
                "serial-run",
                "--port",
                "COM9",
                "--username",
                "root",
                "--password-env",
                "DEVICE_PASSWORD",
                "--command",
                "uname -a",
                "--json",
            ]
        )
        with mock.patch.dict(device_console.os.environ, {"DEVICE_PASSWORD": "s3cr3t"}):
            with mock.patch.object(device_console, "open_serial", return_value=fake):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    result = device_console.command_serial_run(args)
        self.assertEqual(result, 0)
        report = emit_json.call_args.args[0]
        self.assertNotIn("s3cr3t", device_console.json.dumps(report))
        self.assertIn("<REDACTED>", report["session"])
        self.assertIn(b"s3cr3t\n", fake.writes)

    def test_serial_monitor_redacts_environment_secrets(self):
        fake = FakeSerial([b"token=s3cr3t\n"])
        fake.close = lambda: None
        args = device_console.build_parser().parse_args(
            [
                "serial-monitor",
                "--port",
                "COM9",
                "--duration",
                "1",
                "--no-timestamps",
                "--redact-env",
                "DEVICE_TOKEN",
            ]
        )
        with mock.patch.dict(device_console.os.environ, {"DEVICE_TOKEN": "s3cr3t"}):
            with mock.patch.object(device_console, "open_serial", return_value=fake):
                with mock.patch.object(device_console.time, "monotonic", side_effect=[0, 0, 0, 2]):
                    with mock.patch("builtins.print") as print_output:
                        result = device_console.command_serial_monitor(args)
        self.assertEqual(result, 0)
        rendered = "\n".join(str(call.args[0]) for call in print_output.call_args_list)
        self.assertNotIn("s3cr3t", rendered)
        self.assertIn("token=<REDACTED>", rendered)

    def test_serial_monitor_rejects_multiline_secrets_before_connecting(self):
        args = device_console.build_parser().parse_args(
            [
                "serial-monitor",
                "--port",
                "COM9",
                "--redact-env",
                "MULTILINE_SECRET",
            ]
        )
        with mock.patch.dict(device_console.os.environ, {"MULTILINE_SECRET": "first\nsecond"}):
            with mock.patch.object(device_console, "open_serial", side_effect=RuntimeError("opened")) as open_serial:
                with self.assertRaisesRegex(device_console.ToolError, "single-line"):
                    device_console.command_serial_monitor(args)
        open_serial.assert_not_called()

    def test_serial_run_writes_partial_report_on_error(self):
        fake = FakeSerial([b"root# "])
        fake.write = mock.Mock(side_effect=OSError("serial write failed"))
        fake.flush = lambda: None
        fake.close = lambda: None
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            args = device_console.build_parser().parse_args(
                [
                    "serial-run",
                    "--port",
                    "COM9",
                    "--no-login",
                    "--wake",
                    "0",
                    "--command",
                    "uname -a",
                    "--output",
                    str(output),
                ]
            )
            with mock.patch.object(device_console, "open_serial", return_value=fake):
                with mock.patch("builtins.print"):
                    with self.assertRaisesRegex(OSError, "serial write failed"):
                        device_console.command_serial_run(args)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["session"], "root# ")
        self.assertEqual(report["results"], [])
        self.assertEqual(report["error"]["type"], "OSError")
        self.assertEqual(report["error"]["message"], "serial write failed")

    def test_serial_run_preserves_inflight_output_on_read_error(self):
        fake = FakeSerial([])
        fake.read = mock.Mock(
            side_effect=[b"root# ", b"token=s3cr3t", OSError("serial disconnected")]
        )
        fake.write = lambda _value: None
        fake.flush = lambda: None
        fake.close = lambda: None
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            args = device_console.build_parser().parse_args(
                [
                    "serial-run",
                    "--port",
                    "COM9",
                    "--no-login",
                    "--wake",
                    "0",
                    "--command",
                    "dmesg",
                    "--redact-env",
                    "DEVICE_TOKEN",
                    "--output",
                    str(output),
                ]
            )
            with mock.patch.dict(device_console.os.environ, {"DEVICE_TOKEN": "s3cr3t"}):
                with mock.patch.object(device_console, "open_serial", return_value=fake):
                    with mock.patch("builtins.print"):
                        with self.assertRaisesRegex(OSError, "serial disconnected"):
                            device_console.command_serial_run(args)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["error"]["partial_output"], "token=<REDACTED>")

    def test_partial_report_failure_does_not_mask_serial_error(self):
        fake = FakeSerial([b"root# "])
        fake.write = mock.Mock(side_effect=OSError("serial write failed"))
        fake.flush = lambda: None
        fake.close = lambda: None
        args = device_console.build_parser().parse_args(
            [
                "serial-run",
                "--port",
                "COM9",
                "--no-login",
                "--wake",
                "0",
                "--command",
                "uname -a",
            ]
        )
        with mock.patch.object(device_console, "open_serial", return_value=fake):
            with mock.patch.object(
                device_console, "write_json_artifact", side_effect=OSError("disk full")
            ):
                with mock.patch("builtins.print"):
                    with self.assertRaisesRegex(OSError, "serial write failed"):
                        device_console.command_serial_run(args)

    def test_ssh_timeout_is_reported_and_redacted(self):
        args = device_console.build_parser().parse_args(
            [
                "ssh-run",
                "--host",
                "192.0.2.2",
                "--command",
                "collect logs",
                "--redact-env",
                "DEVICE_TOKEN",
                "--json",
            ]
        )
        timeout = device_console.subprocess.TimeoutExpired(
            cmd="ssh", timeout=30, output=b"partial s3cr3t", stderr=b"timed out"
        )
        with mock.patch.dict(device_console.os.environ, {"DEVICE_TOKEN": "s3cr3t"}):
            with mock.patch.object(device_console.shutil, "which", return_value="ssh"):
                with mock.patch.object(device_console.subprocess, "run", side_effect=timeout):
                    with mock.patch.object(device_console, "emit_json") as emit_json:
                        result = device_console.command_ssh_run(args)
        self.assertEqual(result, 124)
        report = emit_json.call_args.args[0]
        self.assertTrue(report["results"][0]["timed_out"])
        self.assertEqual(report["results"][0]["stdout"], "partial <REDACTED>")

    def test_negative_timeout_is_rejected(self):
        args = argparse.Namespace(timeout=-1)
        with self.assertRaises(device_console.ToolError):
            device_console.validate_arguments(args)


if __name__ == "__main__":
    unittest.main()
