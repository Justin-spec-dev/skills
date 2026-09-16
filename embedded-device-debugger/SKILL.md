---
name: embedded-device-debugger
description: Safely diagnose embedded devices through SSH or serial consoles on Windows or Linux, and move build artifacts onto them for testing. Use when Codex needs to inspect a device, run bounded diagnostic commands, capture logs, correlate evidence, guide connection setup, describe a cross-compiled artifact, turn a device-reported address into a source line, or compare a file on the device with a build output. Copying an artifact onto a device only ever happens after the plan has been shown and the user has authorized it; do not use for flashing or otherwise changing a device unless the user explicitly authorizes that action.
---

# Embedded Device Debugger

Use `scripts/device_console.py` as the deterministic connection helper. It uses the operating system's OpenSSH client for SSH and PySerial for serial access.

## Operating workflow

1. Establish the exact target and transport. Reuse connection details already supplied by the user. Ask only for missing values that prevent connection, such as host/user, serial port/baud, or a non-default prompt regex.
2. Run `doctor` before the first connection on a machine. It lists any missing prerequisite in `missing` with a `next_step`, and exits `2` when something is missing. If a prerequisite is missing, read [references/platform-setup.md](references/platform-setup.md). Do not install software or change serial-port permissions without authorization.
3. Start with a short, read-only evidence capture. Identify the OS/shell before selecting commands. Read [references/diagnostic-playbooks.md](references/diagnostic-playbooks.md) only for the relevant symptom area.
4. Form a specific hypothesis, run the smallest discriminating check, and preserve command output. Prefer several focused commands over a large opaque collection script.
5. When the work involves a build artifact rather than the device alone, keep the two machines distinct: `inspect` describes the artifact on the build side, `deploy` copies it to the device, `verify` confirms what is actually on the device, and `symbolize` turns device-reported addresses back into source lines. Run `deploy` without `--apply` first and show the plan. Read [references/build-and-deploy.md](references/build-and-deploy.md) before the first deploy to a device.
6. Report the observed evidence, likely cause, confidence, and next safe check. Distinguish device output from inference.

## Exit codes

Do not treat "the command printed something" as success. The tool signals outcomes through its exit status:

| Code | Meaning |
| --- | --- |
| `0` | Every command ran and every observed exit code was `0` |
| `1` | A command ran but returned a non-zero exit code; for `deploy` a transfer or hash check failed and the destination was left untouched, or `--verify-running` found a file that is not the artifact; for `verify` the two files differ; for `inspect` an architecture mismatch |
| `2` | Usage error, invalid arguments, or a refused safety guard (unset/too-short `--redact-env`, existing `--output` without `--force`, `--json` with `--append`, a `--dest` under `/sys`, `/proc`, `/dev`, or a risky path without `--unsafe-dest`) |
| `124` | A command timed out; an SSH timeout also sets `remote_may_still_run: true` on that result |
| `125` | A result could not be verified: `serial-run --mode posix-shell` never saw the wrapper's exit marker (`exit_code_known: false`); `deploy` found no usable digest on either side, so the transfer stays unverified and is not activated; `verify` could not compare the files at all; `--verify-running` could not answer whether the device path is the artifact |
| `130` | The user interrupted the run |

`doctor` exits `2` when a prerequisite is missing. `ssh-shell` passes the interactive session's status through and has no fixed contract. `deploy` without `--apply` exits `0` with `status: "planned"` and writes nothing.

## Tool usage

Resolve this skill directory from the loaded `SKILL.md`, then invoke the script with an available Python 3 executable:

```text
python <skill-dir>/scripts/device_console.py doctor --json
python <skill-dir>/scripts/device_console.py ssh-run --host 192.0.2.10 --user root --command "uname -a" --command "uptime"
python <skill-dir>/scripts/device_console.py serial-ports --json
python <skill-dir>/scripts/device_console.py serial-monitor --port COM5 --baud 115200 --duration 20 --output boot.log
python <skill-dir>/scripts/device_console.py serial-run --port /dev/ttyUSB0 --baud 115200 --username root --password-env DEVICE_PASSWORD --command "uname -a"
python <skill-dir>/scripts/device_console.py inspect --build-host build --artifact /srv/app/bin/app --host 192.0.2.10 --json
python <skill-dir>/scripts/device_console.py deploy --build-host build --host 192.0.2.10 --user root --artifact /srv/app/bin/app --dest /tmp
python <skill-dir>/scripts/device_console.py verify --build-host build --host 192.0.2.10 --build-path /srv/app/bin/app --device-path /usr/bin/app
python <skill-dir>/scripts/device_console.py symbolize --build-host build --binary /srv/app/bin/app --toolchain aarch64-linux-gnu- --addresses-from oops.log
```

Use `python3` where `python` is not the Python 3 launcher. Run `--help` on a command before guessing an option.

