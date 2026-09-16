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

``deploy`` and ``verify`` reuse 125 for a result that could not be checked:

    deploy  without ``--apply`` prints the plan and exits 0 without writing.
            Applying exits 0 when every artifact was transferred, hash-verified,
            and ran cleanly; 1 when a transfer or the hash check failed, in which
            case the destination is left untouched and the partial file is
            reported, or when ``--verify-running`` found a file that is not the
            artifact; 125 when the device cannot hash the file at all, so the
            transfer stays unverified and is not activated unless
            ``--allow-unverified`` is given; 124 on a timeout.
    verify  exits 0 when the two files match, 1 when they differ, 125 when the
            device has no hash tool, so the comparison is unknown rather than
            negative.

``inspect`` and ``symbolize`` exit 1 when a check ran and failed (an
architecture mismatch, or a helper such as ``readelf`` that is not available).
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
import tempfile
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

# Search overlap retained when rescanning: long enough for any human- or
# CLI-defined prompt regex, short enough that rescans stay cheap.
SCAN_OVERLAP = 1024
# Upper bound on buffered, not-yet-matched output. Beyond this the oldest bytes
# are dropped and counted in dropped_tail_bytes so the report stays honest.
MAX_PENDING_CHARS = 1 << 20
# Per-command captured output cap, so one flooding command cannot exhaust memory
# or bury the evidence an operator actually needs.
MAX_CAPTURED_CHARS = 1 << 20

# Inspection helpers (sha256sum, readelf, addr2line) are not expected to run long;
# a stuck one must not stall the whole chain.
HELPER_TIMEOUT = 30.0
# Refuse to move more than this by default; a mistyped path or a stray rootfs image
# otherwise saturates the link before anyone notices.
DEFAULT_MAX_TRANSFER_BYTES = 256 << 20
DEFAULT_TRANSFER_TIMEOUT = 600.0

# Destinations that are kernel-owned pseudo-filesystems: writing here is never
# what the operator meant.
DESTINATION_REFUSE_PREFIXES = ("/sys", "/proc", "/dev")
# Destinations that can stop the device booting or make it unreachable. Allowed
# only with an explicit --unsafe-dest, because a wrong artifact here is expensive.
DESTINATION_RISKY_PREFIXES = ("/boot", "/lib/modules", "/etc", "/var/lib")
# Read-only on most embedded targets, so a write predictably fails; worth refusing
# up front rather than after a full transfer.
DESTINATION_READONLY_PREFIXES = ("/bin", "/sbin", "/lib", "/usr")

# Device-side digests: 64 hex is sha256, 32 is md5. Nothing here compares whole
# output lines, because the filename appears on the line too.
SHA256_PATTERN = re.compile(r"\b([0-9a-fA-F]{64})\b")
MD5_PATTERN = re.compile(r"\b([0-9a-fA-F]{32})\b")
# Addresses as they appear in kernel oops and backtrace output.
ADDRESS_PATTERN = re.compile(r"\b(?:0x)?([0-9a-fA-F]{8,16})\b")


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
    jump = getattr(args, "device_jump", None)
    if jump:
        # Reach a device that is only routable from an intermediate host without
        # asking that host for credentials of its own.
        command.extend(["-o", f"ProxyJump={jump}"])
    destination = f"{args.user}@{args.host}" if args.user else args.host
    return command, destination


def endpoint_destination(endpoint: argparse.Namespace) -> str:
    return f"{endpoint.user}@{endpoint.host}" if endpoint.user else endpoint.host


def scp_arguments(endpoint: argparse.Namespace, batch_mode: bool = True) -> list[str]:
    """Build scp options that mirror ssh_base_command().

    scp uses ``-P`` for the port where ssh uses ``-p``, so the argv cannot be
    shared; every other option is kept identical so both transports negotiate
    the same host-key policy and identity.
    """
    validate_ssh_endpoint(endpoint.host, endpoint.user)
    scp_path = shutil.which("scp")
    if not scp_path:
        raise ToolError("OpenSSH client 'scp' was not found on PATH. Run doctor for setup details.")
    strict_value = "yes" if endpoint.host_key_policy == "strict" else "accept-new"
    argv = [
        scp_path,
        "-P",
        str(endpoint.port),
        "-o",
        f"ConnectTimeout={endpoint.connect_timeout}",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        f"StrictHostKeyChecking={strict_value}",
    ]
    if batch_mode:
        argv.extend(["-o", "BatchMode=yes"])
    if endpoint.known_hosts:
        argv.extend(["-o", f"UserKnownHostsFile={str(Path(endpoint.known_hosts).expanduser())}"])
    if endpoint.identity:
        identity = Path(endpoint.identity).expanduser()
        if not identity.is_file():
            raise ToolError(f"SSH identity file does not exist: {identity}")
        argv.extend(["-i", str(identity)])
    jump = getattr(endpoint, "device_jump", None)
    if jump:
        argv.extend(["-o", f"ProxyJump={jump}"])
    return argv


def build_endpoint_namespace(args: argparse.Namespace) -> Optional[argparse.Namespace]:
    """Map the ``--build-*`` options onto the shape ssh_base_command() expects.

    Returns ``None`` when no build host was given, which means the build
    environment is this machine and only the built-in read-only helpers may run.
    """
    host = getattr(args, "build_host", None)
    if not host:
        return None
    return argparse.Namespace(
        host=host,
        user=getattr(args, "build_user", None),
        port=getattr(args, "build_port", 22),
        identity=getattr(args, "build_identity", None),
        known_hosts=getattr(args, "build_known_hosts", None),
        host_key_policy=getattr(args, "build_host_key_policy", "strict"),
        connect_timeout=getattr(args, "build_connect_timeout", 10),
    )


def _run_process(
    argv: Sequence[str],
    *,
    timeout: float,
    encoding: str,
    max_output: int = 0,
    redactions: Iterable[Optional[str]] = (),
    stdin: Any = None,
    label: Optional[str] = None,
) -> dict[str, Any]:
    """Run one local process and describe the outcome the way ssh-run does.

    Timeouts report ``exit_code: None`` rather than a fabricated status, and the
    caller decides whether ``remote_may_still_run`` applies.
    """
    command_label = label if label is not None else " ".join(str(part) for part in argv)
    started_at = utc_now()
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL if stdin is None else stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        stdout = decode_bytes(completed.stdout, encoding)
        stderr = decode_bytes(completed.stderr, encoding)
        truncated = bool(max_output) and (len(stdout) + len(stderr)) > max_output
        if truncated:
            stdout = stdout[:max_output]
            stderr = stderr[:max_output]
        return {
            "command": command_label,
            "started_at": started_at,
            "duration_seconds": round(time.monotonic() - started, 3),
            "exit_code": completed.returncode,
            "timed_out": False,
            "output_truncated": truncated,
            "stdout": redact(stdout, redactions),
            "stderr": redact(stderr, redactions),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command_label,
            "started_at": started_at,
            "duration_seconds": round(time.monotonic() - started, 3),
            "exit_code": None,
            "timed_out": True,
            "stdout": redact(decode_bytes(exc.stdout, encoding), redactions),
            "stderr": redact(decode_bytes(exc.stderr, encoding), redactions),
        }
    except OSError as exc:
        # A missing helper is an ordinary outcome on a fallback ladder: macOS has
        # no sha256sum, Windows has neither sha256sum nor file. Report it as a
        # failed command so the next rung runs, instead of aborting the whole
        # inspection with a traceback.
        return {
            "command": command_label,
            "started_at": started_at,
            "duration_seconds": round(time.monotonic() - started, 3),
            "exit_code": 127,
            "timed_out": False,
            "stdout": "",
            "stderr": f"{argv[0] if argv else 'command'}: {exc.strerror or exc}",
        }


def run_remote(
    endpoint: argparse.Namespace,
    remote_command: str,
    *,
    timeout: float,
    encoding: str = "utf-8",
    max_output: int = 0,
    redactions: Iterable[Optional[str]] = (),
) -> dict[str, Any]:
    """Run one command through OpenSSH and return an ssh-run shaped result."""
    if "\x00" in remote_command:
        raise ToolError("SSH command contains a NUL character.")
    base, destination = ssh_base_command(endpoint, batch_mode=True)
    item = _run_process(
        [*base, destination, remote_command],
        timeout=timeout,
        encoding=encoding,
        max_output=max_output,
        redactions=redactions,
        label=remote_command,
    )
    if item["timed_out"]:
        # The local ssh client was killed, but the remote command may well still
        # be running; the timeout is not proof that it stopped.
        item["remote_may_still_run"] = True
    return item


def run_local(
    argv: Sequence[str],
    *,
    timeout: float,
    encoding: str = "utf-8",
    max_output: int = 0,
    redactions: Iterable[Optional[str]] = (),
) -> dict[str, Any]:
    """Run one command on this machine and return the same result shape."""
    return _run_process(
        argv, timeout=timeout, encoding=encoding, max_output=max_output, redactions=redactions
    )


def run_helper(
    args: argparse.Namespace,
    argv: Sequence[str],
    *,
    timeout: float = HELPER_TIMEOUT,
) -> dict[str, Any]:
    """Run a read-only inspection helper on the build environment.

    With ``--build-host`` the helper runs there over SSH; otherwise it runs on
    this machine. Only the built-in helper commands may take this path -- it is
    not a general command runner.
    """
    endpoint = build_endpoint_namespace(args)
    if endpoint is None:
        return run_local(argv, timeout=timeout)
    return run_remote(endpoint, shlex.join(list(argv)), timeout=timeout)


def helper_stdout(result: dict[str, Any]) -> str:
    return result.get("stdout") or ""


def describe_failure(result: dict[str, Any]) -> str:
    detail = (result.get("stderr") or result.get("stdout") or "").strip()
    if result["timed_out"]:
        return f"timed out after {result['duration_seconds']}s"
    if detail:
        return f"exit code {result['exit_code']}: {detail.splitlines()[-1]}"
    return f"exit code {result['exit_code']}"


