# SATA 故障分析报告

> 本报告由 `examples/sample-ata-timeout-eh.log` 样例日志生成，作为输出格式的参考示例。
> 本次分析未提供内核源码目录，结论基于日志内容与 libata 通用机制（主线 5.15 行为）。

## 1. 分析结论

1. **故障现象**：`ata2.00`（对应 `sdb`，Seagate ST4000DM000）在正常读写期间反复出现 NCQ 写/读命令超时（`Emask 0x4 (timeout)`），共发生 3 个故障周期。
2. **故障位置**：单一端口 `ata2`，同控制器其余三个端口（ata1/ata3/ata4）在日志范围内无异常，指向端口/链路/单盘方向，而非控制器整体故障。
3. **EH 触发**：3 个周期均触发了 libata EH，port 被冻结（frozen），执行 hard reset。
4. **设备恢复**：3 个周期均在 hard reset 后以 6.0 Gbps 重新训练成功，设备重新配置（`configured for UDMA/133`），`EH complete`，设备未被离线。
5. **最可能原因**：`ata2` 链路/连接环节间歇性中断（线缆、连接器、背板或供电瞬断），其次为该盘固件在 NCQ 负载下响应挂起。
6. **置信度**：中。日志中 `SError` 全零、reset 后全速率恢复，缺少直接指向信号完整性的寄存器证据，需补充 SMART 与交叉验证确认。

## 2. 环境与设备信息

| 项目 | 值 | 来源 |
|---|---|---|
| 内核版本 | 5.15.0-91-generic (Ubuntu) | `Linux version` 行 |
| 操作系统版本 | Ubuntu 22.04（gcc 11.4.0 构建信息推断） | `Linux version` 行 |
| SATA 控制器 / HBA | Intel AHCI（0000:00:17.0），AHCI 1.3.1，32 slots / 4 ports / 6 Gbps | `ahci 0000:00:17.0` 行 |
| ATA port | ata1~ata4，故障端口为 ata2 | `ataN: SATA max UDMA/133` 行 |
| ataX ↔ hostX ↔ sdX 映射 | ata1→host0→sda；ata2→host1→sdb（故障盘）；ata3→host2→sdc；ata4→host3→sdd | `scsi hostN: ahci` / `scsi N:0:0:0` 行 |
| 故障盘型号/固件 | ST4000DM000-1F2168 / CC54，4TB | `ata2.00: ATA-10:` 行 |
| 序列号 | 日志中未提供 | — |
| 协商速率 | 启动及每次恢复后均为 6.0 Gbps（SStatus 133） | `SATA link up` 行 |
| SATA PM / PMP | 未使用（日志无 PMP 行，设备号均为 .00） | 全日志 |
| 故障发生时间 | 启动后约 8642s、12901s、17355s 三次，每次 EH 约 0.3s | 相对时间戳（无绝对时间，日志未含 `-T` 格式） |

## 3. 故障时间线

日志为相对时间戳（开机秒数），无法换算绝对时间。

| 时间 | 日志事件 | 含义 | 影响 |
|---|---|---|---|
| 1.2~1.5s | ahci 初始化，4 端口全部 6.0G 识别成功 | 系统启动正常 | 基线 |
| 8642.1s | 故障周期 1：ata2.00 两个 WRITE FPDMA QUEUED（tag 0/1）超时，port frozen，hard reset 后 6.0G 恢复，EH complete | 命令在飞期间无响应 | I/O 停顿约 0.3s，命令重试 |
| 12901.5s | 故障周期 2：单个 WRITE FPDMA QUEUED（tag 0）超时，同样流程恢复 | 同周期 1 | 同上 |
| 17355.9s | 故障周期 3：两个 WRITE + 一个 READ FPDMA QUEUED（tag 0/1/2）超时，同样流程恢复 | 超时命令数增多，读写均出现 | 同上；故障未收敛 |

周期差异：三次均为纯超时（`Emask 0x4`）、`SError` 均为 0、reset 均一次成功且未降速、未禁用 NCQ；故障间隔约 4200~4400s，未呈恶化趋势，但周期 3 超时命令数增多，需继续观察。

## 4. 关键日志解析

> 原文：
> ```
> [ 8642.118734] ata2.00: exception Emask 0x0 SAct 0x3 SErr 0x0 action 0x6 frozen
> ```
> 解析：libata 在打印本行前已将 ata2 冻结（`frozen`）。`SAct 0x3` 表示 tag 0 和 tag 1 两条 NCQ 命令在飞；`SError 0x0` 表示 AHCI 未在端口错误寄存器中记录到 PHY/链路错误——链路层"看起来"是干净的，命令是无声丢失而非被链路层报错打断。`action 0x6` 为 EH 计划动作（reset + 重验证类，具体位定义以 5.15 源码为准）。

