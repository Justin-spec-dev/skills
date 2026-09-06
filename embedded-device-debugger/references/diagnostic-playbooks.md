# Diagnostic playbooks

Choose only the section relevant to the symptom. Commands are examples, not a batch that must always be run. Check tool availability first on BusyBox or other minimal systems.

## Establish identity and time

Capture these early so later evidence can be correlated:

```sh
uname -a
cat /etc/os-release 2>/dev/null
date -Ins 2>/dev/null || date
uptime
cat /proc/cmdline 2>/dev/null
```

Record the host-side capture time too. Treat device timestamps as unreliable until clock synchronization is confirmed.

## Boot, kernel, and driver failures

```sh
dmesg --color=never 2>/dev/null || dmesg
journalctl -b -p warning..alert --no-pager 2>/dev/null
systemctl --failed --no-pager 2>/dev/null
cat /proc/interrupts
cat /proc/iomem 2>/dev/null
```

On non-systemd systems, try `logread`, `/var/log/messages`, or the platform's ring-buffer tool. For boot failures, capture serial output from reset through the first stable prompt and keep bootloader, kernel, and userspace phases separate.

## CPU, memory, and hangs

```sh
cat /proc/loadavg
free -m 2>/dev/null || cat /proc/meminfo
ps -eo pid,ppid,stat,comm,%cpu,%mem,args --sort=-%cpu 2>/dev/null || ps w
cat /proc/pressure/cpu 2>/dev/null
cat /proc/pressure/memory 2>/dev/null
```

Prefer snapshots a few seconds apart over a single `top` screen. Look for D-state tasks, OOM messages, interrupt storms, and continuously growing memory rather than assuming high load has one cause.

## Storage and filesystem

```sh
df -hT 2>/dev/null || df -h
df -ih 2>/dev/null
mount
cat /proc/mounts
dmesg --color=never 2>/dev/null | tail -n 200
```

Do not run repair tools, remount read-write, or write test files during diagnosis without authorization. Check inode exhaustion as well as byte capacity.

## Network

```sh
ip -brief address 2>/dev/null || ifconfig -a
ip route 2>/dev/null || route -n
ip neigh 2>/dev/null || arp -an
ss -lntup 2>/dev/null || netstat -lntup 2>/dev/null
cat /etc/resolv.conf
```

Use a bounded ping only when the destination is known to permit it. Separate link state, address assignment, routing, DNS, firewall, and application listening checks.

## Service or application failure

```sh
systemctl status SERVICE --no-pager -l
journalctl -u SERVICE -b --no-pager -n 200
ps w
```

Replace `SERVICE` only with the exact unit identified by the user or process evidence. On minimal systems inspect the relevant init script and log path. Do not restart a service just to see whether the problem goes away before preserving failure evidence.

## Evidence quality

- Keep the exact command, stdout, stderr, exit status when available, and capture time.
- When output is large, first collect a bounded tail or a time window. Expand only if the missing interval matters.
- Search logs using symptom time, subsystem, error code, and device identifier. Do not rely on the word `error` alone.
- Confirm a proposed cause with at least one discriminating observation. State competing explanations when evidence is incomplete.