def first_int(text: str) -> Optional[int]:
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def validate_jump_spec(value: str) -> str:
    """Validate a ProxyJump target: ``[user@]host[:port]``.

    A control character here would inject further ssh_config directives through
    the ``-o`` value, and a leading ``-`` would be read as an option, so both are
    refused before the value reaches ssh.
    """
    if not value or value.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ToolError(
            f"Invalid --device-jump {value!r}. Use [user@]host[:port] with no spaces or control characters."
        )
    spec = value
    user: Optional[str] = None
    if "@" in spec:
        user, _, spec = spec.rpartition("@")
        if not user or not re.fullmatch(r"[A-Za-z0-9_.-]+", user):
            raise ToolError(f"Invalid user in --device-jump {value!r}.")
    host, port = spec, None
    if ":" in spec:
        host, _, port_text = spec.rpartition(":")
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise ToolError(f"Invalid port in --device-jump {value!r}; expected 1-65535.")
        port = int(port_text)
    if not host or host.startswith("-") or "@" in host:
        raise ToolError(f"Invalid host in --device-jump {value!r}.")
    normalized = f"{user}@{host}" if user else host
    return f"{normalized}:{port}" if port is not None else normalized


def parse_artifact_specs(specs: Sequence[str]) -> list[dict[str, str]]:
    """Split ``SRC`` or ``SRC:DST`` specs, naming the two halves explicitly."""
    parsed: list[dict[str, str]] = []
    for spec in specs:
        if not spec:
            raise ToolError("--artifact cannot be empty. Use SRC or SRC:DST.")
        if ":" in spec and not re.match(r"^[A-Za-z]:[\\/]", spec):
            # A Windows drive-letter path is not SRC:DST; splitting on its colon
            # would silently turn "C:\app" into source "C".
            source, _, name = spec.partition(":")
        else:
            source, name = spec, ""
        if not source:
            raise ToolError(f"--artifact {spec!r} has an empty source path.")
        if not name:
            name = source.rstrip("/").rsplit("/", 1)[-1]
        if not name or name in (".", "..") or "/" in name:
            raise ToolError(
                f"--artifact {spec!r}: the destination name must be a plain file name, not {name!r}."
            )
        parsed.append({"source": source, "name": name})
    return parsed


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def validate_destination(dest: str, allow_unsafe: bool) -> str:
    """Refuse device destinations that would damage the boot path or cannot work.

    Mirrors the refusal style of ensure_output_available(): name the exact
    obstacle and the exact remedy, and never relax a check on the user's behalf.
    """
    if not dest or not dest.startswith("/"):
        raise ToolError(f"--dest must be an absolute path on the device: {dest!r}")
    if ".." in Path(dest).parts:
        raise ToolError(f"--dest must not contain '..': {dest!r}")
    # POSIX collapses repeated slashes, so "//etc" reaches /etc on the device.
    # Check the path in the form the device will actually resolve.
    collapsed = re.sub(r"/{2,}", "/", dest)
    if collapsed != dest:
        dest = collapsed
    normalized = dest.rstrip("/") or "/"
    if normalized == "/":
        raise ToolError("--dest cannot be the device root directory; name a directory such as /tmp.")
    for prefix in DESTINATION_REFUSE_PREFIXES:
        if _under(normalized, prefix):
            raise ToolError(
                f"--dest {normalized!r} is under {prefix}, which is a kernel-owned pseudo-filesystem. "
                "Name a directory on a real filesystem, such as /tmp."
            )
    # Checked before the read-only list so /lib/modules reports the boot risk,
    # which is the more serious obstacle, rather than a generic read-only refusal.
    risky = [prefix for prefix in DESTINATION_RISKY_PREFIXES if _under(normalized, prefix)]
    if risky and not allow_unsafe:
        raise ToolError(
            f"--dest {normalized!r} is under {risky[0]}, where a wrong artifact can stop the device "
            "from booting. Re-run with --unsafe-dest only after confirming the device can recover."
        )
    for prefix in DESTINATION_READONLY_PREFIXES:
        # --unsafe-dest is the operator saying they know what they are doing, so it
        # must also unlock paths such as /lib/modules that sit under a read-only
        # prefix; otherwise that option and its documented remedy are unreachable.
        if _under(normalized, prefix) and not (allow_unsafe and risky):
            raise ToolError(
                f"--dest {normalized!r} is under {prefix}, which is read-only on most embedded targets. "
                "Name a writable directory such as /tmp or /var/tmp."
            )
    return normalized


def digest_from_text(text: str, algorithm: str) -> Optional[str]:
    pattern = SHA256_PATTERN if algorithm == "sha256" else MD5_PATTERN
    match = pattern.search(text)
    return match.group(1).lower() if match else None


def run_transfer(
    argv: Sequence[str],
    *,
    timeout: float,
    stdin_handle: Any = None,
    stdout_handle: Any = None,
    redactions: Iterable[Optional[str]] = (),
) -> dict[str, Any]:
    """Run a file-transfer process without ever decoding the payload.

    Artifacts are binary; routing them through the text pipeline that serves
    command output would corrupt them, so the payload goes straight to a file
    handle and only the status is reported.
    """
    started_at = utc_now()
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL if stdin_handle is None else stdin_handle,
            stdout=subprocess.PIPE if stdout_handle is None else stdout_handle,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "command": " ".join(str(part) for part in argv),
            "started_at": started_at,
            "duration_seconds": round(time.monotonic() - started, 3),
            "exit_code": completed.returncode,
            "timed_out": False,
            "stderr": redact(decode_bytes(completed.stderr, "utf-8"), redactions).strip(),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": " ".join(str(part) for part in argv),
            "started_at": started_at,
            "duration_seconds": round(time.monotonic() - started, 3),
            "exit_code": None,
            "timed_out": True,
            # The local client is gone but the remote end may still be writing.
            "remote_may_still_run": True,
            "stderr": redact(decode_bytes(exc.stderr, "utf-8"), redactions).strip(),
        }


def _digest_candidates(path: str, algorithm: str) -> tuple[tuple[str, ...], ...]:
    if algorithm == "md5":
        return (("md5sum", path), ("md5", "-r", path), ("openssl", "dgst", "-md5", path))
    return (("sha256sum", path), ("shasum", "-a", "256", path), ("openssl", "dgst", "-sha256", path))


def _digest_with(runner, path: str, algorithm: str) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for argv in _digest_candidates(path, algorithm):
        result = runner(argv)
        recorded = {"tool": argv[0], "exit_code": result["exit_code"], "timed_out": result["timed_out"]}
        attempts.append(recorded)
        if result["timed_out"]:
            break
        if result["exit_code"] == 0:
            digest = digest_from_text(helper_stdout(result), algorithm)
            if digest:
                return {
                    "known": True,
                    "algorithm": algorithm,
                    "value": digest,
                    "tool": argv[0],
                    "attempts": attempts,
                }
            recorded["note"] = "no digest in output"
    return {"known": False, "algorithm": algorithm, "value": None, "attempts": attempts}


def build_digest(args: argparse.Namespace, path: str, algorithm: str = "sha256") -> dict[str, Any]:
    """Hash a path on the build environment, reporting which tool produced it."""
    return _digest_with(lambda argv: run_helper(args, argv), path, algorithm)


def local_digest(path: Any, algorithm: str = "sha256") -> dict[str, Any]:
    """Hash a staged local file, so a corruption can be localized to one hop."""
    return _digest_with(
        lambda argv: run_local(argv, timeout=HELPER_TIMEOUT), str(path), algorithm
    )


def build_size(args: argparse.Namespace, path: str) -> Optional[int]:
    """Size in bytes via wc -c, which is also the existence check on BusyBox."""
    for argv in (["wc", "-c", path], ["stat", "-c", "%s", path], ["stat", "-f", "%z", path]):
        result = run_helper(args, argv)
        if result["exit_code"] == 0:
            size = first_int(helper_stdout(result))
            if size is not None and size >= 0:
                return size
    return None


def remote_hash_tools(
    endpoint: argparse.Namespace, *, timeout: float, redactions: Iterable[Optional[str]] = ()
) -> dict[str, Any]:
    """Which hash tools the device has, or why the probe could not find out.

    Only lines that are exactly a known tool name count: a login banner or a
    shell profile that prints something would otherwise be mistaken for a tool.
    """
    script = (
        "command -v sha256sum >/dev/null 2>&1 && echo sha256sum; "
        "command -v md5sum >/dev/null 2>&1 && echo md5sum; exit 0"
    )
    result = run_remote(endpoint, script, timeout=timeout, redactions=redactions)
    if result["timed_out"]:
        return {
            "ok": False,
            "tools": [],
            "error": f"the hash-tool probe timed out after {result['duration_seconds']}s",
        }
    if result["exit_code"] != 0:
        return {"ok": False, "tools": [], "error": describe_failure(result)}
    known = ("sha256sum", "md5sum")
    tools = [line.strip() for line in helper_stdout(result).splitlines() if line.strip() in known]
    return {"ok": True, "tools": tools, "error": None}


def remote_digest(
    endpoint: argparse.Namespace,
    path: str,
    *,
    timeout: float,
    redactions: Iterable[Optional[str]] = (),
) -> dict[str, Any]:
    """Hash a path on a remote host, preferring sha256 and saying which was used.

    Separates three outcomes that a caller must not confuse: the device could not
    be asked (probe-failed), it has no hash tool (no-hash-tool), or the tool ran
    and failed. Only the last two say anything about the file.
    """
    probe = remote_hash_tools(endpoint, timeout=timeout, redactions=redactions)
    if not probe["ok"]:
        return {
            "known": False,
            "algorithm": None,
            "value": None,
            "reason": "probe-failed",
            "error": probe["error"],
        }
    tools = probe["tools"]
    if not tools:
        return {"known": False, "algorithm": None, "value": None, "reason": "no-hash-tool"}
    algorithm, tool = ("sha256", "sha256sum") if "sha256sum" in tools else ("md5", "md5sum")
    result = run_remote(
        endpoint, f"{tool} {shlex.quote(path)}", timeout=timeout, redactions=redactions
    )
    if result["timed_out"]:
        return {"known": False, "algorithm": algorithm, "value": None, "reason": "hash-command-timed-out"}
    if result["exit_code"] != 0:
        return {
            "known": False,
            "algorithm": algorithm,
            "value": None,
            "reason": "hash-command-failed",
            "error": describe_failure(result),
        }
    digest = digest_from_text(helper_stdout(result), algorithm)
    if not digest:
        return {"known": False, "algorithm": algorithm, "value": None, "reason": "no-digest-in-output"}
    return {"known": True, "algorithm": algorithm, "value": digest, "tool": tool}


