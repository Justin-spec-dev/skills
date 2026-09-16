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


class RoutedRunner:
    """Stand-in for subprocess.run that answers by matching the command text.

    Rules are checked in order, so the most specific needle has to come first:
    the device probe contains the bare words that also appear in mutating
    commands, and only "tool:" distinguishes it.
    """

    def __init__(self, rules, default=(1, "", "unrouted")):
        self.rules = rules
        self.default = default
        self.calls = []

    def __call__(self, argv, **_kwargs):
        rendered = [str(part) for part in argv]
        self.calls.append(rendered)
        joined = " ".join(rendered)
        for needle, response in self.rules:
            matched = needle(rendered) if callable(needle) else needle in joined
            if matched:
                if isinstance(response, BaseException):
                    raise response
                if callable(response):
                    returncode, stdout, stderr = response(rendered)
                else:
                    returncode, stdout, stderr = response
                return device_console.subprocess.CompletedProcess(
                    args=rendered,
                    returncode=returncode,
                    stdout=stdout.encode(),
                    stderr=stderr.encode(),
                )
        returncode, stdout, stderr = self.default
        return device_console.subprocess.CompletedProcess(
            args=rendered,
            returncode=returncode,
            stdout=stdout.encode(),
            stderr=stderr.encode(),
        )

    def joined_calls(self):
        return [" ".join(call) for call in self.calls]


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


    # --- deploy: plan/apply boundary ---

    BUILD_HASH = "a" * 64
    DEVICE_HASH = "a" * 64
    ARTIFACT = "/home/dev/app"
    # The device probe answers with one line per tool; "tool:" appears only there.
    PROBE = (
        "tool:scp=0\ntool:cat=1\ntool:sha256sum=1\ntool:md5sum=0\n"
        "tool:mv=1\ntool:chmod=1\ntool:rm=1\ndest_writable=1\nuid=0\n"
    )
    # Commands that must never appear in a dry run. The probe's tool list contains
    # the bare words mv/chmod, so these patterns are deliberately more specific.
    MUTATING = ("mv -f", "cat > ", "chmod 0755", "chmod 0")

    @staticmethod
    def is_local_hash(argv):
        """A bare local hashing call, as opposed to one sent over a transport."""
        return len(argv) == 2 and argv[0].endswith("sha256sum")

    # --- regression guards: honesty about what was and was not established ---

    def test_missing_local_helper_falls_through_to_the_next_rung(self):
        # macOS has no sha256sum and Windows has no file; that must not abort the
        # ladder with a traceback.
        result = device_console.run_local(
            ["definitely-not-a-real-binary-device-console"], timeout=5
        )
        self.assertEqual(127, result["exit_code"])
        self.assertFalse(result["timed_out"])
        self.assertIn("definitely-not-a-real-binary", result["stderr"])

    def test_hash_tool_probe_ignores_banner_lines(self):
        runner = RoutedRunner([("command -v sha256sum", (0, "Welcome to the board\nsha256sum\n", ""))])
        with mock.patch.object(device_console.shutil, "which") as which:
            which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                probe = device_console.remote_hash_tools(
                    device_console.build_parser().parse_args(
                        ["verify", "--host", "h", "--build-path", "a", "--device-path", "b"]
                    ),
                    timeout=5,
                )
        self.assertTrue(probe["ok"])
        self.assertEqual(["sha256sum"], probe["tools"])

    def test_unreachable_device_is_not_reported_as_having_no_hash_tool(self):
        runner = RoutedRunner([("command -v sha256sum", (255, "", "Connection closed by remote host"))])
        args = device_console.build_parser().parse_args(
            ["verify", "--host", "h", "--build-path", "a", "--device-path", "b", "--json"]
        )
        with mock.patch.object(device_console.shutil, "which") as which:
            which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    code = device_console.command_verify(args)
        self.assertEqual(125, code)
        report = emit_json.call_args.args[0]
        self.assertEqual("probe-failed", report["device_digest"]["reason"])
        self.assertIn("could not be asked", report["note"])

    def test_build_side_unknown_digest_is_unverified_not_a_mismatch(self):
        # The device can hash, but the build side cannot produce that algorithm:
        # that is an unknown, not evidence that the transfer was corrupted.
        runner = self.transport()
        # The device can hash with md5, but the build side cannot produce md5 at
        # all: that is an unknown, not evidence that the transfer was corrupted.
        runner.rules.insert(0, ("command -v sha256sum", (0, "md5sum\n", "")))
        runner.rules.insert(1, (f"md5sum {self.ARTIFACT}", (127, "", "md5sum: not found")))
        runner.rules.insert(2, ("md5sum ", (0, f"{self.DEVICE_HASH}  part\n", "")))
        runner.rules.insert(3, ("md5 ", (127, "", "md5: not found")))
        runner.rules.insert(4, ("openssl", (127, "", "openssl: not found")))
        args = device_console.build_parser().parse_args(self.deploy_argv("--apply"))
        with mock.patch.object(device_console, "shutil") as shutil_mock:
            shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    code = device_console.command_deploy(args)
        report = emit_json.call_args.args[0]
        self.assertEqual(125, code)
        self.assertEqual("unverified", report["status"])
        self.assertIsNone(report["artifacts"][0]["verified"])
        self.assertNotIn("mismatch", [step["status"] for step in report["steps"]])

    def test_transfer_timeout_exits_124_and_marks_remote_may_still_run(self):
        timeout = device_console.subprocess.TimeoutExpired(cmd="scp", timeout=1)
        runner = self.transport()
        runner.rules.insert(0, ("cat > ", timeout))
        runner.rules.insert(0, ("/usr/bin/scp", timeout))
        args = device_console.build_parser().parse_args(self.deploy_argv("--apply"))
        with mock.patch.object(device_console, "shutil") as shutil_mock:
            shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    code = device_console.command_deploy(args)
        report = emit_json.call_args.args[0]
        self.assertEqual(124, code)
        attempts = report["artifacts"][0]["transfer"]["attempts"]
        self.assertTrue(any(attempt.get("remote_may_still_run") for attempt in attempts))

    def test_deploy_refuses_a_destination_with_repeated_slashes(self):
        for dest in ("//etc/init.d", "///boot"):
            with self.subTest(dest=dest):
                args = device_console.build_parser().parse_args(self.deploy_argv("--dest", dest))
                with self.assertRaises(device_console.ToolError):
                    device_console.validate_arguments(args)

    def test_unsafe_dest_unlocks_a_module_directory(self):
        args = device_console.build_parser().parse_args(
            self.deploy_argv("--dest", "/lib/modules/5.10", "--unsafe-dest")
        )
        device_console.validate_arguments(args)
        refused = device_console.build_parser().parse_args(
            self.deploy_argv("--dest", "/lib/modules/5.10")
        )
        with self.assertRaisesRegex(device_console.ToolError, "unsafe-dest"):
            device_console.validate_arguments(refused)

    def test_direct_rung_is_skipped_when_an_identity_would_reach_both_hosts(self):
        _, report, runner = self.run_deploy("--apply", "--identity", __file__, scp_ok=True)
        item = report["artifacts"][0]
        self.assertIn("identity", item["direct_rung_skipped"])
        for call in runner.joined_calls():
            if "/usr/bin/scp" in call and "build:" in call and "192.0.2.10:" in call:
                self.fail("one argv cannot serve both hops when an identity is set")

    def test_dmesg_capture_is_redacted_like_every_other_channel(self):
        secret = "sup3rs3cr3ttoken"
        runner = self.transport()
        calls = {"count": 0}

        def dmesg_response(_argv):
            calls["count"] += 1
            if calls["count"] == 1:
                return 0, "[  1.0] baseline\n", ""
            return 0, f"[  1.0] baseline\n[  3.0] leak TOKEN={secret}\n", ""

        runner.rules.insert(0, ("dmesg", dmesg_response))
        args = device_console.build_parser().parse_args(
            self.deploy_argv("--apply", "--run", "/tmp/app --selftest", "--capture-dmesg", "--redact-env", "TOKEN")
        )
        with mock.patch.dict(device_console.os.environ, {"TOKEN": secret}):
            with mock.patch.object(device_console, "shutil") as shutil_mock:
                shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
                with mock.patch.object(device_console.subprocess, "run", runner):
                    with mock.patch.object(device_console, "emit_json") as emit_json:
                        device_console.command_deploy(args)
        delta = emit_json.call_args.args[0]["artifacts"][0]["dmesg"]["delta"]
        self.assertTrue(delta)
        for line in delta:
            self.assertNotIn(secret, line)

    def test_run_command_executes_once_for_a_multi_artifact_deploy(self):
        _, _, runner = self.run_deploy(
            "--apply",
            "--artifact",
            self.ARTIFACT,
            "--artifact",
            self.ARTIFACT + "2",
            "--run",
            "/tmp/app --selftest",
        )
        runs = [call for call in runner.joined_calls() if "--selftest" in call]
        self.assertEqual(1, len(runs), "the plan lists --run once, so it must run once")

    def test_symbolize_skips_extracted_tokens_below_the_kaslr_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "oops.log"
            log.write_text(
                "[121.0] pc : foo+0x00000000/0x1000\n"
                "PC is at bar+0x1c/0x40 [0xffffff8000222222]\n",
                encoding="utf-8",
            )
            runner = RoutedRunner([("addr2line", (0, "bar\n/home/dev/x.c:9\n", ""))])
            args = device_console.build_parser().parse_args(
                ["symbolize", "--build-host", "build", "--binary", self.ARTIFACT,
                 "--addresses-from", str(log), "--kaslr-offset", "0xffffff8000000000", "--json"]
            )
            with mock.patch.object(device_console, "shutil") as shutil_mock:
                shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
                with mock.patch.object(device_console.subprocess, "run", runner):
                    with mock.patch.object(device_console, "emit_json") as emit_json:
                        code = device_console.command_symbolize(args)
        report = emit_json.call_args.args[0]
        self.assertEqual(0, code)
        self.assertEqual(["0x222222"], [frame["address"] for frame in report["frames"]])
        self.assertTrue(report["addresses_skipped"])

    def test_compare_architecture_reports_unknown_for_a_narrower_binary(self):
        match, note = device_console.compare_architecture("Intel 80386", "x86_64")
        self.assertIsNone(match)
        self.assertIn("may run", note)
        match, note = device_console.compare_architecture("ARM", "aarch64")
        self.assertIsNone(match)

    def test_chmod_rejects_a_mode_that_is_not_octal(self):
        args = device_console.build_parser().parse_args(self.deploy_argv("--chmod", "u+x"))
        with self.assertRaisesRegex(device_console.ToolError, "chmod"):
            device_console.validate_arguments(args)

    def transport(self, **overrides):
        """A subprocess.run stand-in routed by matching the command text."""
        build_hash = overrides.get("build_hash", self.BUILD_HASH)
        device_hash = overrides.get("device_hash", self.DEVICE_HASH)
        scp_ok = overrides.get("scp_ok", False)
        scp = (0, "", "") if scp_ok else (1, "", "scp: subsystem request failed")
        rules = [
            ("command -v sha256sum", overrides.get("hash_tools", (0, "sha256sum\n", ""))),
            ("tool:", (0, self.PROBE, "")),
            (f"sha256sum {self.ARTIFACT}", (0, f"{build_hash}  {self.ARTIFACT}\n", "")),
            (f"wc -c {self.ARTIFACT}", (0, f"896 {self.ARTIFACT}\n", "")),
            ("wc -c ", (0, "896 file\n", "")),
            # The staged copy is a local file, so it hashes to the build value:
            # only a corruption on the staging hop could change it. Matched by
            # argv shape because a transfer argv also names the staged path.
            (self.is_local_hash, (0, f"{build_hash}  staged\n", "")),
            ("sha256sum", (0, f"{device_hash}  /tmp/part\n", "")),
            ("dmesg", (0, "[    1.0] baseline\n", "")),
            ("--selftest", (0, "selftest ok\n", "")),
            ("/usr/bin/scp", scp),
            ("mv -f", (0, "", "")),
            ("chmod ", (0, "", "")),
            ("cat > ", (0, "", "")),
            (f"cat {self.ARTIFACT}", (0, "", "")),
        ]
        return RoutedRunner(rules)

    def deploy_argv(self, *extra):
        return [
            "deploy",
            "--host",
            "192.0.2.10",
            "--build-host",
            "build",
            "--artifact",
            self.ARTIFACT,
            "--json",
            *extra,
        ]

    def run_deploy(self, *extra, **overrides):
        runner = self.transport(**overrides)
        args = device_console.build_parser().parse_args(self.deploy_argv(*extra))
        with mock.patch.object(device_console, "shutil") as shutil_mock:
            shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    code = device_console.command_deploy(args)
        report = emit_json.call_args.args[0] if emit_json.call_args else None
        return code, report, runner

    def test_deploy_without_apply_reports_a_plan_and_writes_nothing(self):
        code, report, runner = self.run_deploy("--run", "/tmp/app", "--chmod", "0755")
        self.assertEqual(0, code)
        self.assertEqual("planned", report["status"])
        self.assertFalse(report["applied"])
        for call in runner.joined_calls():
            for pattern in self.MUTATING:
                with self.subTest(call=call, pattern=pattern):
                    self.assertNotIn(pattern, call)

    def test_deploy_plan_lists_the_commands_the_apply_path_runs(self):
        _, plan_report, _ = self.run_deploy("--chmod", "0755")
        planned = " ".join(plan_report["plan"]["commands"])
        self.assertIn("mv -f", planned)
        self.assertIn("chmod 0755", planned)

        _, apply_report, apply_runner = self.run_deploy("--chmod", "0755", "--apply")
        executed = " ".join(apply_runner.joined_calls())
        self.assertEqual("applied", apply_report["status"])
        self.assertIn("mv -f", executed)
        self.assertIn("chmod 0755", executed)

    def test_deploy_refuses_kernel_owned_destinations_before_running_anything(self):
        for dest in ("/sys/class", "/proc/sys", "/dev/shm", "/"):
            with self.subTest(dest=dest):
                runner = self.transport()
                args = device_console.build_parser().parse_args(
                    self.deploy_argv("--dest", dest, "--apply")
                )
                with mock.patch.object(device_console.subprocess, "run", runner):
                    with self.assertRaisesRegex(device_console.ToolError, "dest"):
                        device_console.validate_arguments(args)
                self.assertEqual([], runner.calls, "a refused destination must start no process")

    def test_deploy_requires_unsafe_dest_for_a_boot_path(self):
        args = device_console.build_parser().parse_args(
            self.deploy_argv("--dest", "/boot", "--apply")
        )
        with self.assertRaisesRegex(device_console.ToolError, "--unsafe-dest"):
            device_console.validate_arguments(args)
        allowed = device_console.build_parser().parse_args(
            self.deploy_argv("--dest", "/boot", "--unsafe-dest", "--apply")
        )
        device_console.validate_arguments(allowed)

    def test_verify_running_mismatch_fails_the_command_and_is_reported(self):
        runner = self.transport()
        # Insert after the two probe rules and before the generic sha256sum rule,
        # so only this one path reports a different digest.
        runner.rules.insert(2, ("sha256sum /usr/bin/app", (0, "d" * 64 + "  /usr/bin/app\n", "")))
        args = device_console.build_parser().parse_args(
            self.deploy_argv("--apply", "--verify-running", "/usr/bin/app")
        )
        with mock.patch.object(device_console, "shutil") as shutil_mock:
            shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    code = device_console.command_deploy(args)
        report = emit_json.call_args.args[0]
        self.assertFalse(report["verify_running"]["matches_artifact"])
        self.assertEqual(1, code)

    def test_deploy_refuses_capture_dmesg_without_a_run_command(self):
        # The plan lists a dmesg capture, so promising one without --run would be
        # a plan that the apply path never carries out.
        args = device_console.build_parser().parse_args(self.deploy_argv("--capture-dmesg", "--apply"))
        with self.assertRaisesRegex(device_console.ToolError, "--capture-dmesg"):
            device_console.validate_arguments(args)

    def test_deploy_preserves_a_partial_report_when_the_apply_path_raises(self):
        runner = self.transport()
        args = device_console.build_parser().parse_args(self.deploy_argv("--apply"))
        with mock.patch.object(device_console, "shutil") as shutil_mock:
            shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with mock.patch.object(
                    device_console, "remote_digest", side_effect=RuntimeError("link dropped")
                ):
                    with mock.patch.object(device_console, "emit_json"):
                        with mock.patch.object(device_console, "write_json_report") as write_report:
                            with self.assertRaisesRegex(RuntimeError, "link dropped"):
                                device_console.command_deploy(args)
        report = write_report.call_args.args[1]
        self.assertEqual("interrupted", report["status"])
        self.assertTrue(report["partially_applied"])
        self.assertIn("link dropped", report["error"]["message"])

    def test_deploy_refuses_a_destination_that_is_not_absolute(self):
        args = device_console.build_parser().parse_args(self.deploy_argv("--dest", "tmp/app"))
        with self.assertRaisesRegex(device_console.ToolError, "absolute"):
            device_console.validate_arguments(args)

    # --- deploy: transfer ladder ---

    def test_transfer_prefers_scp_over_the_cat_pipe(self):
        _, report, runner = self.run_deploy("--apply", scp_ok=True)
        transfers = [call for call in runner.joined_calls() if "/usr/bin/scp" in call]
        self.assertTrue(transfers)
        self.assertTrue(report["artifacts"][0]["transfer"]["method"].startswith("scp-direct"))

    def test_transfer_falls_back_to_the_cat_pipe_when_scp_fails(self):
        _, report, runner = self.run_deploy("--apply")
        self.assertEqual("cat-to-device", report["artifacts"][0]["transfer"]["method"])
        calls = runner.joined_calls()
        cat_index = next(i for i, call in enumerate(calls) if "cat > " in call)
        scp_indexes = [i for i, call in enumerate(calls) if "/usr/bin/scp" in call]
        # Every scp rung must be tried, and only then the pipe.
        self.assertTrue(scp_indexes)
        self.assertGreater(cat_index, max(scp_indexes))

    def test_scp_receives_the_port_as_uppercase_p(self):
        _, report, runner = self.run_deploy("--apply")
        scp_calls = [call for call in runner.joined_calls() if "/usr/bin/scp" in call]
        self.assertTrue(scp_calls)
        for call in scp_calls:
            self.assertIn("-P 22", call)
            self.assertNotIn("-p 22", call)

    def test_direct_scp_rung_is_skipped_so_a_jump_never_applies_to_the_build_hop(self):
        _, report, runner = self.run_deploy("--apply", "--device-jump", "jump.example")
        methods = [attempt["method"] for attempt in report["artifacts"][0]["transfer"]["attempts"]]
        self.assertNotIn("scp-direct", methods)
        for call in runner.joined_calls():
            # Only the two-remote-operand form is the direct rung; a staging copy
            # legitimately names the build host and must still be allowed.
            if "/usr/bin/scp" in call and "build:" in call and "192.0.2.10:" in call:
                self.fail("scp with a jump host would apply ProxyJump to the build hop too")

    def test_transfer_refuses_an_artifact_larger_than_max_transfer_bytes(self):
        runner = self.transport()
        args = device_console.build_parser().parse_args(
            self.deploy_argv("--apply", "--max-transfer-bytes", "100")
        )
        with mock.patch.object(device_console, "shutil") as shutil_mock:
            shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with self.assertRaisesRegex(device_console.ToolError, "max-transfer-bytes"):
                    device_console.command_deploy(args)

    # --- deploy: hash verification ---

    def test_deploy_aborts_before_activation_when_the_device_hash_differs(self):
        code, report, runner = self.run_deploy(
            "--apply",
            "--chmod",
            "0755",
            "--run",
            "/tmp/app --selftest",
            device_hash="b" * 64,
        )
        self.assertEqual(1, code)
        self.assertEqual("aborted", report["status"])
        self.assertTrue(report["aborted_before_activation"])
        executed = " ".join(runner.joined_calls())
        for pattern in ("mv -f", "chmod 0755", "--selftest"):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, executed)

    def test_deploy_reports_unverified_and_exits_125_without_a_device_hash_tool(self):
        code, report, runner = self.run_deploy(
            "--apply", "--run", "/tmp/app --selftest", hash_tools=(0, "", "")
        )
        self.assertEqual(125, code)
        self.assertEqual("unverified", report["status"])
        self.assertIsNone(report["artifacts"][0]["verified"])
        self.assertNotIn("--selftest", " ".join(runner.joined_calls()))

    def test_deploy_activates_unverified_only_when_explicitly_allowed(self):
        code, report, runner = self.run_deploy(
            "--apply", "--run", "/tmp/app --selftest", "--allow-unverified", hash_tools=(0, "", "")
        )
        self.assertEqual(0, code)
        self.assertEqual("applied", report["status"])
        self.assertIsNone(report["artifacts"][0]["verified"])
        self.assertIn("--selftest", " ".join(runner.joined_calls()))

    def test_deploy_verifies_the_staged_copy_as_well_as_both_endpoints(self):
        _, report, _ = self.run_deploy("--apply")
        item = report["artifacts"][0]
        self.assertTrue(item["verified"])
        self.assertIn("staged_digest", item)
        self.assertEqual("sha256", item["device_digest"]["algorithm"])

    def test_verify_reports_a_mismatch_with_exit_one(self):
        runner = self.transport(device_hash="c" * 64)
        args = device_console.build_parser().parse_args(
            ["verify", "--host", "192.0.2.10", "--build-host", "build",
             "--build-path", self.ARTIFACT, "--device-path", "/tmp/app", "--json"]
        )
        with mock.patch.object(device_console, "shutil") as shutil_mock:
            shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    code = device_console.command_verify(args)
        self.assertEqual(1, code)
        self.assertFalse(emit_json.call_args.args[0]["verified"])

    def test_verify_reports_unknown_with_exit_125_without_a_device_hash_tool(self):
        runner = self.transport(hash_tools=(0, "", ""))
        args = device_console.build_parser().parse_args(
            ["verify", "--host", "192.0.2.10", "--build-host", "build",
             "--build-path", self.ARTIFACT, "--device-path", "/tmp/app", "--json"]
        )
        with mock.patch.object(device_console, "shutil") as shutil_mock:
            shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    code = device_console.command_verify(args)
        self.assertEqual(125, code)
        report = emit_json.call_args.args[0]
        self.assertFalse(report["verification_known"])
        self.assertIsNone(report["verified"])

    # --- build endpoint ---

    def test_build_arguments_cover_every_field_ssh_base_command_reads(self):
        args = device_console.build_parser().parse_args(
            ["build-run", "--build-host", "build", "--command", "uname -a"]
        )
        endpoint = device_console.build_endpoint_namespace(args)
        self.assertIsNotNone(endpoint)
        for field in ("host", "user", "port", "identity", "known_hosts", "host_key_policy", "connect_timeout"):
            with self.subTest(field=field):
                self.assertTrue(hasattr(endpoint, field))

    def test_build_endpoint_is_absent_when_no_build_host_is_given(self):
        args = device_console.build_parser().parse_args(["inspect", "--artifact", self.ARTIFACT])
        self.assertIsNone(device_console.build_endpoint_namespace(args))

    def test_build_run_refuses_to_run_without_a_build_host(self):
        args = device_console.build_parser().parse_args(["build-run", "--command", "uname -a"])
        with self.assertRaisesRegex(device_console.ToolError, "build-host"):
            device_console.build_view_namespace(args)

    def test_build_run_reports_its_own_transport_label(self):
        runner = self.transport()
        args = device_console.build_parser().parse_args(
            ["build-run", "--build-host", "build", "--command", "uname -a", "--json"]
        )
        with mock.patch.object(device_console, "shutil") as shutil_mock:
            shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    device_console.command_build_run(args)
        self.assertEqual("ssh-build", emit_json.call_args.args[0]["transport"])

    def test_device_jump_is_threaded_into_device_argv_as_proxyjump(self):
        args = device_console.build_parser().parse_args(
            ["ssh-run", "--host", "192.0.2.10", "--device-jump", "jump.example",
             "--command", "uname -a"]
        )
        base, _ = device_console.ssh_base_command(args, batch_mode=True)
        self.assertIn("ProxyJump=jump.example", base)

    def test_device_jump_rejects_characters_that_would_inject_ssh_config(self):
        for value in ("-oProxyCommand=evil", "jump\nProxyCommand=evil", "jump host", "jump:0"):
            with self.subTest(value=value):
                with self.assertRaises(device_console.ToolError):
                    device_console.validate_jump_spec(value)

    def test_build_port_range_error_names_the_build_flag(self):
        args = device_console.build_parser().parse_args(
            ["build-run", "--build-host", "build", "--build-port", "70000", "--command", "true"]
        )
        with self.assertRaisesRegex(device_console.ToolError, "--build-port"):
            device_console.validate_arguments(args)

    # --- inspect and symbolize ---

    def inspect_rules(self):
        return [
            ("readelf -h", (0, "  Machine:  AArch64\n  Data:  2's complement, little endian\n  Type:  DYN\n", "")),
            ("readelf -l", (0, "      [Requesting program interpreter: /lib/ld.so.1]\n", "")),
            ("readelf -d", (0, " (NEEDED)  Shared library: [libc.so.6]\n", "")),
            ("readelf -n", (0, "    Build ID: deadbeef\n", "")),
            ("file ", (0, "ELF 64-bit LSB pie executable, ARM aarch64, not stripped\n", "")),
            ("size ", (0, "   text    data     bss\n   1200     256       8\n", "")),
            ("uname", (0, "armv7l\n5.10.0\n", "")),
        ]

    def test_inspect_flags_an_architecture_mismatch_against_the_device(self):
        runner = RoutedRunner(self.inspect_rules())
        args = device_console.build_parser().parse_args(
            ["inspect", "--build-host", "build", "--artifact", self.ARTIFACT,
             "--host", "192.0.2.10", "--json"]
        )
        with mock.patch.object(device_console, "shutil") as shutil_mock:
            shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    code = device_console.command_inspect(args)
        report = emit_json.call_args.args[0]
        self.assertFalse(report["arch_match"])
        self.assertEqual(1, code)

    def test_inspect_reports_an_unknown_architecture_rather_than_guessing(self):
        rules = self.inspect_rules()
        rules[0] = ("readelf -h", (0, "  Machine:  Some New Machine\n", ""))
        runner = RoutedRunner(rules)
        args = device_console.build_parser().parse_args(
            ["inspect", "--build-host", "build", "--artifact", self.ARTIFACT,
             "--host", "192.0.2.10", "--json"]
        )
        with mock.patch.object(device_console, "shutil") as shutil_mock:
            shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    device_console.command_inspect(args)
        self.assertIsNone(emit_json.call_args.args[0]["arch_match"])

    def test_symbolize_applies_the_kaslr_offset_to_every_address(self):
        runner = RoutedRunner([("addr2line", (0, "main\n/home/dev/main.c:42\n", ""))])
        args = device_console.build_parser().parse_args(
            ["symbolize", "--build-host", "build", "--binary", self.ARTIFACT,
             "--address", "0xffffff8000123456", "--kaslr-offset", "0xffffff8000000000", "--json"]
        )
        with mock.patch.object(device_console, "shutil") as shutil_mock:
            shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
            with mock.patch.object(device_console.subprocess, "run", runner):
                with mock.patch.object(device_console, "emit_json") as emit_json:
                    device_console.command_symbolize(args)
        report = emit_json.call_args.args[0]
        self.assertEqual("0x123456", report["frames"][0]["address"])
        self.assertTrue(report["kaslr_offset_applied"])
        self.assertIn("0x123456", report["command"])

    def test_symbolize_extracts_addresses_from_a_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "oops.log"
            log.write_text(
                "Unable to handle kernel paging request at 0xffffff8000123456\n"
                "PC is at do_thing+0x1c/0x40 [0xffffff8000222222]\n",
                encoding="utf-8",
            )
            runner = RoutedRunner([("addr2line", (0, "do_thing\n/home/dev/x.c:9\n", ""))])
            args = device_console.build_parser().parse_args(
                ["symbolize", "--build-host", "build", "--binary", self.ARTIFACT,
                 "--addresses-from", str(log), "--json"]
            )
            with mock.patch.object(device_console, "shutil") as shutil_mock:
                shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
                with mock.patch.object(device_console.subprocess, "run", runner):
                    with mock.patch.object(device_console, "emit_json") as emit_json:
                        device_console.command_symbolize(args)
        frames = emit_json.call_args.args[0]["frames"]
        self.assertEqual(["0xffffff8000123456", "0xffffff8000222222"], [f["address"] for f in frames])

    def test_compare_architecture_handles_readelf_and_uname_spellings(self):
        cases = (
            # readelf writes "X86-64" where uname writes "x86_64".
            ("Advanced Micro Devices X86-64", "x86_64", True),
            ("AArch64", "aarch64", True),
            ("ARM", "armv7l", True),
            ("Intel 80386", "i686", True),
            ("RISC-V", "riscv64", True),
            # A 64-bit ARM binary cannot run on a 32-bit armv8 userspace.
            ("AArch64", "armv8l", False),
            ("Advanced Micro Devices X86-64", "aarch64", False),
            ("Some New Machine", "aarch64", None),
        )
        for machine, uname_m, expected in cases:
            with self.subTest(machine=machine, uname=uname_m):
                self.assertEqual(
                    expected, device_console.compare_architecture(machine, uname_m)[0]
                )

    def test_deploy_uses_the_local_artifact_directly_when_there_is_no_build_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "app"
            artifact.write_bytes(b"ARTIFACT")
            runner = self.transport()
            args = device_console.build_parser().parse_args(
                ["deploy", "--host", "192.0.2.10", "--artifact", str(artifact), "--apply", "--json"]
            )
            with mock.patch.object(device_console, "shutil") as shutil_mock:
                shutil_mock.which.side_effect = lambda name: f"/usr/bin/{name}"
                with mock.patch.object(device_console.subprocess, "run", runner):
                    with mock.patch.object(device_console, "emit_json"):
                        code = device_console.command_deploy(args)
        self.assertEqual(0, code)
        executed = runner.joined_calls()
        # The local artifact is pushed as-is: no staging copy, so there is no hop
        # that could fail and leave an empty file to upload.
        for call in executed:
            self.assertNotIn("device-console-deploy-", call)
        self.assertTrue(any("/usr/bin/scp" in call and str(artifact) in call for call in executed))

    def test_symbolize_rejects_an_address_that_is_not_hexadecimal(self):
        with self.assertRaisesRegex(device_console.ToolError, "hexadecimal"):
            device_console.normalize_address("do_thing+0x1c", 0)

    def test_symbolize_needs_at_least_one_address(self):
        args = device_console.build_parser().parse_args(
            ["symbolize", "--binary", self.ARTIFACT]
        )
        with self.assertRaisesRegex(device_console.ToolError, "address"):
            device_console.command_symbolize(args)


if __name__ == "__main__":
    unittest.main()
