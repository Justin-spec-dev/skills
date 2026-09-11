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
        # raw mode keeps this focused on prompt pinning; exit markers are
        # covered separately by the posix-shell tests.
        args = device_console.build_parser().parse_args(
            [
                "serial-run",
                "--port",
                "COM9",
                "--no-login",
                "--wake",
                "0",
                "--mode",
                "raw",
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
        marker = "__DEVICE_CONSOLE_RC_fixed__:"
        fake = FakeSerial(
            [
                b"device login:",
                b"Password:",
                b"s3cr3t\r\nroot# ",
                b"uname -a\r\nLinux test\r\n" + f"{marker}0".encode() + b"\r\nroot# ",
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
                with mock.patch.object(device_console, "wire_command", return_value=("wrapped", marker)):
                    with mock.patch.object(device_console, "emit_json") as emit_json:
                        result = device_console.command_serial_run(args)
        self.assertEqual(result, 0)
        report = emit_json.call_args.args[0]
        self.assertNotIn("s3cr3t", device_console.json.dumps(report))
        self.assertIn("<REDACTED>", report["session"])
        self.assertIn(b"s3cr3t\n", fake.writes)
        self.assertTrue(report["results"][0]["exit_code_known"])

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
                device_console, "write_json_report", side_effect=OSError("disk full")
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

    # --- redaction guards ---

    def test_redact_env_missing_variable_is_reported(self):
        args = device_console.build_parser().parse_args(
            [
                "ssh-run",
                "--host",
                "192.0.2.2",
                "--command",
                "uname -a",
                "--redact-env",
                "MISSING_DEVICE_TOKEN",
            ]
        )
        with mock.patch.dict(device_console.os.environ, {}, clear=True):
            with mock.patch.object(device_console.subprocess, "run") as run:
                with self.assertRaisesRegex(device_console.ToolError, "not set or empty"):
                    device_console.command_ssh_run(args)
        run.assert_not_called()

    def test_redact_env_short_secret_is_rejected(self):
        args = device_console.build_parser().parse_args(
            [
                "ssh-run",
                "--host",
                "192.0.2.2",
                "--command",
                "uname -a",
                "--redact-env",
                "SHORT_TOKEN",
            ]
        )
        with mock.patch.dict(device_console.os.environ, {"SHORT_TOKEN": "8"}):
            with mock.patch.object(device_console.subprocess, "run") as run:
                with self.assertRaisesRegex(device_console.ToolError, "shorter than"):
                    device_console.command_ssh_run(args)
        run.assert_not_called()

    def test_ssh_run_rejects_multiline_secret_before_connecting(self):
        args = device_console.build_parser().parse_args(
            [
                "ssh-run",
                "--host",
                "192.0.2.2",
                "--command",
                "uname -a",
                "--redact-env",
                "MULTILINE_SECRET",
            ]
        )
        with mock.patch.dict(device_console.os.environ, {"MULTILINE_SECRET": "first\nsecond"}):
            with mock.patch.object(device_console.subprocess, "run") as run:
                with self.assertRaisesRegex(device_console.ToolError, "single-line"):
                    device_console.command_ssh_run(args)
        run.assert_not_called()

    def test_redact_treats_secret_as_literal_text(self):
        # A secret containing regex metacharacters must be replaced literally.
        text = device_console.redact("value=a.b(c) end", ["a.b(c)"])
        self.assertEqual(text, "value=<REDACTED> end")

    # --- exit-code correctness ---

    def test_serial_run_reports_unknown_exit_code_when_marker_is_lost(self):
        fake = FakeSerial([b"root# ", b"\r\nstill running...\r\nroot# "])
        fake.write = lambda _value: None
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
                "--mode",
                "posix-shell",
                "--command",
                "uname -a",
                "--json",
            ]
        )
        with mock.patch.object(device_console, "open_serial", return_value=fake):
            with mock.patch.object(device_console, "emit_json") as emit_json:
                result = device_console.command_serial_run(args)
        # A lost marker must never read as success.
        self.assertEqual(result, 125)
        item = emit_json.call_args.args[0]["results"][0]
        self.assertFalse(item["exit_code_known"])
        self.assertIsNone(item["exit_code"])
        self.assertIn("exit marker not observed", item["exit_code_note"])

    def test_serial_run_resolves_exit_code_from_marker(self):
        marker = "__DEVICE_CONSOLE_RC_fixed__:"
        # Prompt first, then output carrying the wrapper's exit marker.
        fake = FakeSerial([b"root# ", b"\r\nLinux test\r\n" + f"{marker}7".encode() + b"\r\nroot# "])
        fake.write = lambda _value: None
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
                "--mode",
                "posix-shell",
                "--command",
                "false",
                "--json",
            ]
        )
        with mock.patch.object(device_console, "open_serial", return_value=fake):
            with mock.patch.object(device_console, "wire_command", return_value=("wrapped", marker)):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    result = device_console.command_serial_run(args)
        self.assertEqual(result, 1)
        item = emit_json.call_args.args[0]["results"][0]
        self.assertTrue(item["exit_code_known"])
        self.assertEqual(item["exit_code"], 7)

    def test_serial_run_raw_mode_does_not_claim_unknown_exit(self):
        fake = FakeSerial([b"root# ", b"\r\nLinux test\r\nroot# "])
        fake.write = lambda _value: None
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
                "--mode",
                "raw",
                "--command",
                "uname -a",
                "--json",
            ]
        )
        with mock.patch.object(device_console, "open_serial", return_value=fake):
            with mock.patch.object(device_console, "emit_json") as emit_json:
                result = device_console.command_serial_run(args)
        self.assertEqual(result, 0)
        item = emit_json.call_args.args[0]["results"][0]
        self.assertFalse(item["exit_code_known"])
        self.assertNotIn("exit_code_note", item)

    def test_serial_run_defaults_to_posix_shell_for_verifiable_exit_codes(self):
        args = device_console.build_parser().parse_args(
            ["serial-run", "--port", "COM9", "--command", "uname -a"]
        )
        self.assertEqual(args.mode, "posix-shell")
        wire, marker = device_console.wire_command("uname -a", args.mode)
        self.assertIsNotNone(marker)
        self.assertIn("sh -c", wire)

    # --- login budget ---

    def test_serial_run_reports_login_budget_in_timeout_error(self):
        fake = FakeSerial([])
        fake.write = lambda _value: None
        fake.flush = lambda: None
        fake.close = lambda: None
        args = device_console.build_parser().parse_args(
            [
                "serial-run",
                "--port",
                "COM9",
                "--no-login",
                "--wake",
                "1",
                "--command",
                "uname -a",
                "--login-budget",
                "1",
                "--login-timeout",
                "30",
            ]
        )
        clock = {"now": 0.0}

        def ticking_clock() -> float:
            clock["now"] += 30.0
            return clock["now"]

        with mock.patch.object(device_console, "open_serial", return_value=fake):
            # Each clock reading jumps 30s, so the first attempt exhausts the
            # 1s budget (read_until_any consumes one reading for its deadline).
            with mock.patch.object(device_console.time, "monotonic", ticking_clock):
                with self.assertRaisesRegex(device_console.ToolError, "login budget of 1s"):
                    device_console.command_serial_run(args)

    def test_serial_run_clips_each_login_read_to_remaining_budget(self):
        # Alternating login/password prompts keep the loop alive without
        # tripping the repeated-prompt guards; each read must be clipped to
        # what is left of the budget instead of a fresh login-timeout. Real
        # time is used so the budget actually decreases.
        fake = FakeSerial([])
        fake.write = lambda _value: None
        fake.flush = lambda: None
        fake.close = lambda: None
        args = device_console.build_parser().parse_args(
            [
                "serial-run",
                "--port",
                "COM9",
                "--wake",
                "0",
                "--username",
                "root",
                "--password-env",
                "DEVICE_PASSWORD",
                "--command",
                "uname -a",
                "--login-budget",
                "0.25",
                "--login-timeout",
                "0.12",
            ]
        )
        timeouts = []
        clock = {"now": 0.0}
        sequence = {0: ("login", "device login:"), 1: ("password", "Password:")}

        def fake_read_until_any(_patterns, timeout):
            timeouts.append(timeout)
            clock["now"] += 0.1
            return sequence[(len(timeouts) - 1) % 2]

        with mock.patch.dict(device_console.os.environ, {"DEVICE_PASSWORD": "s3cr3t"}):
            with mock.patch.object(device_console, "open_serial", return_value=fake):
                with mock.patch.object(device_console, "SerialTextReader") as reader_cls:
                    reader_cls.return_value.read_until_any.side_effect = fake_read_until_any
                    reader_cls.return_value.pending = ""
                    with mock.patch.object(
                        device_console.time, "monotonic", side_effect=lambda: clock["now"]
                    ):
                        # A third login prompt trips the repeated-prompt guard, which is
                        # the correct safety behaviour; what matters here is the clipping.
                        with self.assertRaises(device_console.ToolError):
                            device_console.command_serial_run(args)
        self.assertEqual(len(timeouts), 3)
        self.assertEqual(timeouts[:2], [0.12, 0.12])
        # The last attempt is clipped to the ~0.05s left, not a fresh 0.12s.
        self.assertAlmostEqual(timeouts[2], 0.05, places=6)

    # --- timeout transparency ---

    def test_ssh_timeout_marks_remote_command_as_possibly_running(self):
        args = device_console.build_parser().parse_args(
            ["ssh-run", "--host", "192.0.2.2", "--command", "dmesg -w", "--json"]
        )
        timeout = device_console.subprocess.TimeoutExpired(cmd="ssh", timeout=30, output=b"", stderr=b"")
        with mock.patch.object(device_console.shutil, "which", return_value="ssh"):
            with mock.patch.object(device_console.subprocess, "run", side_effect=timeout):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    result = device_console.command_ssh_run(args)
        self.assertEqual(result, 124)
        self.assertTrue(emit_json.call_args.args[0]["results"][0]["remote_may_still_run"])

    # --- CLI symmetry (P5) ---

    def test_serial_monitor_emits_json_report(self):
        fake = FakeSerial([b"boot: ok\n"])
        fake.close = lambda: None
        args = device_console.build_parser().parse_args(
            [
                "serial-monitor",
                "--port",
                "COM9",
                "--duration",
                "1",
                "--no-timestamps",
                "--json",
            ]
        )
        with mock.patch.object(device_console, "open_serial", return_value=fake):
            with mock.patch.object(device_console, "emit_json") as emit_json:
                result = device_console.command_serial_monitor(args)
        self.assertEqual(result, 0)
        report = emit_json.call_args.args[0]
        self.assertEqual(report["transport"], "serial-monitor")
        self.assertEqual(report["baud"], 115200)
        self.assertFalse(report["interrupted"])
        lines = [record["line"] for record in report["lines"]]
        self.assertIn("boot: ok", lines)
        self.assertTrue(any(line.startswith("# opened COM9") for line in lines))

    def test_ssh_run_supports_append(self):
        args = device_console.build_parser().parse_args(
            [
                "ssh-run",
                "--host",
                "192.0.2.2",
                "--command",
                "uname -a",
                "--output",
                "report.jsonl",
                "--append",
            ]
        )
        completed = device_console.subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout=b"Linux\n", stderr=b""
        )
        with mock.patch.object(device_console.shutil, "which", return_value="ssh"):
            with mock.patch.object(device_console.subprocess, "run", return_value=completed):
                with mock.patch.object(device_console, "write_json_report") as write_json_report:
                    result = device_console.command_ssh_run(args)
        self.assertEqual(result, 0)
        self.assertTrue(write_json_report.call_args.kwargs["append"])

    def test_append_and_json_are_mutually_exclusive(self):
        args = device_console.build_parser().parse_args(
            [
                "serial-run",
                "--port",
                "COM9",
                "--command",
                "uname -a",
                "--json",
                "--append",
            ]
        )
        with self.assertRaisesRegex(device_console.ToolError, "mutually exclusive"):
            device_console.validate_arguments(args)

    def test_ssh_port_error_names_the_subcommand(self):
        args = device_console.build_parser().parse_args(
            ["ssh-run", "--host", "192.0.2.2", "--port", "70000", "--command", "uname -a"]
        )
        with self.assertRaisesRegex(device_console.ToolError, "SSH --port must be between 1 and 65535"):
            device_console.validate_arguments(args)

    # --- doctor prerequisite reporting (P10) ---

    def test_doctor_reports_missing_prerequisites_with_next_step(self):
        args = device_console.build_parser().parse_args(["doctor", "--json"])
        with mock.patch.object(device_console.shutil, "which", return_value=None):
            with mock.patch.object(device_console.importlib.util, "find_spec", return_value=None):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    result = device_console.command_doctor(args)
        self.assertEqual(result, 2)
        report = emit_json.call_args.args[0]
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["missing"]), 2)
        self.assertIn("platform-setup.md", report["next_step"])

    def test_doctor_ok_when_prerequisites_present(self):
        args = device_console.build_parser().parse_args(["doctor", "--json"])
        with mock.patch.object(device_console.shutil, "which", return_value="/usr/bin/ssh"):
            with mock.patch.object(device_console.importlib.util, "find_spec", return_value=object()):
                with mock.patch.object(device_console, "load_pyserial") as load_pyserial:
                    load_pyserial.return_value = (
                        type("S", (), {"VERSION": "3.5"})(),
                        type("L", (), {"comports": staticmethod(list)})(),
                    )
                    with mock.patch.object(device_console, "emit_json") as emit_json:
                        result = device_console.command_doctor(args)
        self.assertEqual(result, 0)
        report = emit_json.call_args.args[0]
        self.assertTrue(report["ok"])
        self.assertEqual(report["missing"], [])

    def test_doctor_text_output_lists_missing_prerequisites(self):
        args = device_console.build_parser().parse_args(["doctor"])
        with mock.patch.object(device_console.shutil, "which", return_value=None):
            with mock.patch.object(device_console.importlib.util, "find_spec", return_value=None):
                with mock.patch("builtins.print") as print_output:
                    result = device_console.command_doctor(args)
        self.assertEqual(result, 2)
        rendered = "\n".join(str(call.args[0]) for call in print_output.call_args_list)
        self.assertIn("Missing prerequisites:", rendered)
        self.assertIn("platform-setup.md", rendered)

    # --- serial line configuration (previously untested) ---

    def test_configure_serial_maps_every_cli_option(self):
        args = device_console.build_parser().parse_args(
            [
                "serial-run",
                "--port",
                "/dev/ttyUSB0",
                "--baud",
                "57600",
                "--bytesize",
                "7",
                "--parity",
                "E",
                "--stopbits",
                "2",
                "--xonxoff",
                "--rtscts",
                "--dsrdtr",
                "--dtr",
                "on",
                "--rts",
                "on",
                "--timeout",
                "4",
                "--command",
                "uname -a",
            ]
        )

        class Sink:
            pass

        sink = Sink()
        device_console.configure_serial(sink, args, read_timeout=0.25)
        self.assertEqual(sink.port, "/dev/ttyUSB0")
        self.assertEqual(sink.baudrate, 57600)
        self.assertEqual(sink.bytesize, 7)
        self.assertEqual(sink.parity, "E")
        self.assertEqual(sink.stopbits, 2)
        self.assertEqual(sink.timeout, 0.25)
        self.assertEqual(sink.write_timeout, 4)
        self.assertTrue(sink.xonxoff)
        self.assertTrue(sink.rtscts)
        self.assertTrue(sink.dsrdtr)
        self.assertTrue(sink.dtr)
        self.assertTrue(sink.rts)

    def test_configure_serial_defaults_dtr_and_rts_off(self):
        args = device_console.build_parser().parse_args(
            ["serial-run", "--port", "COM9", "--command", "uname -a"]
        )

        class Sink:
            pass

        sink = Sink()
        device_console.configure_serial(sink, args, read_timeout=0.2)
        # Asserting DTR/RTS on open can reset or halt the target board.
        self.assertFalse(sink.dtr)
        self.assertFalse(sink.rts)
        self.assertFalse(sink.xonxoff)
        self.assertFalse(sink.rtscts)
        self.assertEqual(sink.parity, "N")
        self.assertEqual(sink.bytesize, 8)
        self.assertEqual(sink.stopbits, 1)

    def test_configure_serial_clamps_write_timeout(self):
        class Sink:
            pass

        low = device_console.build_parser().parse_args(
            ["serial-run", "--port", "COM9", "--timeout", "0.01", "--command", "uname -a"]
        )
        sink = Sink()
        device_console.configure_serial(sink, low, read_timeout=0.2)
        self.assertEqual(sink.write_timeout, 1.0)

        high = device_console.build_parser().parse_args(
            ["serial-run", "--port", "COM9", "--timeout", "600", "--command", "uname -a"]
        )
        device_console.configure_serial(sink, high, read_timeout=0.2)
        self.assertEqual(sink.write_timeout, 10.0)

    # --- reader scaling and bounds ---

    def test_reader_rescans_only_the_last_line_for_anchored_patterns(self):
        positions = []

        class RecordingPattern:
            """Delegates to a real compiled pattern and records the scan start."""

            def __init__(self, compiled):
                self.compiled = compiled

            @property
            def pattern(self):
                return self.compiled.pattern

            def search(self, text, pos=0, endpos=None):
                positions.append(pos)
                return self.compiled.search(text, pos)

        lines = [b"boot line\r\n"] * 4000
        reader = device_console.SerialTextReader(FakeSerial(lines + [b"root@board:~# "]), "utf-8")
        anchored = RecordingPattern(device_console.re.compile(device_console.DEFAULT_PROMPT))
        name, _ = reader.read_until_any([("prompt", anchored)], 5.0)
        self.assertEqual(name, "prompt")
        # Without the floor the cursor stays at 0 and every chunk rescans the
        # whole buffer, which is the quadratic behaviour this guards against.
        self.assertTrue(positions)
        self.assertGreater(max(positions), 0)

    def test_reader_bounds_memory_on_flood_output(self):
        # Realistic line-based flood: a prompt does arrive, so the reader stops.
        chunk = b"log line without prompt\n" * 170
        chunks = [chunk] * 60 + [b"root@board:~# "]
        reader = device_console.SerialTextReader(FakeSerial(chunks), "utf-8")
        with mock.patch.object(device_console, "MAX_PENDING_CHARS", 1 << 16):
            name, _ = reader.read_until_any(
                [("prompt", device_console.re.compile(device_console.DEFAULT_PROMPT))], 5.0
            )
        self.assertEqual(name, "prompt")
        self.assertGreater(reader.dropped_tail_bytes, 0)
        self.assertLess(reader.dropped_tail_bytes, 60 * 4096)

    def test_dropped_tail_bytes_is_reported(self):
        chunk = b"log line without prompt\n" * 170
        fake = FakeSerial([b"root# ", *([chunk] * 60)])
        fake.write = lambda _value: None
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
                "--mode",
                "raw",
                "--command",
                "dmesg",
                "--max-output",
                "4096",
                "--json",
            ]
        )
        # Buffer cap above one chunk so truncation repeats without rescanning a
        # minimal buffer on every read.
        with mock.patch.object(device_console, "open_serial", return_value=fake):
            with mock.patch.object(device_console, "MAX_PENDING_CHARS", 1 << 12):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    device_console.command_serial_run(args)
        report = emit_json.call_args.args[0]
        self.assertGreater(report["dropped_tail_bytes"], 0)
        item = report["results"][0]
        self.assertTrue(item["output_truncated"])
        # A spent capture budget is not a timeout.
        self.assertFalse(item["timed_out"])
        self.assertEqual(len(item["output"]), 4096)

    # --- output caps ---

    def test_ssh_run_marks_truncated_output(self):
        args = device_console.build_parser().parse_args(
            [
                "ssh-run",
                "--host",
                "192.0.2.2",
                "--command",
                "cat /var/log/messages",
                "--max-output",
                "10",
                "--json",
            ]
        )
        completed = device_console.subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout=b"x" * 400, stderr=b""
        )
        with mock.patch.object(device_console.shutil, "which", return_value="ssh"):
            with mock.patch.object(device_console.subprocess, "run", return_value=completed):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    result = device_console.command_ssh_run(args)
        item = emit_json.call_args.args[0]["results"][0]
        self.assertTrue(item["output_truncated"])
        self.assertEqual(len(item["stdout"]), 10)
        self.assertEqual(result, 0)

    def test_serial_monitor_stops_at_max_output(self):
        fake = FakeSerial([b"line one\n", b"line two\n", b"line three\n", b"line four\n"])
        fake.close = lambda: None
        args = device_console.build_parser().parse_args(
            [
                "serial-monitor",
                "--port",
                "COM9",
                "--no-timestamps",
                "--max-output",
                "12",
                "--json",
            ]
        )
        with mock.patch.object(device_console, "open_serial", return_value=fake):
            with mock.patch.object(device_console, "emit_json") as emit_json:
                device_console.command_serial_monitor(args)
        report = emit_json.call_args.args[0]
        self.assertTrue(report["output_truncated"])

    def test_default_max_output_is_positive(self):
        for subcommand in ("ssh-run", "serial-run", "serial-monitor"):
            argv = [subcommand]
            if subcommand == "ssh-run":
                argv += ["--host", "192.0.2.2", "--command", "uname -a"]
            else:
                argv += ["--port", "COM9"]
                if subcommand == "serial-run":
                    argv += ["--command", "uname -a"]
            with self.subTest(subcommand=subcommand):
                args = device_console.build_parser().parse_args(argv)
                self.assertGreater(args.max_output, 0)


if __name__ == "__main__":
    unittest.main()
