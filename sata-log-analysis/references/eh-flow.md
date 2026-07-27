# libata 错误恢复（EH）机制与调试参考

本文件描述 libata EH 的内部流程、关键函数、恢复级别升级路径和动态调试手段，供分析 EH 行为和给出软件层建议时使用。函数名和行为以主线内核为准；用户提供源码时，一切以用户源码中的实现为准，本文只作为定位地图。

## 目录

- 1. EH 触发路径
- 2. EH 主循环与恢复序列
- 3. Reset 级别与升级路径
- 4. 降速与 NCQ 处理
- 5. 设备重验证与离线
- 5.1 EH 恶化阶梯
- 6. SATA PMP 的 EH 特点
- 7. 常用调试手段
- 8. 常用 sysfs 节点

## 1. EH 触发路径

两条主要入口：

1. **命令超时**：命令在飞超过超时时间（默认 30 秒）→ `ata_qc_timeout()` 超时回调 → 冻结 port（`ata_port_freeze()`，置 ATA_PFLAG_FROZEN，中止在飞 qc）→ `ata_port_schedule_eh()` → 唤醒 EH 内核线程。日志对应 `exception Emask ... frozen`。
2. **错误中断**：AHCI 控制器上报错误中断（is / PxIS 中有错误位）→ 读取 PxSERR 得到 SError → `ata_port_freeze()` 或 `ata_port_abort()` → 调度 EH。SError 值即来源于此。

SCSI 层主动发起 EH 时走 `ata_scsi_error()`（`host_eh_scheduled` 打印与此相关），最终也汇入同一 EH 线程。

## 2. EH 主循环与恢复序列

核心函数链（drivers/ata/libata-eh.c）：

```
scsi_host EH 线程
  └─ ata_scsi_error()
       └─ ata_std_error_handler()      （各驱动 eh_strategy 指向，默认这里）
            └─ ata_do_eh()
                 ├─ ata_eh_autopsy()        分析失败 qc，归类错误（ac_err_mask）
                 ├─ ata_eh_report()         打印 "exception Emask ..." 等行
                 └─ ata_eh_recover()        执行恢复
                      ├─ ata_eh_reset()     按 action 执行 reset（见第 3 节）
                      ├─ ata_eh_qc_complete()/retry  完成或重试失败命令
                      └─ ata_eh_revalidate_and_attach()  重新识别/验证设备
```

分析时对照日志确认每一阶段的打印是否出现：`exception` 行（autopsy/report）→ `hard resetting link`（reset）→ `SATA link up`（训练成功）→ `configured for UDMA/133`（重验证成功）→ `EH complete`（收尾）。缺了哪一段，EH 就停在哪个阶段。

## 3. Reset 级别与升级路径

EH 按由轻到重的顺序尝试（具体由各驱动的 `->prereset`/`->softreset`/`->hardreset`/`->postreset` 操作函数决定）：

1. **prereset**：AHCI 下为 `ahci_prereset`→`sata_std_prereset`，等待 link ready，打印 `link is slow to respond` 即在此等待期间。
2. **softreset**：`sata_link_softreset()`/`ata_bus_softreset()`，向设备发 SRST。失败打印 `softreset failed (device not ready)` 或 `(1st FIS failed)`。
3. **hardreset**：`sata_link_hardreset()`，发起 COMRESET。失败打印 `COMRESET failed (errno=%d)`。AHCI 下为 `ahci_hardreset()`。

升级逻辑：softreset 失败且链路状态异常 → 置 ATA_EH_RESET 相关标志走 hardreset；hardreset 反复失败 → 降速重试（第 4 节）→ 仍失败则 `ata_eh_detach()`/设备离线。

errno 常见值：`-5` = -EIO，`-32` = -EPIPE（链路未建立），`-16` = -EBUSY。errno 含义随版本和路径不同，引用时核对源码。

## 4. 降速与 NCQ 处理

- **降速**：reset 连续失败后，`sata_down_spd_limit()`/`ata_eh_speed_down()` 降低链路速率上限（6.0→3.0→1.5 Gbps），打印 `limiting SATA link speed to X Gbps`。降速后 reset 成功并恢复 → 强证据指向信号完整性/链路质量问题。
- **NCQ**：反复 NCQ 命令失败时部分版本会置 ATA_DFLAG_NCQ_OFF 禁用 NCQ 重试；也有仅重试不关闭的实现。报告中必须按实际日志（是否出现 NCQ disabled 字样、后续是否仍走 FPDMA）描述，不要假设。