- `ssh-run` is non-interactive and expects a key or SSH agent. Use `ssh-shell` in a PTY when the user must complete an interactive password or MFA prompt.
- SSH host-key checking is strict by default. Use `--host-key-policy accept-new` only after the user identifies this as a first connection and the expected fingerprint has been verified out of band. Never disable host-key verification.
- Supply serial login passwords through `--password-env`, or use `--ask-password` in a PTY so the user can type it without echo. Never put a password in a command argument, chat message, transcript, or saved artifact. Use repeatable `--redact-env NAME` options with `ssh-run`, `serial-monitor`, or `serial-run` for other known secrets. A selected variable that is unset, empty, or shorter than 4 characters is rejected before connecting, and values containing CR or LF are rejected for every transport because output is emitted line by line.
- Use bounded `--duration` and `--timeout` values for AI-driven work. `serial-run` additionally bounds the whole login exchange with `--login-budget` (default 45s). Do not leave monitors or shells running after collecting the needed evidence.
- `serial-run` defaults to `--mode posix-shell`, which wraps each command in `sh -c` so a real exit code can be reported. Use `--mode raw` only on consoles without `sh`; raw results have no verified exit code.
- Check exit codes, not just output. `ssh-run` reports the remote exit code and, on timeout, sets `remote_may_still_run: true` because killing the local client does not stop the remote command. In `serial-run --mode posix-shell` each result carries `exit_code_known`; when the wrapper's exit marker never arrives the process exits `125` and the result is explicitly unverified rather than reported as success.
- Output is capped. `--max-output CHARS` (default 1048576, `0` disables) stops collection once a command or monitor exceeds the cap and sets `output_truncated: true`. Reader buffers are bounded too, and bytes discarded from a flooded buffer are reported as `dropped_tail_bytes`, so a flooding command cannot hide its own truncation.
- The serial helper defaults DTR and RTS to `off`. Opening a serial port can still affect hardware with reset/boot wiring; warn the user before connecting when those lines are known to be sensitive.
- The default serial prompt matcher recognizes common uncolored shell, BusyBox, network-device, and bootloader prompts, then pins the first observed prompt for the rest of the session. Supply a narrow `--prompt` regex for colored, dynamic, or unusual prompts.
- Save evidence with `--output` when analysis spans multiple checks. The tool refuses to overwrite an existing file unless `--force` is supplied, and `--append` extends an existing file instead. Add `--json` to `ssh-run`, `serial-monitor`, or `serial-run` for a machine-readable report; `--json` cannot be combined with `--append` (appending writes JSON Lines, one document per line). `serial-run` writes captured partial evidence plus an `error` field if the session fails. A read failure includes the in-flight data as `error.partial_output` when available.
- A device that is only routable from the build host is reached with `--device-jump [user@]host[:port]`, which adds an ssh `ProxyJump` to the device connection. The jump hop authenticates with the local agent and `ssh_config`; `--identity` applies to the device hop only. `--device-jump` says how the agent reaches the device and is unrelated to where the artifact lives.
- `deploy` moves the artifact itself: it tries scp first and falls back to a `cat` pipe, because an embedded sshd often has no sftp subsystem and no scp protocol. The relayed scp rung is skipped when one argv cannot serve both hosts correctly (a jump, an identity, a custom port or known_hosts), and the report says which reason applied. The device destination is only replaced after its hash matches; until then the artifact sits beside it as a `.device-console-*.part` file, which is reported rather than deleted if the transfer fails.
- `deploy --capture-dmesg` reports the kernel messages added while `--run` executed (it requires `--run`), or says plainly that `dmesg` could not be read. It does not capture serial output: to correlate serial evidence, run `serial-monitor` alongside and align the two by timestamp.
- If network, device-node, or sandbox access is denied, request the required execution permission instead of weakening the host or device security configuration.

## Authorization boundary

Read-only inspection is the default. Before running a command that changes device state, describe the exact command and consequence and obtain explicit authorization. This includes reboot/power actions, killing processes, changing services or configuration, remounting filesystems, writing under `/sys` or `/proc/sys`, package changes, firewall/network changes, firmware flashing, bootloader environment writes, storage erasure, and GPIO or actuator control.

Copying an artifact onto a device is a change to that device. `deploy` therefore writes nothing unless `--apply` is given: run it without `--apply` first, show the resulting plan and destination path to the user, and add `--apply` only once they have authorized that specific write. The same holds for `--run`, which executes the artifact on the device, and `--chmod`. Never add `--apply` because the plan looked correct, and never pick a destination under `/boot`, `/lib/modules`, or `/etc` on the user's behalf.

Never infer permission to make a change from permission to connect or diagnose. If a device appears to be safety-critical or controls physical equipment, limit work to passive observation until the user confirms a safe operating state.