> 原文：
> ```
> [ 8642.118762] ata2.00: cmd 61/08:00:00:08:40/00:00:3a:00:00/40 tag 0 ncq dma 4096 out
>                         res 40/00:00:00:00:00/00:00:00:00:00/00 Emask 0x4 (timeout)
> [ 8642.118773] ata2.00: status: { DRDY }
> ```
> 解析：`cmd 61` = WRITE FPDMA QUEUED（NCQ 写）。`Emask 0x4` = AC_ERR_TIMEOUT，命令超过默认超时（30s）未完成。`res 40/00` + `status: { DRDY }`：EH 读回 taskfile 时设备处于就绪态、无错误位——设备并没有报错，只是完成中断没有回来。这是"命令发出后石沉大海"的典型形态，区别于设备主动报错（那种会是 `res 51/04`、ABRT）。

> 原文：
> ```
> [ 8642.119015] ata2: hard resetting link
> [ 8642.430112] ata2: SATA link up 6.0 Gbps (SStatus 133 SControl 300)
> [ 8642.431508] ata2.00: configured for UDMA/133
> [ 8642.431521] ata2: EH complete
> ```
> 解析：EH 执行硬复位（COMRESET 路径），约 0.31s 后链路以全速 6.0 Gbps 重建（SStatus 133：DET=3 链路建立、SPD=3 Gen3、IPM=1 Active），设备重新识别配置成功，本轮 EH 结束。注意 `EH complete` 只表示恢复动作执行完毕，三次复发证明问题未根治。

## 5. libata EH 流程分析

- **EH 触发原因**：NCQ 命令（WRITE/READ FPDMA QUEUED）超时，走 `ata_qc_timeout` 超时路径（非错误中断路径，与 SError=0 一致）。
- **超时命令**：周期 1 为 tag 0/1 两条写；周期 2 为 tag 0 一条写；周期 3 为 tag 0/1 写 + tag 2 读。
- **port 冻结**：是，三个周期均为 `frozen`（`ata_port_freeze`）。
- **EH action**：`action 0x6`（reset + 重验证；位定义以源码为准）。
- **soft reset**：日志未出现 softreset 打印，AHCI 常规路径直接进入 hard reset（具体序列以 5.15 `libahci.c`/`libata-eh.c` 为准）。
- **hard reset**：执行，一次成功，无 `COMRESET failed`。
- **PMP reset**：不适用（无 PMP）。
- **重新识别 / 重新验证**：是，`configured for UDMA/133` 表明 `ata_dev_revalidate` 成功；容量参数无变化。
- **降速**：未发生（无 `limiting SATA link speed`）。
- **NCQ 处理**：未禁用（恢复后仍应走 FPDMA；日志未出现 NCQ disabled）。
- **命令重试**：失败的 qc 由 EH 重新下发（无上层 I/O error 打印，说明重试成功）。
- **最终状态**：设备恢复在线；故障以约 70 分钟周期反复。

## 6. 内核源码分析

本次未提供内核源码目录，本章不适用。若后续提供 5.15.y 源码，建议重点核对：`drivers/ata/libata-eh.c` 中 exception 行打印与 `action 0x6` 位解析、超时判定路径；`drivers/ata/libahci.c` 中 `ahci_hardreset` 的等待与重试逻辑。

## 7. 根因推测

| 优先级 | 可能原因 | 支持证据 | 反向证据 | 置信度 |
|---|---|---|---|---|
| P1 | ata2 链路/连接环节间歇性中断（线缆、连接器、背板触点或该端口供电瞬断），命令在飞期间链路瞬断导致完成中断丢失 | 仅单端口故障；纯 timeout 无设备报错；reset 即恢复；三个周期形态一致 | `SError` 全零，AHCI 未记录 PHY 层错误（但瞬断若短于硬件检测窗口可以不落 SError，不构成强反证） | 中 |
| P2 | 磁盘固件在 NCQ 负载下响应挂起（ST4000DM000/CC54） | 超时均为 FPDMA QUEUED；设备本身无错误寄存器报错 | 读命令也出现在周期 3；同型号固件的 sda 在同控制器上正常（固件版本相同但个体/负载不同，削弱但不排除） | 中 |
| P3 | NCQ 深度/队列交互问题（驱动或盘固件对深队列的处理缺陷） | 周期 3 三条命令同时在飞时超时 | 周期 2 仅单条命令在飞也超时，不支持深度相关 | 低 |

明确排除倾向：不支持"磁盘介质损坏"（无任何 UNC/IDNF、无 sense Medium Error、无重试后 I/O error）；不支持"控制器整体故障"（同 HBA 其余三端口正常）。

## 8. 排查与验证方案

### 8.1 立即执行（低风险检查）

