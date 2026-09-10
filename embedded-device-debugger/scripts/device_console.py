#!/usr/bin/env python3
"""Cross-platform SSH and serial helper for embedded-device diagnostics.

Exit codes (interpret them; do not assume 0 just because output exists):

    0    every command ran and every observed exit code was 0
    1    a command ran but reported a non-zero exit code
    2    usage error, invalid arguments, or a refused safety guard
         (e.g. an unset/over-short ``--redact-env``, or an output file that
         already exists without ``--force``)
    124  a command timed out; an SSH timeout also sets
         ``remote_may_still_run: true`` on that result
    125  ``serial-run --mode posix-shell`` finished a command without observing
         the wrapper's exit marker, so the result is unverified. This is not
         success; the result carries ``exit_code_known: false``
    130  the user interrupted the run (``serial-monitor`` / ``serial-run``)

``doctor`` exits 0 when all prerequisites are present and 2 when one is
missing; in both cases the report lists ``missing`` and ``next_step``.
``ssh-shell`` is interactive and passes the session's exit status straight
through, so it has no fixed contract.
"""

from __future__ import annotations

import argparse
import codecs
import getpass
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, TextIO


class ToolError(RuntimeError):
    """An expected, user-actionable tool error."""


DEFAULT_PROMPT = (
    r"(?m)^(?:(?:\[[^\r\n]{1,72}\]|[A-Za-z0-9_.@:/~()\\-]{0,72}) ?[#$>]|=>) ?\Z"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def decode_bytes(value: Any, encoding: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return bytes(value).decode(encoding, errors="replace")


MIN_SECRET_LENGTH = 4


def redact(text: str, secret_values: Iterable[Optional[str]]) -> str:
    """Replace secret values with a placeholder.

    Secrets are applied longest first so a secret contained in a longer one is
    fully covered. Patterns are escaped, so a secret is matched literally.
    """
    result = text
    for secret in sorted({s for s in secret_values if s}, key=len, reverse=True):
        result = re.sub(re.escape(secret), "<REDACTED>", result)
    return result


def secret_values_from_env(names: Sequence[str]) -> list[str]:
    """Collect secret values from environment variables.

    Raises when a requested variable is unset or empty instead of silently
    producing an unredacted report, and rejects values too short to redact
    without destroying unrelated output.
    """
    values: list[str] = []
    for name in names:
        value = os.environ.get(name)
        if not value:
            raise ToolError(
                f"--redact-env {name}: environment variable is not set or empty. "
                "Fix the variable name or unset it and retry."
            )
        if len(value) < MIN_SECRET_LENGTH:
            raise ToolError(
                f"--redact-env {name}: secret value is shorter than {MIN_SECRET_LENGTH} characters; "
                "redacting it would corrupt unrelated output. Use a longer secret or remove this option."
            )
        values.append(value)
    return values


def validate_single_line_secrets(secrets: Iterable[str]) -> None:
    """Reject secrets containing CR or LF.

    Output is emitted and stored line by line, so a multi-line secret cannot be
    redacted reliably and its newlines would also reshape the transcript.
    """
    if any("\r" in secret or "\n" in secret for secret in secrets):
        raise ToolError("--redact-env values must be single-line secrets (no CR or LF).")


def emit_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def ensure_output_available(path_value: Optional[str], force: bool, append: bool) -> None:
    if not path_value:
        return
    path = Path(path_value).expanduser()
    if path.exists() and not (force or append):
        raise ToolError(f"Output exists: {path}. Use --force or --append explicitly.")


def prepare_output(path_value: Optional[str], force: bool, append: bool) -> Optional[TextIO]:
    if not path_value:
        return None
    ensure_output_available(path_value, force=force, append=append)
    path = Path(path_value).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a" if append else "w", encoding="utf-8", newline="\n")


def write_json_report(
    path_value: Optional[str], value: Any, force: bool, append: bool = False
) -> None:
    """Write a JSON report, one complete document per line when appending.

    Appending keeps the file valid JSON Lines: each record is serialized in full
    before anything is written, so an interrupted run cannot produce a partial
    line. Serializing before touching the filesystem also means a mid-write
    failure cannot leave a truncated report that a caller might mistake for
    complete evidence.
    """
    if not path_value:
        return
    path = Path(path_value).expanduser()
    ensure_output_available(path_value, force=force, append=append)
    payload = json.dumps(value, ensure_ascii=False, indent=None) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)


def load_pyserial():
    try:
        import serial  # type: ignore
        from serial.tools import list_ports  # type: ignore
    except ImportError as exc:
        raise ToolError(
            "PySerial is required for serial commands. Install requirements.txt in an approved Python environment."
        ) from exc
    return serial, list_ports


def validate_ssh_endpoint(host: str, user: Optional[str]) -> None:
    if not host or host.startswith("-") or any(ch.isspace() for ch in host) or "@" in host:
        raise ToolError("Invalid SSH host. Pass only a hostname or address, without user@ or options.")
    if user and not re.fullmatch(r"[A-Za-z0-9_.-]+", user):
        raise ToolError("Invalid SSH user name.")


def ssh_base_command(args: argparse.Namespace, batch_mode: bool) -> tuple[list[str], str]:
    validate_ssh_endpoint(args.host, args.user)
    ssh_path = shutil.which("ssh")
    if not ssh_path:
        raise ToolError("OpenSSH client 'ssh' was not found on PATH. Run doctor for setup details.")

    strict_value = "yes" if args.host_key_policy == "strict" else "accept-new"
    command = [
        ssh_path,
        "-p",
        str(args.port),
        "-o",
        f"ConnectTimeout={args.connect_timeout}",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        f"StrictHostKeyChecking={strict_value}",
    ]
    if batch_mode:
        command.extend(["-o", "BatchMode=yes"])
    if args.known_hosts:
        command.extend(["-o", f"UserKnownHostsFile={str(Path(args.known_hosts).expanduser())}"])
    if args.identity:
        identity = Path(args.identity).expanduser()
        if not identity.is_file():
            raise ToolError(f"SSH identity file does not exist: {identity}")
        command.extend(["-i", str(identity)])
    destination = f"{args.user}@{args.host}" if args.user else args.host
    return command, destination


def command_doctor(args: argparse.Namespace) -> int:
    ssh_path = shutil.which("ssh")
    serial_spec = importlib.util.find_spec("serial")
    missing: list[str] = []
    if not ssh_path:
        missing.append("OpenSSH client 'ssh' (required for SSH transports)")
    if serial_spec is None:
        missing.append("PySerial (required for serial transports)")
    result: dict[str, Any] = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "ssh": {"available": bool(ssh_path), "path": ssh_path},
        "pyserial": {"available": serial_spec is not None},
        "ok": not missing,
        "missing": missing,
        "next_step": (
            "Read references/platform-setup.md for install steps; install nothing without user approval."
            if missing
            else "All prerequisites detected."
        ),
    }
    if serial_spec is not None:
        try:
            serial, list_ports = load_pyserial()
            result["pyserial"]["version"] = getattr(serial, "VERSION", "unknown")
            result["serial_ports"] = [port.device for port in list_ports.comports()]
        except Exception as exc:
            result["serial_ports_error"] = str(exc)

    if args.json:
        emit_json(result)
    else:
        print(f"Platform: {result['platform']}")
        print(f"Python: {result['python']} ({result['python_executable']})")
        print(f"OpenSSH: {ssh_path or 'not found'}")
        print(f"PySerial: {result['pyserial'].get('version', 'not installed')}")
        if "serial_ports" in result:
            ports = result["serial_ports"]
            print(f"Serial ports: {', '.join(ports) if ports else 'none detected'}")
        if missing:
            print("Missing prerequisites:")
            for item in missing:
                print(f"  - {item}")
            print("Next: read references/platform-setup.md, then install only with user approval.")
    return 0 if not missing else 2