def probe_device(
    args: argparse.Namespace, dest_dir: str, *, timeout: float
) -> dict[str, Any]:
    """Read-only device probe: which tools exist and can the destination be written."""
    script = (
        'for t in scp cat sha256sum md5sum mv chmod rm; do '
        'if command -v "$t" >/dev/null 2>&1; then echo "tool:$t=1"; else echo "tool:$t=0"; fi; '
        "done; "
        f'if test -w {shlex.quote(dest_dir)}; then echo "dest_writable=1"; else echo "dest_writable=0"; fi; '
        'echo "uid=$(id -u 2>/dev/null || echo unknown)"'
    )
    result = run_remote(args, script, timeout=timeout)
    tools: dict[str, bool] = {}
    dest_writable: Optional[bool] = None
    uid: Optional[str] = None
    for line in helper_stdout(result).splitlines():
        key, _, value = line.strip().partition("=")
        if key.startswith("tool:"):
            tools[key[len("tool:") :]] = value == "1"
        elif key == "dest_writable":
            dest_writable = value == "1"
        elif key == "uid":
            uid = value
    return {
        "tools": tools,
        "dest_writable": dest_writable,
        "uid": uid,
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "error": None if result["exit_code"] == 0 else describe_failure(result),
    }


def download_from_build(
    args: argparse.Namespace, source: str, local_path: Path, *, timeout: float
) -> dict[str, Any]:
    """Bring the artifact to the staging directory. Payload stays binary."""
    endpoint = build_endpoint_namespace(args)
    if endpoint is None:
        return {"method": "local", "attempts": []}
    destination = endpoint_destination(endpoint)
    attempts: list[dict[str, Any]] = []
    try:
        scp_argv = scp_arguments(endpoint)
    except ToolError:
        scp_argv = []
    for label, argv in (
        ("scp-legacy-from-build", [*scp_argv, "-O", f"{destination}:{source}", str(local_path)]),
        ("scp-from-build", [*scp_argv, f"{destination}:{source}", str(local_path)]),
    ):
        if not scp_argv:
            break
        with local_path.open("wb") as handle:
            record = run_transfer(argv, timeout=timeout, stdout_handle=handle)
        record["method"] = label
        attempts.append(record)
        if record["exit_code"] == 0:
            return {"method": label, "attempts": attempts}
    # Fallback: the build host only needs cat, and the bytes never meet a decoder.
    base, build_destination = ssh_base_command(endpoint, batch_mode=True)
    with local_path.open("wb") as handle:
        record = run_transfer(
            [*base, "-T", build_destination, f"cat {shlex.quote(source)}"],
            timeout=timeout,
            stdout_handle=handle,
        )
    record["method"] = "cat-from-build"
    attempts.append(record)
    if record["exit_code"] == 0:
        return {"method": "cat-from-build", "attempts": attempts}
    return {"method": None, "attempts": attempts}


def upload_to_device(
    args: argparse.Namespace,
    local_path: Path,
    part_path: str,
    *,
    timeout: float,
    redactions: Iterable[Optional[str]] = (),
) -> dict[str, Any]:
    """Copy the staged artifact to the device, first with scp then with cat."""
    destination = endpoint_destination(args)
    attempts: list[dict[str, Any]] = []
    try:
        scp_argv = scp_arguments(args)
    except ToolError:
        scp_argv = []
    for label, extra in (("scp-legacy", ["-O"]), ("scp", [])):
        if not scp_argv:
            break
        argv = [*scp_argv, *extra, str(local_path), f"{destination}:{part_path}"]
        record = run_transfer(argv, timeout=timeout, redactions=redactions)
        record["method"] = label
        attempts.append(record)
        if record["exit_code"] == 0:
            return {"method": label, "attempts": attempts}
    # Fallback for targets whose sshd has no sftp subsystem and no scp protocol:
    # only cat is needed, and the payload is piped, never decoded.
    base, ssh_destination = ssh_base_command(args, batch_mode=True)
    with local_path.open("rb") as handle:
        record = run_transfer(
            [*base, "-T", ssh_destination, f"cat > {shlex.quote(part_path)}"],
            timeout=timeout,
            stdin_handle=handle,
            redactions=redactions,
        )
    record["method"] = "cat-to-device"
    attempts.append(record)
    if record["exit_code"] == 0:
        return {"method": "cat-to-device", "attempts": attempts}
    return {"method": None, "attempts": attempts}


def direct_rung_blocked_reason(
    args: argparse.Namespace, endpoint: argparse.Namespace
) -> Optional[str]:
    """Why the relayed scp rung must be skipped, or None when it is safe to use.

    scp applies a single option set to both operands, so a custom port, identity,
    or known_hosts file would be applied to the build host too — which would
    offer the device's private key to a different machine. Only the all-defaults
    case can be served by one argv, and everything else goes through staging.
    """
    if getattr(args, "device_jump", None):
        return "a device jump would also be applied to the build hop"
    if args.identity or endpoint.identity:
        return "an identity would be offered to both hosts, not just the device"
    if args.known_hosts or endpoint.known_hosts:
        return "a known_hosts override cannot be applied to one operand alone"
    if args.port != 22 or endpoint.port != 22:
        return "scp uses one port for both operands"
    if args.host_key_policy != "strict" or endpoint.host_key_policy != "strict":
        return "scp uses one host-key policy for both operands"
    return None


def copy_artifact_directly(
    args: argparse.Namespace, source: str, part_path: str, *, timeout: float
) -> dict[str, Any]:
    """Single scp that relays through this machine: build:SRC -> device:PART.

    Returns a ``skipped`` reason when one argv cannot serve both hops correctly.
    """
    endpoint = build_endpoint_namespace(args)
    if endpoint is None:
        return {"method": None, "attempts": [], "skipped": "the build environment is this machine"}
    blocked = direct_rung_blocked_reason(args, endpoint)
    if blocked:
        return {"method": None, "attempts": [], "skipped": blocked}
    source_destination = endpoint_destination(endpoint)
    device_destination = endpoint_destination(args)
    try:
        scp_argv = scp_arguments(args)
    except ToolError as exc:
        return {"method": None, "attempts": [], "skipped": f"scp is unavailable: {exc}"}
    attempts: list[dict[str, Any]] = []
    for label, extra in (("scp-direct-legacy", ["-O"]), ("scp-direct", [])):
        argv = [*scp_argv, *extra, f"{source_destination}:{source}", f"{device_destination}:{part_path}"]
        record = run_transfer(argv, timeout=timeout)
        record["method"] = label
        attempts.append(record)
        if record["exit_code"] == 0:
            return {"method": label, "attempts": attempts, "skipped": None}
    return {"method": None, "attempts": attempts, "skipped": None}


def part_path_for(dest_dir: str, name: str) -> str:
    """A sibling of the final path, so the final rename is atomic.

    Writing beside the destination also leaves the running binary's inode alone,
    which avoids ETXTBSY when replacing an executable that is still executing.
    """
    return f"{dest_dir.rstrip('/')}/{name}.device-console-{uuid.uuid4().hex[:8]}.part"


def dmesg_snapshot(
    args: argparse.Namespace, *, timeout: float, redactions: Iterable[Optional[str]] = ()
) -> dict[str, Any]:
    result = run_remote(args, "dmesg", timeout=timeout, redactions=redactions)
    if result["timed_out"] or result["exit_code"] != 0:
        return {"available": False, "lines": [], "error": describe_failure(result)}
    return {"available": True, "lines": helper_stdout(result).splitlines(), "error": None}


