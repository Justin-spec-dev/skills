---
name: embedded-device-debugger
description: Safely diagnose embedded devices through SSH or serial consoles on Windows or Linux. Use when Codex needs to inspect a device, run bounded diagnostic commands, capture logs, correlate evidence, or guide connection setup; do not use for flashing or changing a device unless the user explicitly authorizes that action.
---

# Embedded Device Debugger

Use `scripts/device_console.py` as the deterministic connection helper. It uses the operating system's OpenSSH client for SSH and PySerial for serial access.

## Operating workflow

1. Establish the exact target and transport. Reuse connection details already supplied by the user. Ask only for missing values that prevent connection, such as host/user, serial port/baud, or a non-default prompt regex.
2. Run `doctor` before the first connection on a machine. If a prerequisite is missing, read [references/platform-setup.md](references/platform-setup.md). Do not install software or change serial-port permissions without authorization.
3. Start with a short, read-only evidence capture. Identify the OS/shell before selecting commands. Read [references/diagnostic-playbooks.md](references/diagnostic-playbooks.md) only for the relevant symptom area.
4. Form a specific hypothesis, run the smallest discriminating check, and preserve command output. Prefer several focused commands over a large opaque collection script.
5. Report the observed evidence, likely cause, confidence, and next safe check. Distinguish device output from inference.

## Tool usage

Resolve this skill directory from the loaded `SKILL.md`, then invoke the script with an available Python 3 executable:

```text
python <skill-dir>/scripts/device_console.py doctor --json
python <skill-dir>/scripts/device_console.py ssh-run --host 192.0.2.10 --user root --command "uname -a" --command "uptime"
python <skill-dir>/scripts/device_console.py serial-ports --json
python <skill-dir>/scripts/device_console.py serial-monitor --port COM5 --baud 115200 --duration 20 --output boot.log
python <skill-dir>/scripts/device_console.py serial-run --port /dev/ttyUSB0 --baud 115200 --username root --password-env DEVICE_PASSWORD --command "uname -a"
```

Use `python3` where `python` is not the Python 3 launcher. Run `--help` on a command before guessing an option.

- `ssh-run` is non-interactive and expects a key or SSH agent. Use `ssh-shell` in a PTY when the user must complete an interactive password or MFA prompt.
- SSH host-key checking is strict by default. Use `--host-key-policy accept-new` only after the user identifies this as a first connection and the expected fingerprint has been verified out of band. Never disable host-key verification.
- Supply serial login passwords through `--password-env`, or use `--ask-password` in a PTY so the user can type it without echo. Never put a password in a command argument, chat message, transcript, or saved artifact. Use `--redact-env` for other known secrets.
- Use bounded `--duration` and `--timeout` values for AI-driven work. Do not leave monitors or shells running after collecting the needed evidence.
- The serial helper defaults DTR and RTS to `off`. Opening a serial port can still affect hardware with reset/boot wiring; warn the user before connecting when those lines are known to be sensitive.
- Save evidence with `--output` when analysis spans multiple checks. The tool refuses to overwrite an existing file unless `--force` is explicitly supplied.
- If network, device-node, or sandbox access is denied, request the required execution permission instead of weakening the host or device security configuration.

## Authorization boundary

Read-only inspection is the default. Before running a command that changes device state, describe the exact command and consequence and obtain explicit authorization. This includes reboot/power actions, killing processes, changing services or configuration, remounting filesystems, writing under `/sys` or `/proc/sys`, package changes, firewall/network changes, firmware flashing, bootloader environment writes, storage erasure, and GPIO or actuator control.

Never infer permission to make a change from permission to connect or diagnose. If a device appears to be safety-critical or controls physical equipment, limit work to passive observation until the user confirms a safe operating state.