def command_ssh_run(args: argparse.Namespace) -> int:
    ensure_output_available(args.output, force=args.force, append=args.append)
    base, destination = ssh_base_command(args, batch_mode=True)
    secrets = secret_values_from_env(args.redact_env)
    validate_single_line_secrets(secrets)
    results: list[dict[str, Any]] = []

    for remote_command in args.command:
        if "\x00" in remote_command:
            raise ToolError("SSH command contains a NUL character.")
        started_at = utc_now()
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [*base, destination, remote_command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=args.timeout,
                check=False,
            )
            item = {
                "command": remote_command,
                "started_at": started_at,
                "duration_seconds": round(time.monotonic() - started, 3),
                "exit_code": completed.returncode,
                "timed_out": False,
                "stdout": redact(decode_bytes(completed.stdout, args.encoding), secrets),
                "stderr": redact(decode_bytes(completed.stderr, args.encoding), secrets),
            }
        except subprocess.TimeoutExpired as exc:
            # The local ssh client was killed, but the remote command may well
            # still be running; the timeout is not proof that it stopped.
            item = {
                "command": remote_command,
                "started_at": started_at,
                "duration_seconds": round(time.monotonic() - started, 3),
                "exit_code": None,
                "timed_out": True,
                "remote_may_still_run": True,
                "stdout": redact(decode_bytes(exc.stdout, args.encoding), secrets),
                "stderr": redact(decode_bytes(exc.stderr, args.encoding), secrets),
            }
        results.append(item)
        if item["timed_out"] and not args.continue_on_error:
            break
        if item["exit_code"] not in (0, None) and not args.continue_on_error:
            break

    report = {
        "tool": "device-console",
        "transport": "ssh",
        "target": destination,
        "port": args.port,
        "captured_at": utc_now(),
        "results": results,
    }
    write_json_report(args.output, report, force=args.force, append=args.append)

    if args.json:
        emit_json(report)
    else:
        for item in results:
            print(f"--- $ {item['command']}")
            if item["stdout"]:
                print(item["stdout"], end="" if item["stdout"].endswith("\n") else "\n")
            if item["stderr"]:
                print("[stderr]", file=sys.stderr)
                print(item["stderr"], file=sys.stderr, end="" if item["stderr"].endswith("\n") else "\n")
            status = "timeout" if item["timed_out"] else f"exit={item['exit_code']}"
            print(f"--- {status}; {item['duration_seconds']}s")

    if any(item["timed_out"] for item in results):
        return 124
    return 0 if all(item["exit_code"] == 0 for item in results) else 1