## 5. 设备重验证与离线

- reset 成功后走 `ata_dev_revalidate()`：重新读 IDENTIFY、容量、设置。失败打印 `revalidation failed (errno=%d)`。
- 多次 EH 仍无法恢复 → `ata_eh_detach()` / SCSI 层 `scsi_device_set_state(offline)`：用户看到 `sdX` 消失、DID_NO_CONNECT / DID_TARGET_FAILURE。
- `EH complete` 只表示本轮恢复动作执行完毕，不代表故障消除。恢复后同一 ataX 反复进入 EH 是"未根治"的关键证据。

## 5.1 EH 恶化阶梯

反复 EH 时，比较**每个周期**的恢复质量，恶化通常沿以下阶梯逐级出现：

```
误码/超时（reset 一次成功，~0.3s 恢复）
  → link is slow to respond（恢复耗时拉长到数秒）
    → softreset failed（软复位开始失败）
      → COMRESET failed（硬复位也失败）
        → limiting SATA link speed（降速重试）
          → revalidation failed / device offline（放弃设备）
```

分析动作：对每个周期记录"reset 类型、失败打印、恢复耗时（从 `hard resetting link` 到 `EH complete` 的间隔）、是否降速"。恢复耗时变长、出现更高阶梯的打印，都说明故障在进行性恶化——这比"发生了 N 次 EH"本身更有诊断价值。反之，多个周期形态完全一致（同型 EH），说明故障稳定但未根治。

## 6. SATA PMP 的 EH 特点

- PMP 设备本身表现为 `ataX.15`（如 `ata6.15: Port Multiplier ... 15 ports`），子设备为 `ataX.00`~`ataX.14`。
- 子端口故障的 EH 只 reset 该子端口对应的链路；PMP 主链路故障（`ataX` 本身 link down）会影响全部子端口。
- 日志中 `PMP` reset 字样（pmp link hardreset / `sata_pmp` 相关打印）表明 EH 在恢复主链路。
- 判别关键：同一 ataX 下是否所有 ataX.Y 同时异常。全部异常 → 主链路/PMP 芯片；单一异常 → 该子端口链路或该盘。注意排查"单盘故障拖累整条链路"的 PMP 实现缺陷场景：症状是单盘介质错误伴随全端口超时，需要单盘直连绕过 PMP 来区分。

## 7. 常用调试手段

给用户建议时使用（均为低风险，需 root）：

```bash
# 1) libata 动态调试（需内核开启 CONFIG_DYNAMIC_DEBUG）
echo 'module libata +p' > /sys/kernel/debug/dynamic_debug/control
echo 'module ahci +p'   > /sys/kernel/debug/dynamic_debug/control

# 2) 提高 libata 打印级别（部分版本支持）
echo 8 > /proc/sys/dev/scsi/logging_level   # SCSI 层日志级别（谨慎，量大）

# 3) tracepoint（需挂载 tracefs）
cd /sys/kernel/tracing 2>/dev/null || cd /sys/kernel/debug/tracing
echo 1 > events/libata/enable        # 内核较新版本提供 libata trace events
echo 1 > events/scsi/enable

# 4) 查看超时参数（只读检查）
cat /sys/block/sdX/device/timeout
```

修改超时、关 NCQ（`libata.force=noncq` 内核参数或 `echo 1 > /sys/block/sdX/device/queue_depth`）、降速（`libata.force=1.5Gbps`）属于会改变故障行为的干预手段，建议时注明这会改变复现条件，且修改内核参数需重启。

## 8. 常用 sysfs 节点

```bash
ls /sys/class/ata_port/          # ataX：port 级状态
ls /sys/class/ata_link/          # linkX：链路协商速率、电源状态
cat /sys/class/ata_link/linkX/sata_spd        # 当前速率
cat /sys/class/ata_link/linkX/sata_spd_limit  # 限速值
cat /sys/class/ata_link/linkX/hw_sata_spd_limit
ls /sys/class/ata_device/        # devX.Y：设备（含 PMP 子设备）
cat /sys/class/scsi_host/hostX/link_power_management_policy
ls /sys/class/scsi_disk/
```
