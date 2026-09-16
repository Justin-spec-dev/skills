# Build-to-device workflow

Read this file before the first `deploy` to a device, or when a transfer fails, when the device and the build machine are different hosts, or when a device-reported address needs to become a source line.

## Topology

Three machines are involved and they are not the same thing:

- the **build environment**, where the compiler and the artifacts live;
- the **device**, where the artifact has to run;
- the **agent machine**, which runs `device_console.py` and holds the SSH credentials.

`--host`/`--user`/`--port` describe the device. `--build-host`/`--build-user`/`--build-port` describe the build environment. Omitting `--build-host` means the build environment *is* the agent machine, and only the built-in read-only helpers (`sha256sum`, `wc`, `file`, `readelf`, `size`, `addr2line`) may run there; `build-run`, which executes arbitrary commands, requires an explicit `--build-host`.

Reach a device that is only routable from an intermediate host with `--device-jump [user@]host[:port]`. That adds an ssh `ProxyJump` to every device-facing command, so scp, verification, `mv`, and `--run` all work normally from the agent. Two properties of ProxyJump are worth remembering: the jump hop authenticates with the local agent and `ssh_config` only, so `--identity` does not apply to it; and the jump host must satisfy the same `StrictHostKeyChecking` policy as the device, so a first connection may need the jump host's fingerprint verified out of band as well. Never relax host-key checking to get past that.

`--device-jump` answers "how does the agent reach the device". It is unrelated to "where do the artifact bytes live", which the transfer ladder answers.

## Artifact transfer

`deploy` tries these in order and records every attempt in the report:

1. **scp relayed through the agent** — `build:SRC -> device:PART` in one local scp process. No local copy, no staging. This rung is skipped whenever one argv cannot serve both hops correctly: with `--device-jump`, or with any custom `--identity`, `--known-hosts`, `--host-key-policy`, or non-default port on either endpoint. scp applies one option set to both operands, so using it there would offer the device's private key to the build host. The report records the reason under `direct_rung_skipped`.
2. **stage locally, then push** — scp from the build host into a local temporary directory, then scp to the device. Used when rung 1 fails.
3. **`cat` pipes** — `ssh build 'cat SRC' > local`, and `ssh device 'cat > PART' < local`. This needs only `cat` on each side, and is the rung that works on the many embedded targets whose sshd offers neither an sftp subsystem nor the scp protocol.

Each scp rung is tried with `-O` (legacy protocol) before the default, for the same reason: OpenSSH 9 and later use SFTP by default, which fails immediately against dropbear or BusyBox. Failures from every rung are preserved in `artifacts[].transfer.attempts`, so a wrong guess about the target is visible instead of being retried silently.

The payload never passes through the text pipeline. It is streamed between file handles, so a binary artifact cannot be corrupted by decoding, and only the transfer status is reported.

## Why the destination is replaced last

The artifact is written to `<dest>.device-console-<random>.part` in the same directory, hashed there, and only then renamed over the destination. Three things follow:

- the bytes at the destination are always bytes that verified, so an interrupted transfer cannot leave a truncated binary in place;
- the rename is atomic because it stays within one directory;
- replacing a binary that is currently executing does not fail with `ETXTBSY`, because the running inode is never written to.

If a transfer or the hash check fails, the partial file is left where it is and its exact path plus a ready-to-run cleanup command are reported. It is deliberately not deleted: a `.part` file is evidence of what actually arrived.

The build-side hash is recomputed immediately before the transfer rather than reused from the plan, because a rebuild between the dry run and the apply would otherwise make the planned hash stale.

## Verifying a transfer

The device is asked for a digest with `sha256sum`, falling back to `md5sum`. The artifact is hashed again in whichever algorithm the device used, so the comparison is always like for like, and `algorithm` is reported so a weaker digest is visible rather than implied.

When the device has no hash tool at all, the outcome is `status: "unverified"` with exit code `125`, and the artifact is **not** activated: an unverifiable transfer is not the same as a successful one. `--allow-unverified` activates it anyway and records `verified: null`. The same reasoning covers the other direction: if the *build* side cannot produce a digest in the algorithm the device used, that is an unknown too, never a mismatch.

`--verify-running PATH` compares a path on the device with the deployed artifacts — use it to answer "is the binary being executed the one I just built", because comparing the deployed file alone does not prove the running process uses it (a process may hold a deleted or replaced inode). It runs once for the whole deploy, reports which artifact matched, exits 1 only when a comparison was possible and failed, and exits 125 when neither side could supply a digest.

## The standard loop

```sh
# 1. What is this artifact, and can it run on that device?
python <skill-dir>/scripts/device_console.py inspect --build-host build --artifact /srv/app/bin/app --host BOARD --json

# 2. What exactly would be copied, and where?
python <skill-dir>/scripts/device_console.py deploy --build-host build --host BOARD --artifact /srv/app/bin/app --dest /tmp

# 3. Only after the plan has been shown and authorized:
python <skill-dir>/scripts/device_console.py deploy --build-host build --host BOARD --artifact /srv/app/bin/app \
    --dest /tmp --apply --chmod 0755 --run "/tmp/app --selftest" --capture-dmesg --json

# 4. Turn any address from the output into a source line.
python <skill-dir>/scripts/device_console.py symbolize --build-host build --binary /srv/app/bin/app \
    --toolchain aarch64-linux-gnu- --addresses-from oops.log --json
```

