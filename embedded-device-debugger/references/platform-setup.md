# Platform setup

Read this file only when `doctor` reports a missing dependency or the connection fails at the local machine boundary.

## Common requirements

- Python 3.9 or newer.
- An OpenSSH client reachable as `ssh` for SSH operations.
- PySerial for serial operations. From the skill directory, install it only with the user's approval:

  `python -m pip install -r requirements.txt`

Use `python3` instead of `python` on systems where that is the Python 3 launcher. A virtual environment is preferred when the user has not chosen a Python environment.

## Windows

- Check OpenSSH with `Get-Command ssh` or the tool's `doctor` command. If absent, enable the Windows OpenSSH Client optional feature using the user's normal administration process.
- Serial ports normally look like `COM3`. For port numbers above 9, continue to pass the ordinary `COM10` form to PySerial.
- Close PuTTY, Tera Term, vendor flash tools, and other applications that may already own the port.
- For an interactive session, prefer the tool's `--ask-password` option in a PTY. For automation, populate the `--password-env` variable through the user's existing secret-management method and remove it after the session. Do not type a password into a command or persist it in shell history.

## Linux

- Serial ports commonly appear as `/dev/ttyUSB0`, `/dev/ttyACM0`, or stable links under `/dev/serial/by-id/`. Prefer `/dev/serial/by-id/` when available.
- If opening the port returns permission denied, inspect `ls -l <port>` and `id`. Do not run the tool as root by default. Adding the user to `dialout`, `uucp`, or a device-specific group changes local permissions and requires the user's approval; it may also require a new login session.
- Stop or reconfigure services such as ModemManager only when evidence shows they are taking the target port and the user authorizes the service change.
- Read a password without placing it in history:

  `read -rsp 'Device password: ' DEVICE_PASSWORD; export DEVICE_PASSWORD; echo`

  `unset DEVICE_PASSWORD`

  For a user-attended session, `--ask-password` in a PTY avoids the environment variable entirely.

## Connection failures

- `Host key verification failed`: inspect the existing `known_hosts` entry and confirm whether the device was reimaged or replaced. Never delete a host key merely to make the warning disappear.
- SSH exit code `255`: distinguish name resolution, routing, TCP refusal, authentication, and host-key errors using stderr.
- Serial `access denied` or `resource busy`: identify the owning application or service; do not repeatedly reopen the port.
- No serial output: verify voltage/level shifter, TX/RX crossover, common ground, baud, parity, flow control, and whether the console is output-only. Do not change wiring while powered unless the hardware procedure permits it.
- Garbled serial output usually indicates an incorrect baud/clock or electrical-level problem. Preserve a short raw sample before changing settings.