def command_ssh_shell(args: argparse.Namespace) -> int:
    base, destination = ssh_base_command(args, batch_mode=False)
    command = [*base, "-tt", destination]
    if args.command:
        command.append(args.command)
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        return 130


def serial_port_records() -> list[dict[str, Any]]:
    _, list_ports = load_pyserial()
    records = []
    for port in list_ports.comports():
        records.append(
            {
                "device": port.device,
                "description": port.description,
                "hwid": port.hwid,
                "vid": port.vid,
                "pid": port.pid,
                "serial_number": port.serial_number,
                "manufacturer": port.manufacturer,
                "product": port.product,
                "location": port.location,
            }
        )
    return records


def command_serial_ports(args: argparse.Namespace) -> int:
    records = serial_port_records()
    if args.json:
        emit_json(records)
    elif not records:
        print("No serial ports detected.")
    else:
        for record in records:
            label = record["description"] or "unknown device"
            details = []
            if record["vid"] is not None and record["pid"] is not None:
                details.append(f"VID:PID={record['vid']:04X}:{record['pid']:04X}")
            if record["serial_number"]:
                details.append(f"serial={record['serial_number']}")
            suffix = f" ({', '.join(details)})" if details else ""
            print(f"{record['device']}: {label}{suffix}")
    return 0