1. **操作**：收集带绝对时间的完整日志与设备健康数据：
   ```bash
   dmesg -T > dmesg_full.txt
   journalctl -k --since "7 days ago" > journal_k.txt
   smartctl -a /dev/sdb > sdb_smart.txt
   smartctl -a /dev/sda > sda_smart.txt   # 同型号对照盘
   ```
   **预期结果**：sdb 的 SMART 中 UDMA_CRC_Error_Count、Command Timeout 计数、通电时间与启停次数。
   **下一步判断**：CRC 计数随故障增长 → 走 8.2 换线缆；计数为零且 SMART 整体健康 → 偏向固件/供电方向，走 8.3 降 NCQ 深度验证。

2. **操作**：查看链路错误与速率状态：
   ```bash
   cat /sys/class/ata_link/link2/sata_spd /sys/class/ata_link/link2/sata_spd_limit
   ls /sys/class/ata_port/ata2/
   ```
   **预期结果**：确认当前速率与限速值。
   **下一步判断**：若已被限速，说明发生过更严重的 reset 失败，与本日志不符，需重新核对日志完整性。

3. **操作**：对比 sda（同型号正常盘）与 sdb 的 `smartctl -x` 全量报告及故障期间负载（iostat 历史）。
   **预期结果**：两盘 SMART 差异、故障是否与特定负载相关。
   **下一步判断**：sdb SMART 异常增长 → 换盘验证；两者相当 → 链路/供电方向。

### 8.2 硬件交叉验证（需停机窗口，热插拔有掉盘风险，操作前确认业务可承受）

1. **操作**：更换 ata2 的 SATA 线缆（包括背板段）。
   **预期结果**：更换后故障周期消失。
   **下一步判断**：消失 → 线缆/连接器确认；依旧 → 下一步。

2. **操作**：对调 sdb 与 sdc 的端口（sdb 换到 ata3 槽位）。
   **预期结果**：观察故障跟随盘走还是留在 ata2 端口。
   **下一步判断**：跟随盘 → 盘本体/固件方向，升级固件或换盘；留在 ata2 → 端口/背板/主板方向，检查该端口供电与主板。

3. **操作**：临时将 ata2 限速到 3.0 Gbps（`libata.force=3.0Gbps` 内核参数，需重启，属干预性措施）。
   **预期结果**：降速后故障消失。
   **下一步判断**：消失 → 信号完整性坐实，长期方案为更换线缆/背板而非维持限速。

### 8.3 软件深入定位

1. **操作**：开启 libata/ahci 动态调试复现（需 CONFIG_DYNAMIC_DEBUG，低风险但日志量大）：
   ```bash
   echo 'module libata +p' > /sys/kernel/debug/dynamic_debug/control
   echo 'module ahci +p' > /sys/kernel/debug/dynamic_debug/control
   ```
   **预期结果**：下次故障时获得 qc 提交/完成细节与 AHCI 中断状态。
   **下一步判断**：确认完成中断是否到达主机——中断丢失 → 平台/中断方向；中断未来但链路无错 → 盘固件方向。

2. **操作**：将 sdb 的 NCQ 队列深度降为 1 验证（`echo 1 > /sys/block/sdb/device/queue_depth`，会改变复现条件）。
   **预期结果**：故障消失。
   **下一步判断**：消失 → NCQ 交互问题，查 CC54 固件更新；依旧 → 排除 P3。

3. **操作**：查 Seagate CC54 固件是否有针对该型号的更新。**风险：固件更新有变砖风险，需备份并走厂商工具。**
   **下一步判断**：有更新且 changelog 涉及 timeout/NCQ → 安排窗口更新。

## 9. 信息缺口

| 缺失信息 | 影响 | 获取方式 |
|---|---|---|
| 绝对时间戳（日志为相对秒数） | 无法与业务负载、供电事件对齐 | `dmesg -T` / `journalctl -k` |
| SMART 数据 | 无法区分盘本体与链路 | `smartctl -x /dev/sdb` |
| 磁盘序列号 | 无法追踪个体与固件批次 | `smartctl -i /dev/sdb` |
| 故障期间是否存在上层 I/O error | 判断重试是否最终失败 | `journalctl -k` 全文搜索 `I/O error` |
| 整机拓扑（背板/线缆走线、供电分区） | 影响硬件交叉验证设计 | 现场信息 |
| 内核源码 | 无法做函数级验证 | 用户提供 5.15.0-91 对应源码 |

## 10. 最终判断

- **已确认事实**：ata2/sdb 发生 3 次 NCQ 命令超时；每次 EH hard reset 后 6.0G 恢复；SError 全零；无介质错误、无降速、未离线；同控制器其余端口正常。
- **高概率判断**：故障位于 ata2 端口链路或 sdb 盘本体两个候选之内，控制器与驱动整体性问题可基本排除；设备当前可正常使用但故障会复发。
- **尚未验证的推测**：线缆/连接器接触不良（P1）与固件 NCQ 挂起（P2）之间的区分，依赖 SMART 计数与端口对调实验；在拿到数据前，不应判定"硬盘损坏"，也不应判定"线缆问题"。