`--max-transfer-bytes` (default 256 MiB) is checked before anything moves, so a mistyped path that resolves to a rootfs image is refused rather than transferred.

## Looking at a device failure from the build side

The reverse direction matters as much as deploying: the device reports a failure, and the code that explains it lives on the build host, not on the machine running the agent. The chain is read-only throughout.

```sh
# 1. Establish that the running binary is the artifact this source tree produced.
python <skill-dir>/scripts/device_console.py verify --build-host build --host BOARD \
    --build-path /home/dev/proj/build/app --device-path /usr/bin/app

# 2. Collect the failure evidence from the device.
python <skill-dir>/scripts/device_console.py ssh-run --host BOARD --command "dmesg | tail -60" --json

# 3. Turn the reported addresses into file:line.
python <skill-dir>/scripts/device_console.py symbolize --build-host build --binary /home/dev/proj/build/vmlinux \
    --toolchain aarch64-linux-gnu- --addresses-from oops.log --json

# 4. Read the source the addresses point at. It is on the build host, so it is read
#    through build-run rather than with a local file read.
python <skill-dir>/scripts/device_console.py build-run --build-host build --command "sed -n '395,425p' /home/dev/proj/drivers/foo.c"
python <skill-dir>/scripts/device_console.py build-run --build-host build --command "grep -rn 'foo_dma_map' /home/dev/proj | head"
```

Step 1 is the one worth insisting on. If the device is running a different build than the tree being read, every symbol and every offset below it is a confident wrong answer, and the time lost to that is far greater than one hash comparison. Step 4 costs one round trip per command, so read a range rather than a whole tree, and use `--max-output` when a `grep` could return a lot.

`--addresses-from` takes a local file. When the user pastes a log into the conversation instead, write the pasted text to a temporary file first, or pass the addresses directly with repeated `--address`.

## Serial evidence

`--capture-dmesg` reports the kernel messages the kernel added while `--run` executed, which needs no extra channel. Serial output is different: capturing it while an SSH command runs would mean reading a second stream concurrently, which needs a thread and buys little over running two commands. Instead, run the existing monitor alongside and align the two by timestamp:

```sh
python <skill-dir>/scripts/device_console.py serial-monitor --port /dev/ttyUSB0 --baud 115200 --duration 60 --output run.log
```

Start the monitor before `deploy --apply`, and compare its timestamps with `captured_at`, `started_at`, and `duration_seconds` in the deploy report. A kernel panic that never reaches `dmesg` — because the box died before the buffer was readable — is exactly the case this pairing exists for.

`dmesg` is unavailable on a device booted with `kernel.dmesg_restrict=1` for a non-root user. That is reported as an unavailable capture, never as "no new kernel messages"; the two are different findings.

## Symbolization

Resolve the toolchain on the build host with `--toolchain PREFIX` (for example `aarch64-linux-gnu-`). The prefix is checked against the build host's `PATH`: if it is not there, the command degrades to plain `addr2line` and the note in the report says so, rather than failing with a bare "command not found".

Kernel addresses need the KASLR offset subtracted before they mean anything, unless the device boots with `nokaslr` or randomization is disabled. Pass `--kaslr-offset 0x...` when it is enabled; the report records whether an offset was applied, and states plainly that without one every kernel symbol is shifted by the unknown base. Userspace addresses from a PIE binary are a different problem: `addr2line` needs the load base too, and a wrong base produces confidently wrong line numbers. Confirm one known-good address before trusting a batch.

The `file:line` that comes back names a path **on the build host**. Read it there, with `build-run`, rather than expecting the same path to exist locally.

`--addresses-from` extracts candidate addresses from an oops or backtrace log. It is a heuristic, not an oops parser: it takes any 8–16 digit hexadecimal token, so check `addresses_truncated` and prefer explicit `--address` values when precision matters.

## Limits

- `deploy` copies files and runs one command. It does not flash storage, write bootloader environments, or manage services; those need explicit authorization and are outside this tool.
- There is no rollback. The `.part` scheme protects the destination from a failed transfer, but replacing a working binary with a verified-but-wrong artifact is still a change the user has to accept.
- The direct rung assumes the agent can reach both hosts. It never asks the build host to authenticate to the device on its own, so no credentials need to exist there.
- `inspect` costs one round trip per helper. On a slow link, use `--json` and read the report rather than re-running it repeatedly.

## Troubleshooting

| Symptom | Cause and next step |
| --- | --- |
| Every scp rung fails with "subsystem request failed" | The device sshd has no sftp subsystem. Move on to the `cat` rung; it needs only `cat` on the device. |
| `scp: unknown option -- O` | The local scp predates OpenSSH 9. Harmless: the next rung drops `-O`. |
| Transfer succeeds but the hash differs | The staging hop corrupted the bytes. Compare `staged_digest` with the build digest to localize it; check for a shell banner or CRLF translation on the way in. |
| `mv -f` fails with "Read-only file system" | The destination is on a read-only mount. Choose a writable path such as `/tmp`; do not remount read-write without authorization. |
| `chmod` succeeds but `--run` fails with "Permission denied" | The device may mount the directory `noexec`, or the interpreter named by the ELF is missing. Check with `inspect` and `mount`. |
| Device reports no hash tool | The transfer stays unverified and exits 125. Either accept it with `--allow-unverified`, or copy a `sha256sum` onto the device first. |
| Host key error naming a host you never typed | The jump host is not in the given `known_hosts`. Verify its fingerprint out of band; do not disable host-key checking. |