def open_serial(args: argparse.Namespace, read_timeout: float = 0.2):
    serial, _ = load_pyserial()
    connection = serial.Serial()
    connection.port = args.port
    connection.baudrate = args.baud
    connection.bytesize = args.bytesize
    connection.parity = args.parity
    connection.stopbits = args.stopbits
    connection.timeout = read_timeout
    connection.write_timeout = min(max(args.timeout, 1.0), 10.0)
    connection.xonxoff = args.xonxoff
    connection.rtscts = args.rtscts
    connection.dsrdtr = args.dsrdtr
    connection.dtr = args.dtr == "on"
    connection.rts = args.rts == "on"
    try:
        connection.open()
    except Exception:
        connection.close()
        raise
    return connection


def timestamped_line(line: str, timestamps: bool) -> str:
    return f"[{utc_now()}] {line}" if timestamps else line


def command_serial_monitor(args: argparse.Namespace) -> int:
    secrets = secret_values_from_env(args.redact_env)
    validate_single_line_secrets(secrets)
    output = prepare_output(args.output, force=args.force, append=args.append)
    connection = None
    decoder = codecs.getincrementaldecoder(args.encoding)(errors="replace")
    pending = ""
    started = time.monotonic()
    started_at = utc_now()
    last_data = started
    records: list[dict[str, Any]] = []
    interrupted = False

    def emit(line: str) -> None:
        rendered = timestamped_line(redact(line, secrets), not args.no_timestamps)
        if args.json:
            records.append({"timestamp": utc_now(), "line": rendered})
        else:
            print(rendered, flush=True)
        if output:
            output.write(rendered + "\n")
            output.flush()

    def build_report() -> dict[str, Any]:
        return {
            "tool": "device-console",
            "transport": "serial-monitor",
            "target": args.port,
            "baud": args.baud,
            "started_at": started_at,
            "captured_at": utc_now(),
            "duration_seconds": round(time.monotonic() - started, 3),
            "interrupted": interrupted,
            "lines": records,
        }

    try:
        connection = open_serial(args)
        emit(f"# opened {args.port} at {args.baud} baud")
        while True:
            now = time.monotonic()
            if args.duration > 0 and now - started >= args.duration:
                break
            if args.idle_timeout > 0 and now - last_data >= args.idle_timeout:
                emit(f"# idle timeout after {args.idle_timeout:g}s")
                break
            waiting = getattr(connection, "in_waiting", 0)
            data = connection.read(max(1, min(waiting or 1, 65536)))
            if not data:
                continue
            last_data = time.monotonic()
            pending += decoder.decode(data)
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                emit(line.rstrip("\r"))
    except KeyboardInterrupt:
        interrupted = True
        emit("# interrupted")
    finally:
        # Flush the trailing partial line before the report is emitted so the
        # JSON never omits evidence that was already captured.
        if pending:
            emit(pending.rstrip("\r"))
        if connection is not None:
            connection.close()
        if output:
            output.close()

    if args.json:
        emit_json(build_report())
    return 130 if interrupted else 0


class SerialTextReader:
    def __init__(self, connection: Any, encoding: str):
        self.connection = connection
        self.decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self.pending = ""

    def read_until_any(
        self, patterns: Sequence[tuple[str, re.Pattern[str]]], timeout: float
    ) -> tuple[Optional[str], str]:
        buffer = self.pending
        self.pending = ""
        deadline = time.monotonic() + timeout
        try:
            while True:
                matches: list[tuple[int, int, str]] = []
                for name, pattern in patterns:
                    match = pattern.search(buffer)
                    if match:
                        matches.append((match.start(), match.end(), name))
                if matches:
                    _, end, name = min(matches, key=lambda item: (item[0], item[1]))
                    consumed = buffer[:end]
                    self.pending = buffer[end:]
                    return name, consumed
                if time.monotonic() >= deadline:
                    return None, buffer
                waiting = getattr(self.connection, "in_waiting", 0)
                data = self.connection.read(max(1, min(waiting or 1, 65536)))
                if data:
                    buffer += self.decoder.decode(data)
        except BaseException:
            self.pending = buffer
            raise