def dmesg_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Lines the kernel added while the command ran, or an honest null.

    A failed capture must never be reported as "no new kernel messages"; those are
    different findings and only one of them is reassuring.
    """
    if not before.get("available") or not after.get("available"):
        return {
            "available": False,
            "delta": None,
            "note": "dmesg could not be read on the device, so no delta is available",
            "before_error": before.get("error"),
            "after_error": after.get("error"),
        }
    before_lines = before["lines"]
    after_lines = after["lines"]
    if len(after_lines) >= len(before_lines) and after_lines[: len(before_lines)] == before_lines:
        return {"available": True, "delta": after_lines[len(before_lines) :], "wrapped": False, "note": None}
    # The ring buffer dropped the oldest lines, so a positional delta would be a
    # guess. Report the wrap rather than pretending to know what is new.
    missing = [line for line in before_lines if line not in after_lines]
    return {
        "available": True,
        "delta": after_lines,
        "wrapped": bool(missing),
        "note": "kernel ring buffer wrapped during the run; showing the whole capture instead of a delta",
    }


# Ordered: the first family whose member appears in the ELF machine string wins,
# so aarch64 and x86-64 must be checked before the families they overlap with.
# Members are compared after stripping every non-alphanumeric character, because
# readelf writes "Advanced Micro Devices X86-64" and "RISC-V" while uname writes
# "x86_64" and "riscv64" for the same thing.
ARCH_FAMILIES = (
    ("aarch64", ("aarch64", "arm64")),
    ("x86-64", ("x8664", "amd64")),
    ("i386", ("i386", "i486", "i586", "i686", "80386", "80486")),
    ("riscv", ("riscv",)),
    ("mips", ("mips",)),
    ("ppc", ("ppc", "powerpc")),
    ("s390", ("s390",)),
    ("arm", ("arm",)),
)


# Families a narrower binary *might* run under, given kernel and rootfs support
# for 32-bit userspace. Not decidable from these strings, so the answer is
# "unknown" rather than a confident "cannot run".
ARCH_PLAUSIBLE_ON = {
    "i386": ("x86-64",),
    "arm": ("aarch64",),
}


def _arch_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.strip().lower())


def _family_of(value_key: str, substring: bool) -> Optional[str]:
    for family, members in ARCH_FAMILIES:
        if substring:
            if any(member in value_key for member in members):
                return family
        elif any(value_key.startswith(member) for member in members):
            return family
    return None


def compare_architecture(machine: Optional[str], uname_m: Optional[str]) -> tuple[Optional[bool], str]:
    """Say whether an ELF machine string can run on a device reporting uname -m.

    Returns None rather than guessing when either side is unrecognised, or when a
    narrower binary might run under a wider kernel. A wrong "match" would send
    someone chasing the wrong bug, and a wrong "mismatch" after a working deploy
    is just as expensive.
    """
    if not machine or not uname_m:
        return None, "architecture comparison needs both an ELF machine and uname -m"
    machine_family = _family_of(_arch_key(machine), substring=True)
    if machine_family is None:
        return None, f"unrecognised ELF machine: {machine!r}"
    uname_family = _family_of(_arch_key(uname_m), substring=False)
    if uname_family is None:
        return None, f"unrecognised architecture from uname -m: {uname_m!r}"
    if machine_family == uname_family:
        return True, f"{machine} matches device {uname_m}"
    if uname_family in ARCH_PLAUSIBLE_ON.get(machine_family, ()):
        return None, (
            f"{machine} may run on {uname_m} if the kernel and rootfs provide support for the "
            "narrower architecture; the machine strings alone cannot settle it"
        )
    return False, f"{machine} cannot run on a device reporting {uname_m}"


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
            stdout = decode_bytes(completed.stdout, args.encoding)
            stderr = decode_bytes(completed.stderr, args.encoding)
            truncated = bool(args.max_output) and (len(stdout) + len(stderr)) > args.max_output
            if truncated:
                # Keep the head of each stream: a flooding command otherwise
                # buries the evidence the operator actually needs.
                stdout = stdout[: args.max_output]
                stderr = stderr[: args.max_output]
            item = {
                "command": remote_command,
                "started_at": started_at,
                "duration_seconds": round(time.monotonic() - started, 3),
                "exit_code": completed.returncode,
                "timed_out": False,
                "output_truncated": truncated,
                "stdout": redact(stdout, secrets),
                "stderr": redact(stderr, secrets),
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
        if item.get("output_truncated") and not args.continue_on_error:
            break
        if item["exit_code"] not in (0, None) and not args.continue_on_error:
            break

    report = {
        "tool": "device-console",
        "transport": getattr(args, "transport_label", "ssh"),
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


def configure_serial(connection: Any, args: argparse.Namespace, read_timeout: float) -> None:
    """Apply CLI arguments to a PySerial connection object.

    Split out from open_serial so the argument-to-PySerial mapping is testable
    without touching a real port.
    """
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
    # DTR/RTS default to "off": opening a port with them asserted can reset or
    # halt the target board.
    connection.dtr = args.dtr == "on"
    connection.rts = args.rts == "on"


def open_serial(args: argparse.Namespace, read_timeout: float = 0.2):
    serial, _ = load_pyserial()
    connection = serial.Serial()
    configure_serial(connection, args, read_timeout)
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
    truncated = False
    captured_chars = 0

    def emit(line: str) -> None:
        nonlocal captured_chars
        rendered = timestamped_line(redact(line, secrets), not args.no_timestamps)
        captured_chars += len(rendered) + 1
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
            "output_truncated": truncated,
            "lines": records,
        }

    try:
        connection = open_serial(args)
        emit(f"# opened {args.port} at {args.baud} baud")
        while True:
            now = time.monotonic()
            if args.max_output and captured_chars > args.max_output:
                truncated = True
                emit(f"# output truncated after {args.max_output} characters")
                break
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


def has_end_anchor(patterns: Sequence[tuple[str, re.Pattern[str]]]) -> bool:
    """True when a pattern can only match at the end of the buffer."""
    return any(re.search(r"\\[Zz]|\$$", pattern.pattern) for _, pattern in patterns)


def scan_floor(
    patterns: Sequence[tuple[str, re.Pattern[str]]], buffer: str, scanned: int
) -> int:
    """Lowest index that can still contain the start of a new match.

    With an end-anchored pattern a match must reach the end of the buffer, and
    no match can span a newline that the pattern requires, so everything before
    the last newline is irrelevant. Without one, only the SCAN_OVERLAP
    characters behind the previous cursor need re-examination.
    """
    if has_end_anchor(patterns):
        return buffer.rfind("\n") + 1
    start = scanned - SCAN_OVERLAP
    return start if start > 0 else 0


class SerialTextReader:
    def __init__(self, connection: Any, encoding: str):
        self.connection = connection
        self.encoding = encoding
        self.decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self.pending = ""
        self.dropped_tail_bytes = 0

    def read_until_any(
        self,
        patterns: Sequence[tuple[str, re.Pattern[str]]],
        timeout: float,
        capture_limit: int = 0,
    ) -> tuple[Optional[str], str]:
        buffer = self.pending
        self.pending = ""
        deadline = time.monotonic() + timeout
        # Rescanning the whole buffer on every chunk is quadratic and cost ~13s
        # for 4 MiB of output, which silently blew the command timeout and
        # reported finished commands as timed out. scan_floor() keeps each
        # rescan bounded without changing which match is reported.
        scanned = 0
        try:
            while True:
                matches: list[tuple[int, int, str]] = []
                start = scan_floor(patterns, buffer, scanned)
                for name, pattern in patterns:
                    match = pattern.search(buffer, start)
                    if match:
                        matches.append((match.start(), match.end(), name))
                if matches:
                    _, end, name = min(matches, key=lambda item: (item[0], item[1]))
                    consumed = buffer[:end]
                    self.pending = buffer[end:]
                    return name, consumed
                scanned = len(buffer)
                if time.monotonic() >= deadline:
                    return None, buffer
                # Stop reading as soon as the capture budget is spent: continuing
                # would only collect bytes the caller will discard anyway.
                if capture_limit and len(buffer) >= capture_limit:
                    return None, buffer
                waiting = getattr(self.connection, "in_waiting", 0)
                data = self.connection.read(max(1, min(waiting or 1, 65536)))
                if data:
                    buffer += self.decoder.decode(data)
                    # Bound memory on flood output: keep the search overlap plus the
                    # unconsumed tail, which still covers any pattern a human or the
                    # CLI defines. Dropped bytes are reported as dropped_tail_bytes.
                    if len(buffer) > MAX_PENDING_CHARS:
                        dropped = len(buffer) - MAX_PENDING_CHARS
                        self.dropped_tail_bytes += len(buffer[:dropped].encode(self.encoding, "replace"))
                        buffer = buffer[dropped:]
                        scanned = max(0, scanned - dropped)
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


def capture_login(
    reader: SerialTextReader,
    connection: Any,
    args: argparse.Namespace,
    prompt: re.Pattern[str],
    login_prompt: re.Pattern[str],
    password_prompt: re.Pattern[str],
    password: Optional[str],
    session_chunks: list[str],
    newline: bytes,
) -> re.Pattern[str]:
    """Drive the login exchange and return the prompt pattern to use for commands.

    Raises ToolError when the console never reaches a prompt within
    --login-budget. The budget bounds the whole exchange, not just one read.
    """
    login_patterns = [("prompt", prompt)]
    if not args.no_login:
        login_patterns.extend([("login", login_prompt), ("password", password_prompt)])

    budget = args.login_budget
    saw_output = False
    username_sent = False
    password_sent = False
    active_prompt = prompt
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
            if args.prompt == DEFAULT_PROMPT:
                observed_matches = list(prompt.finditer(chunk))
                if observed_matches:
                    observed = observed_matches[-1].group(0)
                    active_prompt = re.compile(r"(?m)^" + re.escape(observed) + r"\Z")
            return active_prompt
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
    raise ToolError(f"Could not reach a serial shell prompt within --login-budget={args.login_budget:g}s.")


def command_serial_run(args: argparse.Namespace) -> int:
    ensure_output_available(args.output, force=args.force, append=args.append)
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
    validate_single_line_secrets(secrets)
    if password:
        secrets.append(password)

    newline = newline_bytes(args.newline)
    session_chunks: list[str] = []
    results: list[dict[str, Any]] = []
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

        active_prompt = capture_login(
            reader,
            connection,
            args,
            prompt,
            login_prompt,
            password_prompt,
            password,
            session_chunks,
            newline,
        )

        for command in args.command:
            wire, marker = wire_command(command, args.mode)
            started_at = utc_now()
            started = time.monotonic()
            connection.write(wire.encode(args.encoding) + newline)
            connection.flush()
            matched, output = reader.read_until_any(
                [("prompt", active_prompt)], args.timeout, capture_limit=args.max_output
            )
            truncated = bool(args.max_output) and len(output) >= args.max_output
            if truncated:
                output = output[: args.max_output]
            item = {
                "command": command,
                "started_at": started_at,
                "duration_seconds": round(time.monotonic() - started, 3),
                "exit_code": None,
                # Hitting the capture cap is not a timeout: the prompt simply was
                # not reached before the budget was spent.
                "timed_out": matched is None and not truncated,
                "output_truncated": truncated,
                "output": redact(output, secrets),
            }
            resolve_serial_exit_code(item, marker)
            results.append(item)
            if item["timed_out"] and not args.continue_on_error:
                break
            if truncated and not args.continue_on_error:
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
    if reader is not None and reader.dropped_tail_bytes:
        report["dropped_tail_bytes"] = reader.dropped_tail_bytes
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


def device_endpoint_present(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "host", None))


def build_view_namespace(args: argparse.Namespace) -> argparse.Namespace:
    """Re-shape a build-run namespace so command_ssh_run can drive the build host.

    Reusing the ssh-run loop verbatim is deliberate: build-run then inherits the
    same truncation, redaction, timeout, and break-on-error semantics rather than
    growing a second, subtly different implementation.
    """
    endpoint = build_endpoint_namespace(args)
    if endpoint is None:
        raise ToolError(
            "build-run requires --build-host: it runs arbitrary commands, so it never runs locally."
        )
    view = argparse.Namespace(**vars(args))
    view.host = endpoint.host
    view.user = endpoint.user
    view.port = endpoint.port
    view.identity = endpoint.identity
    view.known_hosts = endpoint.known_hosts
    view.host_key_policy = endpoint.host_key_policy
    view.connect_timeout = endpoint.connect_timeout
    # A jump host is how the agent reaches the device, never the build host.
    view.device_jump = None
    view.transport_label = "ssh-build"
    return view


def command_build_run(args: argparse.Namespace) -> int:
    return command_ssh_run(build_view_namespace(args))


def readelf_field(text: str, label: str) -> Optional[str]:
    match = re.search(rf"^\s*{re.escape(label)}:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def command_inspect(args: argparse.Namespace) -> int:
    source = args.artifact
    helpers = {
        "file": ["file", "-b", source],
        "readelf_header": ["readelf", "-h", source],
        "readelf_program": ["readelf", "-l", source],
        "readelf_dynamic": ["readelf", "-d", source],
        "readelf_notes": ["readelf", "-n", source],
        "size": ["size", source],
    }
    raw: dict[str, Any] = {}
    texts: dict[str, str] = {}
    for name, argv in helpers.items():
        result = run_helper(args, argv)
        texts[name] = helper_stdout(result)
        raw[name] = {
            "command": " ".join(argv),
            "available": result["exit_code"] == 0,
            "exit_code": result["exit_code"],
            "note": None if result["exit_code"] == 0 else describe_failure(result),
        }
    header_text = texts.get("readelf_header", "")
    machine = readelf_field(header_text, "Machine")
    data = readelf_field(header_text, "Data")
    elf_type = readelf_field(header_text, "Type")
    build_id = readelf_field(texts.get("readelf_notes", ""), "Build ID")
    interpreter = None
    match = re.search(r"\[Requesting program interpreter: ([^\]]+)\]", texts.get("readelf_program", ""))
    if match:
        interpreter = match.group(1)
    needed = re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", texts.get("readelf_dynamic", ""))
    file_text = texts.get("file", "")
    stripped: Optional[bool] = None
    if "ELF" in file_text:
        # Only an ELF has a meaningful stripped state; `file` exits 0 with
        # "cannot open ..." for a mistyped path, which is not a stripped binary.
        stripped = "not stripped" not in file_text.lower()

    report: dict[str, Any] = {
        "tool": "device-console",
        "transport": "build-inspect",
        "artifact": source,
        "build_host": getattr(args, "build_host", None) or "local",
        "captured_at": utc_now(),
        "elf": {
            "machine": machine,
            "data": data,
            "type": elf_type,
            "interpreter": interpreter,
            "needed_libraries": needed,
            "build_id": build_id,
            "stripped": stripped,
            "file": file_text.strip() or None,
            "size": (texts.get("size") or "").strip() or None,
        },
        "helpers": raw,
        "arch_match": None,
        "arch_note": "the device was not queried, so no architecture comparison was made",
    }

    if device_endpoint_present(args):
        uname_result = run_remote(args, "uname -m; uname -r", timeout=args.timeout)
        report["device"] = {
            "target": endpoint_destination(args),
            "exit_code": uname_result["exit_code"],
            "output": helper_stdout(uname_result).strip(),
            "error": None if uname_result["exit_code"] == 0 else describe_failure(uname_result),
        }
        lines = [line.strip() for line in helper_stdout(uname_result).splitlines() if line.strip()]
        uname_m = lines[0] if lines else None
        report["device"]["machine"] = uname_m
        report["device"]["kernel"] = lines[1] if len(lines) > 1 else None
        match_value, note = compare_architecture(machine, uname_m)
        report["arch_match"] = match_value
        report["arch_note"] = note

    write_json_report(args.output, report, force=args.force, append=args.append)
    if args.json:
        emit_json(report)
    else:
        print(f"artifact: {source} (on {report['build_host']})")
        for key, value in report["elf"].items():
            if value not in (None, "", []):
                print(f"  {key}: {value}")
        if report["arch_match"] is not None:
            verdict = "MATCH" if report["arch_match"] else "MISMATCH"
            print(f"  architecture: {verdict} - {report['arch_note']}")
        else:
            print(f"  architecture: unknown - {report['arch_note']}")
        for name, detail in raw.items():
            if not detail["available"]:
                print(f"  [{name} unavailable] {detail['note']}", file=sys.stderr)

    # A report that identified nothing, or could not ask the device, must not exit
    # like a completed inspection: a caller gating on the status would read it as
    # "inspected and compatible".
    if machine is None:
        return 1
    if report["arch_match"] is False:
        return 1
    if (report.get("device") or {}).get("error"):
        return 1
    return 0


def command_verify(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "tool": "device-console",
        "transport": "verify",
        "build_path": args.build_path,
        "device_path": args.device_path,
        "target": endpoint_destination(args),
        "captured_at": utc_now(),
    }
    device = remote_digest(args, args.device_path, timeout=args.timeout)
    report["device_digest"] = device
    if not device["known"]:
        report["verified"] = None
        report["verification_known"] = False
        report["note"] = describe_hash_unknown(device, "the two files cannot be compared")
        write_json_report(args.output, report, force=args.force, append=args.append)
        if args.json:
            emit_json(report)
        else:
            print(f"verification unknown: {report['note']}", file=sys.stderr)
        return 125

    build = build_digest(args, args.build_path, algorithm=device["algorithm"])
    report["build_digest"] = build
    report["algorithm"] = device["algorithm"]
    if not build["known"]:
        report["verified"] = None
        report["verification_known"] = False
        report["note"] = f"the build side could not produce a {device['algorithm']} digest"
        write_json_report(args.output, report, force=args.force, append=args.append)
        if args.json:
            emit_json(report)
        else:
            print(f"verification unknown: {report['note']}", file=sys.stderr)
        return 125

    matched = build["value"] == device["value"]
    report["verified"] = matched
    report["verification_known"] = True
    report["note"] = (
        "the device file is byte-identical to the build artifact"
        if matched
        else "the device file differs from the build artifact"
    )
    write_json_report(args.output, report, force=args.force, append=args.append)
    if args.json:
        emit_json(report)
    else:
        if matched:
            print(f"match: {args.device_path} == {args.build_path} ({device['algorithm']})")
        else:
            print(
                f"mismatch: {args.device_path} is not {args.build_path}\n"
                f"  build  {device['algorithm']} {build['value']}\n"
                f"  device {device['algorithm']} {device['value']}",
                file=sys.stderr,
            )
    return 0 if matched else 1


def normalize_address(value: str, offset: int) -> str:
    text = value.strip()
    if not re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]+", text):
        raise ToolError(f"Not a hexadecimal address: {value!r}")
    number = int(text, 16) - offset
    if number < 0:
        raise ToolError(f"Address {value!r} is smaller than --kaslr-offset; check the offset.")
    return f"0x{number:x}"


def addresses_from_log(path: str, limit: int) -> tuple[list[str], bool]:
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(f"Cannot read --addresses-from {path}: {exc.strerror or exc}") from exc
    seen: list[str] = []
    for match in ADDRESS_PATTERN.finditer(text):
        canonical = f"0x{int(match.group(1), 16):x}"
        if canonical not in seen:
            seen.append(canonical)
        if len(seen) >= limit:
            return seen, True
    return seen, False


def command_symbolize(args: argparse.Namespace) -> int:
    offset = int(args.kaslr_offset, 16) if args.kaslr_offset else 0
    addresses: list[str] = []
    truncated = False
    for value in args.address or []:
        addresses.append(normalize_address(value, offset))
    skipped: list[str] = []
    if args.addresses_from:
        extracted, truncated = addresses_from_log(args.addresses_from, args.max_addresses)
        for value in extracted:
            try:
                normalized = normalize_address(value, offset)
            except ToolError:
                # A log is full of hexadecimal tokens that are not addresses; one
                # of them being impossible under the offset must not discard the
                # addresses the log did contain.
                skipped.append(value)
                continue
            if normalized not in addresses:
                addresses.append(normalized)
    if not addresses:
        raise ToolError("symbolize needs at least one --address or --addresses-from file.")

    prefix = args.toolchain or ""
    probe = run_helper(args, ["sh", "-c", f"command -v {shlex.quote(prefix + 'addr2line')} || command -v addr2line || true"])
    resolved = helper_stdout(probe).strip().splitlines()
    toolchain_note: Optional[str] = None
    if resolved:
        candidate = resolved[0].strip()
        # Use the resolved path so a missing prefix degrades to plain addr2line
        # instead of failing with "command not found" -- but say so, because a
        # host addr2line may not describe the target's binaries.
        if prefix and not candidate.endswith(prefix + "addr2line"):
            toolchain_note = (
                f"{prefix}addr2line is not on the build host; using {candidate}, whose results "
                "may not describe the target's binaries"
            )
            prefix = ""
    elif prefix:
        toolchain_note = (
            f"could not confirm that {prefix}addr2line is on the build host; running it as given"
        )
    command = [
        f"{prefix}addr2line",
        "-f",
        "-C",
        "-e",
        args.binary,
        *addresses,
    ]
    result = run_helper(args, command, timeout=args.timeout)
    lines = [line for line in helper_stdout(result).splitlines()]
    frames: list[dict[str, Any]] = []
    for index, address in enumerate(addresses):
        function = lines[index * 2] if index * 2 < len(lines) else None
        location = lines[index * 2 + 1] if index * 2 + 1 < len(lines) else None
        frames.append({"address": address, "function": function, "location": location})
    report: dict[str, Any] = {
        "tool": "device-console",
        "transport": "symbolize",
        "build_host": getattr(args, "build_host", None) or "local",
        "binary": args.binary,
        "command": " ".join(command),
        "captured_at": utc_now(),
        "kaslr_offset": args.kaslr_offset,
        "toolchain_note": toolchain_note,
        "kaslr_offset_applied": bool(offset),
        "kaslr_note": (
            f"{hex(offset)} was subtracted from every address; if the device has KASLR disabled "
            "the addresses must be symbolized without an offset"
            if offset
            else "no KASLR offset was applied; if the device randomizes the kernel base, every "
            "symbol below is shifted by that unknown amount"
        ),
        "addresses_truncated": truncated,
        "addresses_skipped": skipped,
        "exit_code": result["exit_code"],
        "frames": frames,
        "error": None if result["exit_code"] == 0 else describe_failure(result),
    }
    write_json_report(args.output, report, force=args.force, append=args.append)
    if args.json:
        emit_json(report)
    else:
        print(f"$ {report['command']}")
        for frame in frames:
            print(f"  {frame['address']}  {frame['function'] or '??'}  {frame['location'] or '??'}")
        if report["error"]:
            print(f"[addr2line] {report['error']}", file=sys.stderr)
    if result["timed_out"]:
        return 124
    return 0 if result["exit_code"] == 0 else 1


def record_exit_code(record: dict[str, Any]) -> int:
    """124 for a timeout, 1 for any other failure, so callers can tell them apart."""
    if record.get("timed_out"):
        return 124
    return 0 if record.get("exit_code") == 0 else 1


def attempts_exit_code(attempts: Sequence[dict[str, Any]]) -> int:
    """Exit status for a transfer ladder whose every rung failed."""
    if any(attempt.get("timed_out") for attempt in attempts):
        return 124
    return 1


def describe_hash_unknown(device_digest: dict[str, Any], subject: str) -> str:
    """Say why a digest is missing, without blaming the file for a dead connection."""
    reason = device_digest.get("reason")
    if reason == "no-hash-tool":
        return f"the device has no sha256sum or md5sum, so {subject} could not be verified"
    if reason == "probe-failed":
        return (
            f"the device could not be asked for a digest ({device_digest.get('error')}), "
            f"so {subject} could not be verified"
        )
    return f"the device hash command failed ({reason}), so {subject} could not be verified"


def expected_digest(
    args: argparse.Namespace, item: dict[str, Any], algorithm: str, fresh: dict[str, Any]
) -> dict[str, Any]:
    """The digest the transferred bytes should have.

    Prefers the staged copy's hash, because those are the bytes that were sent;
    then the apply-time hash of the source. Only a device that used a different
    algorithm forces a third hash of the source.
    """
    staged = item.get("staged_digest")
    if staged and staged.get("known") and staged.get("algorithm") == algorithm:
        return {"known": True, "value": staged["value"], "source": "the staged copy"}
    if fresh.get("known") and fresh.get("algorithm") == algorithm:
        return {"known": True, "value": fresh["value"], "source": "the build artifact at apply time"}
    recomputed = build_digest(args, item["source"], algorithm=algorithm)
    if recomputed.get("known"):
        return {
            "known": True,
            "value": recomputed["value"],
            "source": f"the build artifact re-hashed as {algorithm}",
        }
    return {"known": False, "value": None, "source": None}


def verify_running_path(
    args: argparse.Namespace,
    report: dict[str, Any],
    items: list[dict[str, Any]],
    secrets: Iterable[Optional[str]],
) -> int:
    """Compare a device path with the deployed artifacts, once for the whole run."""
    if not args.verify_running:
        return 0
    running = remote_digest(args, args.verify_running, timeout=args.timeout, redactions=secrets)
    entry: dict[str, Any] = {
        "path": args.verify_running,
        "digest": running,
        "matches_artifact": None,
        "matched_artifact": None,
        "compared": [],
    }
    if not running["known"]:
        entry["note"] = describe_hash_unknown(running, f"{args.verify_running} could not be compared")
        report["verify_running"] = entry
        report["steps"].append({"step": "verify-running", "at": utc_now(), "status": "unverified"})
        return 125
    compared_any = False
    for item in items:
        # Compare like with like: the device may only offer md5, so the artifact is
        # hashed again in whatever algorithm the device used.
        comparable = build_digest(args, item["source"], algorithm=running["algorithm"])
        entry["compared"].append({"artifact": item["name"], "digest": comparable})
        if not comparable["known"]:
            continue
        compared_any = True
        if comparable["value"] == running["value"]:
            entry["matches_artifact"] = True
            entry["matched_artifact"] = item["name"]
            break
    if entry["matches_artifact"] is None:
        # Only call it a mismatch when a comparison was actually possible.
        entry["matches_artifact"] = False if compared_any else None
    entry["comparison_known"] = compared_any
    report["verify_running"] = entry
    if entry["matches_artifact"] is None:
        report["steps"].append({"step": "verify-running", "at": utc_now(), "status": "unverified"})
        return 125
    report["steps"].append(
        {
            "step": "verify-running",
            "at": utc_now(),
            "status": "match" if entry["matches_artifact"] else "mismatch",
        }
    )
    return 0 if entry["matches_artifact"] else 1


def run_deployed_command(
    args: argparse.Namespace,
    report: dict[str, Any],
    item: dict[str, Any],
    secrets: Iterable[Optional[str]],
) -> int:
    """Run --run once, capturing kernel messages around it when asked."""
    if not args.run:
        return 0
    before = (
        dmesg_snapshot(args, timeout=args.timeout, redactions=secrets) if args.capture_dmesg else None
    )
    run_result = run_remote(
        args, args.run, timeout=args.timeout, max_output=args.max_output, redactions=secrets
    )
    after = (
        dmesg_snapshot(args, timeout=args.timeout, redactions=secrets) if args.capture_dmesg else None
    )
    item["run"] = run_result
    if args.capture_dmesg:
        item["dmesg"] = dmesg_delta(before, after)
    report["steps"].append(
        {
            "step": "run",
            "at": utc_now(),
            "status": "timeout" if run_result["timed_out"] else f"exit={run_result['exit_code']}",
        }
    )
    return record_exit_code(run_result)


def deploy_plan_steps(args: argparse.Namespace, items: list[dict[str, Any]], dest_dir: str) -> dict[str, Any]:
    """The exact commands the apply path will run, built by the same code shape.

    Keeping this next to the executor is what makes the dry run trustworthy: a
    reviewer compares two lists instead of re-deriving the plan from prose.
    """
    commands: list[str] = []
    for item in items:
        commands.append(f"transfer {item['source']} -> {item['dest_path']} (staged at {item['part_path']})")
        commands.append(f"mv -f {item['part_path']} {item['dest_path']}")
    if args.chmod:
        for item in items:
            commands.append(f"chmod {args.chmod} {item['dest_path']}")
    if args.run:
        commands.append(args.run)
    if args.verify_running:
        commands.append(f"verify that {args.verify_running} matches the artifact")
    if args.capture_dmesg:
        commands.append("dmesg (before and after the run)")
    skipped = items[0].get("direct_rung_skipped") if items else None
    order = ["scp-direct (build:SRC -> device:PART, relayed by this machine)"]
    if skipped:
        order = [f"scp-direct: skipped, {skipped}"]
    order.append("stage locally, then scp or cat to the device")
    return {
        "steps": ["resolve", "probe", "transfer", "verify-hash", "activate", "capture"],
        "dest": dest_dir,
        "commands": commands,
        "direct_rung_skipped": skipped,
        "transfer_order": order,
        "note": (
            "the device destination is only replaced after its hash matches; until then the "
            "artifact sits beside it as a .part file"
        ),
    }


def print_deploy_plan(report: dict[str, Any]) -> None:
    print("PLAN (nothing has been written to the device)")
    print(f"  device:   {report['target']}")
    print(f"  dest:     {report['dest']}")
    probe = report.get("device_probe") or {}
    if probe.get("dest_writable") is False:
        print("  WARNING:  the destination directory is not writable by this user")
    if probe.get("error"):
        # The plan is a prediction built from this probe; say when it was blind.
        print(
            f"  WARNING:  the device probe did not complete ({probe['error']}), "
            "so this plan is less certain than usual",
            file=sys.stderr,
        )
    for item in report["artifacts"]:
        digest = item["build_digest"]
        print(
            f"  artifact: {item['source']} -> {item['dest_path']} "
            f"({item['size_bytes']} bytes, {digest['algorithm']} {digest['value'][:16]}...)"
        )
    if report["plan"].get("direct_rung_skipped"):
        print(f"  transfer: stage-and-push ({report['plan']['direct_rung_skipped']})")
    for command in report["plan"]["commands"]:
        print(f"  would:    {command}")


def command_deploy(args: argparse.Namespace) -> int:
    ensure_output_available(args.output, force=args.force, append=args.append)
    secrets = secret_values_from_env(args.redact_env)
    validate_single_line_secrets(secrets)
    items = parse_artifact_specs(args.artifact)
    dest_dir = validate_destination(args.dest, args.unsafe_dest)
    _, destination = ssh_base_command(args, batch_mode=True)

    report: dict[str, Any] = {
        "tool": "device-console",
        "transport": "ssh",
        "operation": "deploy",
        "target": destination,
        "port": args.port,
        "dest": dest_dir,
        "applied": bool(args.apply),
        "captured_at": utc_now(),
        "artifacts": items,
        "steps": [],
    }

    # --- read-only: describe the artifact on the build side -------------------
    for item in items:
        item["dest_path"] = f"{dest_dir.rstrip('/')}/{item['name']}"
        item["part_path"] = part_path_for(dest_dir, item["name"])
        digest = build_digest(args, item["source"])
        item["build_digest"] = digest
        if not digest["known"]:
            raise ToolError(
                f"Cannot hash {item['source']!r} on the build environment "
                f"({getattr(args, 'build_host', None) or 'local'}). Check the path and that "
                "sha256sum, shasum, or openssl is available there."
            )
        size = build_size(args, item["source"])
        item["size_bytes"] = size
        if size is None:
            raise ToolError(
                f"Cannot read the size of {item['source']!r} on the build environment. Check the path."
            )
        if args.max_transfer_bytes and size > args.max_transfer_bytes:
            raise ToolError(
                f"{item['name']} is {size} bytes, above --max-transfer-bytes ({args.max_transfer_bytes}). "
                "Raise the limit deliberately if this transfer is intended."
            )
    report["steps"].append({"step": "resolve", "at": utc_now(), "status": "ok"})

    # --- read-only: what the device can do ------------------------------------
    probe = probe_device(args, dest_dir, timeout=args.timeout)
    report["device_probe"] = probe
    report["steps"].append(
        {
            "step": "probe",
            "at": utc_now(),
            "status": "ok" if probe["exit_code"] == 0 else "incomplete",
            "error": probe["error"],
        }
    )

    # Decide at plan time whether one scp argv can serve both hops, so the dry run
    # can say which rungs it will take instead of leaving it to the apply.
    build_endpoint = build_endpoint_namespace(args)
    direct_skip = (
        direct_rung_blocked_reason(args, build_endpoint)
        if build_endpoint is not None
        else "the build environment is this machine"
    )
    for item in items:
        item["direct_rung_skipped"] = direct_skip

    report["plan"] = deploy_plan_steps(args, items, dest_dir)
    report["status"] = "planned"

    if not args.apply:
        # The gate: everything below this line can write to the device, and none
        # of it is reachable without --apply.
        if args.json:
            emit_json(report)
        else:
            print_deploy_plan(report)
            print(
                "PLAN ONLY - nothing was written to the device. Re-run with --apply to execute.",
                file=sys.stderr,
            )
        write_json_report(args.output, report, force=args.force, append=args.append)
        return 0

    if not args.json:
        print_deploy_plan(report)

    # --- mutating: transfer, verify, then activate ----------------------------
    stage_dir: Optional[Path] = None
    cleanup_required = False
    left_behind: list[str] = []
    worst_exit = 0
    try:
        for item in items:
            # Recompute at apply time: a rebuild between the dry run and the apply
            # would otherwise make the planned hash stale and unverifiable. This
            # value, not a later re-read, is what the transferred bytes represent.
            fresh = build_digest(args, item["source"])
            item["build_digest_at_apply"] = fresh

            attempts: list[dict[str, Any]] = []
            transfer = copy_artifact_directly(
                args, item["source"], item["part_path"], timeout=args.transfer_timeout
            )
            method = transfer["method"]
            attempts.extend(transfer["attempts"])
            item["direct_rung_skipped"] = transfer["skipped"]
            device_attempts = len(transfer["attempts"])
            staged_local: Optional[Path] = None
            if method is None:
                if build_endpoint_namespace(args) is None:
                    # The artifact is already on this machine, so there is nothing
                    # to stage; copying it to a temporary directory first would only
                    # add a hop that could fail on its own.
                    staged_local = Path(item["source"]).expanduser()
                else:
                    if stage_dir is None:
                        if args.stage_dir:
                            stage_dir = Path(args.stage_dir).expanduser()
                            cleanup_required = False
                        else:
                            stage_dir = Path(tempfile.mkdtemp(prefix="device-console-deploy-"))
                            cleanup_required = True
                        stage_dir.mkdir(parents=True, exist_ok=True)
                    staged_local = stage_dir / item["name"]
                    staged = download_from_build(
                        args, item["source"], staged_local, timeout=args.transfer_timeout
                    )
                    attempts.extend(staged["attempts"])
                    if staged["method"] is None:
                        staged_local = None
                if staged_local is not None:
                    uploaded = upload_to_device(
                        args,
                        staged_local,
                        item["part_path"],
                        timeout=args.transfer_timeout,
                        redactions=secrets,
                    )
                    attempts.extend(uploaded["attempts"])
                    device_attempts += len(uploaded["attempts"])
                    method = uploaded["method"]
            item["transfer"] = {"method": method, "attempts": attempts}
            report["steps"].append(
                {"step": "transfer", "at": utc_now(), "status": "ok" if method else "failed", "method": method}
            )
            if method is None:
                # Only claim a leftover when something was actually sent to the
                # device; a failure on the build hop leaves nothing there.
                if device_attempts:
                    left_behind.append(item["part_path"])
                report["status"] = "aborted"
                report["aborted_before_activation"] = True
                report["error"] = f"transfer failed for {item['name']}; no rung succeeded"
                worst_exit = max(worst_exit, attempts_exit_code(attempts))
                break

            if staged_local is not None:
                item["staged_digest"] = local_digest(staged_local, item["build_digest"]["algorithm"])

            device = remote_digest(args, item["part_path"], timeout=args.timeout, redactions=secrets)
            item["device_digest"] = device
            if not device["known"]:
                item["verified"] = None
                item["verification_known"] = False
                item["verification_note"] = describe_hash_unknown(device, "the transfer")
                report["steps"].append(
                    {"step": "verify-hash", "at": utc_now(), "status": "unverified", "note": item["verification_note"]}
                )
                if not args.allow_unverified:
                    left_behind.append(item["part_path"])
                    report["status"] = "unverified"
                    report["aborted_before_activation"] = True
                    report["error"] = item["verification_note"]
                    worst_exit = max(worst_exit, 125)
                    break
            else:
                expected = expected_digest(args, item, device["algorithm"], fresh)
                item["expected_digest"] = expected
                if not expected["known"]:
                    item["verified"] = None
                    item["verification_known"] = False
                    item["verification_note"] = (
                        f"the build side could not produce a {device['algorithm']} digest, "
                        "so the transfer could not be checked"
                    )
                    report["steps"].append(
                        {"step": "verify-hash", "at": utc_now(), "status": "unverified", "note": item["verification_note"]}
                    )
                    if not args.allow_unverified:
                        left_behind.append(item["part_path"])
                        report["status"] = "unverified"
                        report["aborted_before_activation"] = True
                        report["error"] = item["verification_note"]
                        worst_exit = max(worst_exit, 125)
                        break
                else:
                    matched = expected["value"] == device["value"]
                    item["verified"] = matched
                    item["verification_known"] = True
                    report["steps"].append(
                        {
                            "step": "verify-hash",
                            "at": utc_now(),
                            "status": "ok" if matched else "mismatch",
                            "algorithm": device["algorithm"],
                        }
                    )
                    if not matched:
                        left_behind.append(item["part_path"])
                        report["status"] = "aborted"
                        report["aborted_before_activation"] = True
                        report["error"] = (
                            f"{item['name']} does not match the build artifact "
                            f"(expected {expected['value']} from {expected['source']}, "
                            f"device {device['value']}); the file was left as a .part "
                            "and the destination was not touched"
                        )
                        worst_exit = max(worst_exit, 1)
                        break

            # The .part is the only thing written so far; replacing the real path
            # now means the bytes that become the artifact are the verified bytes.
            move = run_remote(
                args,
                f"mv -f {shlex.quote(item['part_path'])} {shlex.quote(item['dest_path'])}",
                timeout=args.timeout,
                redactions=secrets,
            )
            item["move"] = move
            report["steps"].append(
                {"step": "activate", "at": utc_now(), "status": "ok" if move["exit_code"] == 0 else "failed"}
            )
            if move["exit_code"] != 0:
                left_behind.append(item["part_path"])
                report["status"] = "aborted"
                report["error"] = f"could not move the verified file into place: {describe_failure(move)}"
                worst_exit = max(worst_exit, record_exit_code(move))
                break

            if args.chmod:
                chmod = run_remote(
                    args,
                    f"chmod {shlex.quote(args.chmod)} {shlex.quote(item['dest_path'])}",
                    timeout=args.timeout,
                    redactions=secrets,
                )
                item["chmod"] = chmod
                if chmod["exit_code"] != 0:
                    report["status"] = "aborted"
                    report["error"] = f"chmod failed: {describe_failure(chmod)}"
                    worst_exit = max(worst_exit, record_exit_code(chmod))
                    break
        if report["status"] == "planned":
            # --run and --verify-running appear once in the plan, so they run once
            # for the whole deploy rather than once per artifact.
            worst_exit = max(worst_exit, verify_running_path(args, report, items, secrets))
            worst_exit = max(worst_exit, run_deployed_command(args, report, items[0], secrets))
            report["status"] = "applied"
    except (Exception, KeyboardInterrupt) as failure:
        # Preserve what already happened: at this point the device may have been
        # written to, and a report that never reaches disk would hide that.
        report["status"] = "interrupted"
        report["partially_applied"] = True
        report["error"] = {"type": type(failure).__name__, "message": redact(str(failure), secrets)}
        if left_behind:
            report["left_behind"] = left_behind
            report["cleanup_command"] = "rm -f " + " ".join(shlex.quote(path) for path in left_behind)
        try:
            write_json_report(args.output, report, force=True, append=args.append)
            if args.json:
                emit_json(report)
            else:
                print(f"device-console: interrupted after partial writes: {failure}", file=sys.stderr)
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
    finally:
        if stage_dir is not None and cleanup_required and not args.keep_stage:
            shutil.rmtree(stage_dir, ignore_errors=True)

    if left_behind:
        report["left_behind"] = left_behind
        report["cleanup_command"] = "rm -f " + " ".join(shlex.quote(path) for path in left_behind)
    report["captured_at"] = utc_now()
    write_json_report(args.output, report, force=args.force, append=args.append)
    if args.json:
        emit_json(report)
    else:
        print(f"{report['status']}: {report['target']}:{report['dest']}")
        for item in items:
            transfer = item.get("transfer") or {}
            if transfer.get("method"):
                print(f"  {item['name']}: transferred via {transfer['method']}")
            if item.get("verified") is not None:
                verdict = "verified" if item["verified"] else "MISMATCH"
                print(f"  {item['name']}: {verdict}")
            elif item.get("verification_note"):
                print(f"  {item['name']}: UNVERIFIED - {item['verification_note']}", file=sys.stderr)
            run_result = item.get("run")
            if run_result:
                status = "timeout" if run_result["timed_out"] else f"exit={run_result['exit_code']}"
                print(f"  run {item.get('dest_path')}: {status}")
                if run_result["stdout"]:
                    print(run_result["stdout"], end="" if run_result["stdout"].endswith("\n") else "\n")
                if run_result["stderr"]:
                    print(run_result["stderr"], file=sys.stderr, end="" if run_result["stderr"].endswith("\n") else "\n")
            dmesg = item.get("dmesg")
            if dmesg:
                if not dmesg["available"]:
                    print(f"  [dmesg] {dmesg['note']}", file=sys.stderr)
                elif dmesg.get("wrapped"):
                    # The baseline was overwritten, so these lines include
                    # pre-existing ones and must not be labelled as new.
                    print(f"  [dmesg] {dmesg['note']}", file=sys.stderr)
                    for line in dmesg["delta"]:
                        print(f"    {line}")
                elif dmesg["delta"]:
                    print(f"  kernel messages during the run ({len(dmesg['delta'])} lines):")
                    for line in dmesg["delta"]:
                        print(f"    {line}")
                else:
                    # Captured successfully and genuinely empty: say so, because
                    # silence here is a finding, not a missing answer.
                    print("  kernel messages during the run: none")
        running = report.get("verify_running")
        if running:
            verdict = running.get("matches_artifact")
            if verdict is True:
                print(f"  {running['path']}: matches {running['matched_artifact']}")
            elif verdict is False:
                print(
                    f"  {running['path']}: DIFFERS from every deployed artifact "
                    "(the device is not running what was just deployed)",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  {running['path']}: could not be compared - "
                    f"{running.get('note') or 'no usable digest on either side'}",
                    file=sys.stderr,
                )
        if report.get("error"):
            print(f"device-console: {report['error']}", file=sys.stderr)
        if left_behind:
            print(f"partial files left on the device; remove with: {report['cleanup_command']}", file=sys.stderr)

    return worst_exit


def add_ssh_connection_arguments(parser: argparse.ArgumentParser, required: bool = True) -> None:
    parser.add_argument("--host", required=required, help="Hostname or IP address, without user@")
    parser.add_argument("--user", help="SSH user")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--identity", help="Private-key path")
    parser.add_argument("--known-hosts", help="Alternate known_hosts file")
    parser.add_argument("--host-key-policy", choices=("strict", "accept-new"), default="strict")
    parser.add_argument("--connect-timeout", type=int, default=10)
    parser.add_argument(
        "--device-jump",
        metavar="[USER@]HOST[:PORT]",
        help=(
            "Reach the device through this host (ssh ProxyJump). The jump hop uses the local "
            "ssh agent and ssh_config; --identity applies to the device connection only."
        ),
    )


def add_build_ssh_arguments(parser: argparse.ArgumentParser) -> None:
    """Options for the build environment, mirroring the device endpoint defaults."""
    parser.add_argument(
        "--build-host",
        help="Build-environment host; omit to use this machine for read-only helpers",
    )
    parser.add_argument("--build-user", help="SSH user on the build host")
    parser.add_argument("--build-port", type=int, default=22)
    parser.add_argument("--build-identity", help="Private-key path for the build host")
    parser.add_argument("--build-known-hosts", help="Alternate known_hosts file for the build host")
    parser.add_argument("--build-host-key-policy", choices=("strict", "accept-new"), default="strict")
    parser.add_argument("--build-connect-timeout", type=int, default=10)


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


def add_max_output_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-output",
        type=int,
        default=MAX_CAPTURED_CHARS,
        metavar="CHARS",
        help=(
            "Stop collecting a command's output after this many characters and mark it "
            f"truncated (default: {MAX_CAPTURED_CHARS}; 0 disables the cap)"
        ),
    )


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
    add_max_output_argument(ssh_run)
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
    add_max_output_argument(serial_monitor)
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
    serial_run.add_argument(
        "--mode",
        choices=("raw", "posix-shell"),
        default="posix-shell",
        help=(
            "posix-shell wraps each command so a real exit code can be reported (default); "
            "raw sends the command verbatim for shells without sh, and then exit codes are unverified"
        ),
    )
    serial_run.add_argument("--continue-on-error", action="store_true")
    serial_run.add_argument("--json", action="store_true")
    add_output_guard_arguments(serial_run)
    add_max_output_argument(serial_run)
    serial_run.set_defaults(func=command_serial_run)

    build_run = subparsers.add_parser(
        "build-run", help="Run one or more commands on the build environment over OpenSSH"
    )
    add_build_ssh_arguments(build_run)
    build_run.add_argument("--command", action="append", required=True, help="Build-side command; repeat as needed")
    build_run.add_argument("--timeout", type=float, default=30, help="Per-command timeout in seconds")
    build_run.add_argument("--encoding", default="utf-8")
    build_run.add_argument("--continue-on-error", action="store_true")
    build_run.add_argument("--redact-env", action="append", default=[], metavar="NAME")
    build_run.add_argument("--json", action="store_true")
    add_output_guard_arguments(build_run)
    add_max_output_argument(build_run)
    build_run.set_defaults(func=command_build_run)

    inspect = subparsers.add_parser(
        "inspect", help="Describe a build artifact without touching the device"
    )
    add_build_ssh_arguments(inspect)
    add_ssh_connection_arguments(inspect, required=False)
    inspect.add_argument("--artifact", required=True, help="Artifact path on the build side")
    inspect.add_argument("--timeout", type=float, default=30)
    inspect.add_argument("--json", action="store_true")
    add_output_guard_arguments(inspect)
    inspect.set_defaults(func=command_inspect)

    verify = subparsers.add_parser(
        "verify", help="Compare a build artifact with a file on the device"
    )
    add_ssh_connection_arguments(verify)
    add_build_ssh_arguments(verify)
    verify.add_argument("--build-path", required=True, help="Path on the build side")
    verify.add_argument("--device-path", required=True, help="Path on the device")
    verify.add_argument("--timeout", type=float, default=30)
    verify.add_argument("--json", action="store_true")
    add_output_guard_arguments(verify)
    verify.set_defaults(func=command_verify)

    symbolize = subparsers.add_parser(
        "symbolize", help="Turn device-reported addresses into source locations"
    )
    add_build_ssh_arguments(symbolize)
    symbolize.add_argument("--binary", required=True, help="ELF with debug info, on the build side")
    symbolize.add_argument("--toolchain", help="Cross-toolchain prefix, such as aarch64-linux-gnu-")
    symbolize.add_argument("--address", action="append", default=[], help="Address; repeat as needed")
    symbolize.add_argument(
        "--addresses-from", help="Log file on this machine to extract addresses from"
    )
    symbolize.add_argument("--max-addresses", type=int, default=64)
    symbolize.add_argument("--kaslr-offset", metavar="HEX", help="Offset subtracted from each address")
    symbolize.add_argument("--timeout", type=float, default=60)
    symbolize.add_argument("--json", action="store_true")
    add_output_guard_arguments(symbolize)
    symbolize.set_defaults(func=command_symbolize)

    deploy = subparsers.add_parser(
        "deploy", help="Copy a build artifact to the device, verify it, and optionally run it"
    )
    add_ssh_connection_arguments(deploy)
    add_build_ssh_arguments(deploy)
    deploy.add_argument(
        "--artifact",
        action="append",
        required=True,
        metavar="SRC[:DST]",
        help="Artifact on the build side, optionally renamed; repeat as needed",
    )
    deploy.add_argument("--dest", default="/tmp", help="Destination directory on the device")
    deploy.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to the device. Without it the command only prints the plan",
    )
    deploy.add_argument("--run", help="Command to run on the device once the artifact is verified")
    deploy.add_argument("--chmod", metavar="MODE", help="chmod the deployed file after verification")
    deploy.add_argument(
        "--verify-running", metavar="PATH", help="Also compare this device path with the artifact"
    )
    deploy.add_argument(
        "--capture-dmesg", action="store_true", help="Report kernel messages added while --run executes"
    )
    deploy.add_argument("--stage-dir", help="Local staging directory (default: a temporary directory)")
    deploy.add_argument("--keep-stage", action="store_true", help="Keep the staging directory")
    deploy.add_argument(
        "--max-transfer-bytes",
        type=int,
        default=DEFAULT_MAX_TRANSFER_BYTES,
        metavar="BYTES",
        help=f"Refuse artifacts larger than this (default: {DEFAULT_MAX_TRANSFER_BYTES}; 0 disables)",
    )
    deploy.add_argument("--transfer-timeout", type=float, default=DEFAULT_TRANSFER_TIMEOUT)
    deploy.add_argument(
        "--unsafe-dest",
        action="store_true",
        help="Allow destinations such as /boot or /etc that can stop the device booting",
    )
    deploy.add_argument(
        "--allow-unverified",
        action="store_true",
        help="Continue when the device cannot hash the file, so the transfer stays unverified",
    )
    deploy.add_argument("--timeout", type=float, default=30, help="Per-command timeout in seconds")
    deploy.add_argument("--redact-env", action="append", default=[], metavar="NAME")
    deploy.add_argument("--json", action="store_true")
    add_output_guard_arguments(deploy)
    add_max_output_argument(deploy)
    deploy.set_defaults(func=command_deploy)

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
        "max_output",
        "transfer_timeout",
        "max_transfer_bytes",
        "max_addresses",
        "build_connect_timeout",
    ):
        if hasattr(args, field) and getattr(args, field) < 0:
            raise ToolError(f"--{field.replace('_', '-')} cannot be negative")
    if hasattr(args, "port") and isinstance(args.port, int) and not 1 <= args.port <= 65535:
        if args.subcommand in ("ssh-run", "ssh-shell", "deploy", "verify", "build-run", "inspect"):
            raise ToolError("SSH --port must be between 1 and 65535")
        raise ToolError(f"{args.subcommand} --port must be between 1 and 65535")
    if hasattr(args, "build_port") and not 1 <= args.build_port <= 65535:
        raise ToolError("--build-port must be between 1 and 65535")
    if hasattr(args, "baud") and args.baud <= 0:
        raise ToolError("--baud must be positive")
    if hasattr(args, "wake") and args.wake < 0:
        raise ToolError("--wake cannot be negative")
    if hasattr(args, "connect_timeout") and args.connect_timeout == 0:
        raise ToolError("--connect-timeout must be positive")
    if hasattr(args, "build_connect_timeout") and args.build_connect_timeout == 0:
        raise ToolError("--build-connect-timeout must be positive")
    if (
        args.subcommand in ("ssh-run", "serial-run", "deploy", "verify", "build-run", "symbolize", "inspect")
        and args.timeout == 0
    ):
        raise ToolError("--timeout must be positive")
    if getattr(args, "chmod", None) and not re.fullmatch(r"[0-7]{3,4}", args.chmod):
        raise ToolError(f"--chmod must be an octal mode such as 0755, not {args.chmod!r}")
    if hasattr(args, "transfer_timeout") and args.transfer_timeout == 0:
        raise ToolError("--transfer-timeout must be positive")
    if hasattr(args, "login_timeout") and args.login_timeout == 0:
        raise ToolError("--login-timeout must be positive")
    if hasattr(args, "login_budget") and args.login_budget == 0:
        raise ToolError("--login-budget must be positive")
    if getattr(args, "kaslr_offset", None) and not re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]+", args.kaslr_offset):
        raise ToolError(f"--kaslr-offset must be hexadecimal: {args.kaslr_offset!r}")
    if getattr(args, "device_jump", None):
        validate_jump_spec(args.device_jump)
    if args.subcommand == "deploy":
        # Refuse a dangerous destination before any process starts.
        validate_destination(args.dest, args.unsafe_dest)
        if args.capture_dmesg and not args.run:
            # Otherwise the plan promises a capture that nothing would perform.
            raise ToolError("--capture-dmesg captures kernel messages around --run, so it requires --run")
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