def newline_bytes(name: str) -> bytes:
    return {"lf": b"\n", "cr": b"\r", "crlf": b"\r\n"}[name]


def wire_command(command: str, mode: str) -> tuple[str, Optional[str]]:
    if "\x00" in command or "\r" in command or "\n" in command:
        raise ToolError("Serial commands must be a single line without NUL, CR, or LF characters.")
    if mode == "raw":
        return command, None
    marker = f"__DEVICE_CONSOLE_RC_{uuid.uuid4().hex[:12]}__:"
    quoted = shlex.quote(command)
    wire = f"sh -c {quoted}; __dc_rc=$?; printf '\\n{marker}%s\\n' \"$__dc_rc\""
    return wire, marker


def compile_pattern(label: str, value: str) -> re.Pattern[str]:
    try:
        return re.compile(value)
    except re.error as exc:
        raise ToolError(f"Invalid {label} regex: {exc}") from exc


def resolve_serial_exit_code(item: dict[str, Any], marker: Optional[str]) -> None:
    """Fill in exit_code / exit_code_known for one serial-run result.

    In posix-shell mode the wrapper always prints an exit marker. If it never
    arrives the command outcome is unknown, and the report must say so instead
    of silently looking like success.
    """
    known = False
    if marker:
        matches = re.findall(re.escape(marker) + r"(-?\d+)", item["output"])
        if matches:
            item["exit_code"] = int(matches[-1])
            known = True
    if not known and not item["timed_out"] and marker:
        item["exit_code_note"] = "exit marker not observed; command result unknown"
    item["exit_code_known"] = known


def command_serial_run(args: argparse.Namespace) -> int:
    ensure_output_available(args.output, force=args.force, append=False)
    prompt = compile_pattern("prompt", args.prompt)
    login_prompt = compile_pattern("login-prompt", args.login_prompt)
    password_prompt = compile_pattern("password-prompt", args.password_prompt)
    password: Optional[str] = None
    if args.password_env:
        if args.password_env not in os.environ:
            raise ToolError(f"Environment variable is not set: {args.password_env}")
        password = os.environ[args.password_env]
    elif args.ask_password:
        password = getpass.getpass("Serial password: ")
    secrets = secret_values_from_env(args.redact_env)
    if password:
        secrets.append(password)

    newline = newline_bytes(args.newline)
    session_chunks: list[str] = []
    results: list[dict[str, Any]] = []
    logged_in = False
    username_sent = False
    password_sent = False
    active_prompt = prompt
    connection = None
    reader: Optional[SerialTextReader] = None
    failure: Optional[BaseException] = None

    try:
        connection = open_serial(args)
        reader = SerialTextReader(connection, args.encoding)
        if args.wake > 0:
            connection.write(newline * args.wake)
            connection.flush()
        if args.initial_delay:
            time.sleep(args.initial_delay)

        login_patterns = [("prompt", prompt)]
        if not args.no_login:
            login_patterns.extend([("login", login_prompt), ("password", password_prompt)])

        # Bound the whole login exchange, not just each read: a silent device
        # must not hold the tool for attempts x login-timeout seconds.
        budget = args.login_budget
        saw_output = False
        while True:
            if budget <= 0:
                break
            attempt_timeout = args.login_timeout if budget is None else min(args.login_timeout, budget)
            attempt_started = time.monotonic()
            matched, chunk = reader.read_until_any(login_patterns, attempt_timeout)
            if budget is not None:
                budget -= time.monotonic() - attempt_started
            session_chunks.append(chunk)
            if chunk:
                saw_output = True
            if matched == "prompt":
                logged_in = True
                if args.prompt == DEFAULT_PROMPT:
                    observed_matches = list(prompt.finditer(chunk))
                    if observed_matches:
                        observed = observed_matches[-1].group(0)
                        active_prompt = re.compile(r"(?m)^" + re.escape(observed) + r"\Z")
                break
            if matched == "login":
                if not args.username:
                    raise ToolError("Serial console requested a username; provide --username.")
                if username_sent:
                    raise ToolError("Serial login prompt repeated after the username was sent.")
                connection.write(args.username.encode(args.encoding) + newline)
                connection.flush()
                username_sent = True
                continue
            if matched == "password":
                if password is None:
                    raise ToolError("Serial console requested a password; provide it through --password-env.")
                if password_sent:
                    raise ToolError("Serial password prompt repeated after the password was sent.")
                connection.write(password.encode(args.encoding) + newline)
                connection.flush()
                password_sent = True
                continue
            # No login or prompt pattern matched within the remaining budget.
            # Say what was actually observed rather than guessing at a cause.
            raise ToolError(
                f"No serial prompt matched within the login budget of {args.login_budget:g}s "
                f"({budget:.1f}s remaining); console output seen: {'yes' if saw_output else 'no'}. "
                "Check baud/login settings or provide a narrower --prompt regex."
            )
        if not logged_in:
            raise ToolError(
                f"Could not reach a serial shell prompt within --login-budget={args.login_budget:g}s."
            )

        for command in args.command:
            wire, marker = wire_command(command, args.mode)
            started_at = utc_now()
            started = time.monotonic()
            connection.write(wire.encode(args.encoding) + newline)
            connection.flush()
            matched, output = reader.read_until_any([("prompt", active_prompt)], args.timeout)
            item = {
                "command": command,
                "started_at": started_at,
                "duration_seconds": round(time.monotonic() - started, 3),
                "exit_code": None,
                "timed_out": matched is None,
                "output": redact(output, secrets),
            }
            resolve_serial_exit_code(item, marker)
            results.append(item)
            if item["timed_out"] and not args.continue_on_error:
                break
            if item["exit_code"] not in (0, None) and not args.continue_on_error:
                break
    except (Exception, KeyboardInterrupt) as exc:
        failure = exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:
                if failure is None:
                    failure = exc

    report = {
        "tool": "device-console",
        "transport": "serial",
        "target": args.port,
        "baud": args.baud,
        "mode": args.mode,
        "captured_at": utc_now(),
        "session": redact("".join(session_chunks), secrets),
        "results": results,
    }
    if failure is not None:
        error = {
            "type": type(failure).__name__,
            "message": redact(str(failure), secrets),
        }
        if reader is not None and reader.pending:
            error["partial_output"] = redact(reader.pending, secrets)
        report["error"] = error

        try:
            write_json_report(args.output, report, force=args.force, append=args.append)
            if args.json:
                emit_json(report)
        except Exception as evidence_error:
            try:
                print(
                    f"device-console: failed to preserve partial report: {evidence_error}",
                    file=sys.stderr,
                )
            except Exception:
                pass
        if isinstance(failure, KeyboardInterrupt):
            return 130
        raise failure.with_traceback(failure.__traceback__)

    write_json_report(args.output, report, force=args.force, append=args.append)

    if args.json:
        emit_json(report)
    else:
        session = report["session"]
        if session:
            print("--- session")
            print(session, end="" if session.endswith("\n") else "\n")
        for item in results:
            print(f"--- $ {item['command']}")
            if item["output"]:
                print(item["output"], end="" if item["output"].endswith("\n") else "\n")
            if item["timed_out"]:
                status = "timeout"
            elif item["exit_code"] is not None:
                status = f"exit={item['exit_code']}"
            elif item["exit_code_known"] is False and args.mode == "posix-shell":
                status = "exit UNKNOWN (marker lost)"
            else:
                status = "prompt received"
            print(f"--- {status}; {item['duration_seconds']}s")

    if any(item["timed_out"] for item in results):
        return 124
    known_codes = [item["exit_code"] for item in results if item["exit_code"] is not None]
    # posix-shell wraps every command so it can report a status; a missing status
    # means the result is unverified and must not be reported as success.
    if args.mode == "posix-shell" and any(item["exit_code_known"] is False for item in results):
        return 125
    return 0 if all(code == 0 for code in known_codes) else 1


def add_ssh_connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True, help="Hostname or IP address, without user@")
    parser.add_argument("--user", help="SSH user")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--identity", help="Private-key path")
    parser.add_argument("--known-hosts", help="Alternate known_hosts file")
    parser.add_argument("--host-key-policy", choices=("strict", "accept-new"), default="strict")
    parser.add_argument("--connect-timeout", type=int, default=10)


def add_serial_connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", required=True, help="COM port or /dev/tty* path")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--bytesize", type=int, choices=(5, 6, 7, 8), default=8)
    parser.add_argument("--parity", choices=("N", "E", "O", "M", "S"), default="N")
    parser.add_argument("--stopbits", type=float, choices=(1, 1.5, 2), default=1)
    parser.add_argument("--xonxoff", action="store_true")
    parser.add_argument("--rtscts", action="store_true")
    parser.add_argument("--dsrdtr", action="store_true")
    parser.add_argument("--dtr", choices=("off", "on"), default="off")
    parser.add_argument("--rts", choices=("off", "on"), default="off")
    parser.add_argument("--encoding", default="utf-8")


def add_output_guard_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", help="Write a transcript/report to this path")
    parser.add_argument("--force", action="store_true", help="Replace an existing output file")
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to an existing output file instead of replacing it",
    )
    parser.set_defaults(force=False, append=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="device-console",
        description="Cross-platform SSH and serial helper for bounded embedded-device diagnostics.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    doctor = subparsers.add_parser("doctor", help="Check local SSH and serial prerequisites")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=command_doctor)

    ssh_run = subparsers.add_parser("ssh-run", help="Run one or more commands through OpenSSH")
    add_ssh_connection_arguments(ssh_run)
    ssh_run.add_argument("--command", action="append", required=True, help="Remote command; repeat as needed")
    ssh_run.add_argument("--timeout", type=float, default=30, help="Per-command timeout in seconds")
    ssh_run.add_argument("--encoding", default="utf-8")
    ssh_run.add_argument("--continue-on-error", action="store_true")
    ssh_run.add_argument("--redact-env", action="append", default=[], metavar="NAME")
    ssh_run.add_argument("--json", action="store_true")
    add_output_guard_arguments(ssh_run)
    ssh_run.set_defaults(func=command_ssh_run)

    ssh_shell = subparsers.add_parser("ssh-shell", help="Open an interactive OpenSSH session")
    add_ssh_connection_arguments(ssh_shell)
    ssh_shell.add_argument("--command", help="Optional remote command")
    ssh_shell.set_defaults(func=command_ssh_shell)

    serial_ports = subparsers.add_parser("serial-ports", help="List serial ports")
    serial_ports.add_argument("--json", action="store_true")
    serial_ports.set_defaults(func=command_serial_ports)

    serial_monitor = subparsers.add_parser("serial-monitor", help="Capture bounded serial output")
    add_serial_connection_arguments(serial_monitor)
    serial_monitor.add_argument("--duration", type=float, default=30, help="Seconds; 0 runs until interrupted")
    serial_monitor.add_argument("--idle-timeout", type=float, default=0, help="Stop after this many idle seconds")
    serial_monitor.add_argument("--timeout", type=float, default=2, help="Serial write timeout basis")
    serial_monitor.add_argument("--no-timestamps", action="store_true")
    serial_monitor.add_argument("--redact-env", action="append", default=[], metavar="NAME")
    serial_monitor.add_argument("--json", action="store_true", help="Emit a JSON report instead of a text transcript")
    add_output_guard_arguments(serial_monitor)
    serial_monitor.set_defaults(func=command_serial_monitor)

    serial_run = subparsers.add_parser("serial-run", help="Log in and run commands on a serial console")
    add_serial_connection_arguments(serial_run)
    serial_run.add_argument("--command", action="append", required=True, help="Command; repeat as needed")
    serial_run.add_argument("--username")
    password_source = serial_run.add_mutually_exclusive_group()
    password_source.add_argument("--password-env", help="Environment variable holding the password")
    password_source.add_argument(
        "--ask-password",
        action="store_true",
        help="Prompt for a password without echo; requires an interactive terminal",
    )
    serial_run.add_argument("--redact-env", action="append", default=[], metavar="NAME")
    serial_run.add_argument("--prompt", default=DEFAULT_PROMPT)
    serial_run.add_argument(
        "--login-prompt",
        default=r"(?im)^(?:[A-Za-z0-9_.-]+\s+)?(?:login|username):\s*$",
    )
    serial_run.add_argument("--password-prompt", default=r"(?im)^password:\s*$")
    serial_run.add_argument("--no-login", action="store_true", help="Do not react to login/password prompts")
    serial_run.add_argument("--wake", type=int, default=1, help="Newlines sent before waiting for a prompt")
    serial_run.add_argument("--initial-delay", type=float, default=0.2)
    serial_run.add_argument("--login-timeout", type=float, default=15, help="Per-read timeout while logging in")
    serial_run.add_argument(
        "--login-budget",
        type=float,
        default=45,
        help="Total seconds allowed for the whole login exchange (default: 45)",
    )
    serial_run.add_argument("--timeout", type=float, default=30, help="Per-command prompt timeout")
    serial_run.add_argument("--newline", choices=("lf", "cr", "crlf"), default="lf")
    serial_run.add_argument("--mode", choices=("raw", "posix-shell"), default="raw")
    serial_run.add_argument("--continue-on-error", action="store_true")
    serial_run.add_argument("--json", action="store_true")
    add_output_guard_arguments(serial_run)
    serial_run.set_defaults(func=command_serial_run)

    return parser


def validate_arguments(args: argparse.Namespace) -> None:
    for field in (
        "timeout",
        "connect_timeout",
        "login_timeout",
        "login_budget",
        "duration",
        "idle_timeout",
        "initial_delay",
    ):
        if hasattr(args, field) and getattr(args, field) < 0:
            raise ToolError(f"--{field.replace('_', '-')} cannot be negative")
    if hasattr(args, "port") and isinstance(args.port, int) and not 1 <= args.port <= 65535:
        if args.subcommand in ("ssh-run", "ssh-shell"):
            raise ToolError("SSH --port must be between 1 and 65535")
        raise ToolError(f"{args.subcommand} --port must be between 1 and 65535")
    if hasattr(args, "baud") and args.baud <= 0:
        raise ToolError("--baud must be positive")
    if hasattr(args, "wake") and args.wake < 0:
        raise ToolError("--wake cannot be negative")
    if hasattr(args, "connect_timeout") and args.connect_timeout == 0:
        raise ToolError("--connect-timeout must be positive")
    if args.subcommand in ("ssh-run", "serial-run") and args.timeout == 0:
        raise ToolError("--timeout must be positive")
    if hasattr(args, "login_timeout") and args.login_timeout == 0:
        raise ToolError("--login-timeout must be positive")
    if hasattr(args, "login_budget") and args.login_budget == 0:
        raise ToolError("--login-budget must be positive")
    if getattr(args, "force", False) and getattr(args, "append", False):
        raise ToolError("--force and --append are mutually exclusive")
    if getattr(args, "json", False) and getattr(args, "append", False):
        raise ToolError("--json and --append are mutually exclusive; append produces JSON Lines, not one document")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_arguments(args)
        return int(args.func(args))
    except ToolError as exc:
        print(f"device-console: {exc}", file=sys.stderr)
        return 2
    except (LookupError, OSError, ValueError) as exc:
        print(f"device-console: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
