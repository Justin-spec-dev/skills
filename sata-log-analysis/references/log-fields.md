# SATA/ATA 日志关键字段解码参考

本文件给出 libata/AHCI 日志中常见字段和短语的位级含义与触发路径。定义主要依据内核头文件 `include/linux/ata.h`、`include/linux/libata.h`、`include/scsi/scsi.h` 及 libata 各源文件。若用户提供的内核版本与本文定义存在出入，以用户源码为准。表中标注"不确定"的条目不要写进报告结论。

## 目录

- 1. exception 行整体格式
- 2. Emask（AC_ERR_* 错误位）
- 3. SError 寄存器位
- 4. SStatus / SControl
- 5. cmd / res / status / error（taskfile）
- 6. action 标志
- 7. 常见 failed command 名
- 8. DID_*（SCSI host byte）
- 9. SAM_STAT_*（SCSI status byte）
- 10. 常见日志短语 → 触发路径
- 11. 层级判别速查
- 12. 复发间隔模式与 SMART 清单

## 1. exception 行整体格式

典型：

```
ata2.00: exception Emask 0x0 SAct 0x1 SErr 0x0 action 0x6 frozen
ata2.00: failed command: WRITE FPDMA QUEUED
ata2.00: cmd 61/08:00:00:08:00/00:00:00:00:00/40 tag 0 ncq dma 4096 out
         res 40/00:00:00:00:00/00:00:00:00:00/00 Emask 0x4 (timeout)
ata2.00: status: { DRDY }
```

- `ata2.00`：libata port 2 上的设备，`.00` 为 PMP 端口号（无 PMP 时恒为 0；PMP 下 `.00`~`.14` 为各子端口，`.15` 常指 PMP 自身）。
- `SAct 0x1`：SActive 寄存器，bit0 置位表示 tag 0 的 NCQ 命令正在执行。
- `frozen`：超时路径下 libata 先冻结 port（ap->pflags |= ATA_PFLAG_FROZEN），中止在飞命令，再进入 EH。frozen 是"要进 EH"的信号，不是故障本身。

## 2. Emask（AC_ERR_* 错误位）

Emask 是 libata 内部的错误类别掩码（`include/linux/libata.h` 中 `AC_ERR_*`）。这是日志解读中最常用的字段：

| 值 | 宏 | 含义 |
|---|---|---|
| 0x1 | AC_ERR_DEV | 设备返回了错误（看 res/status/error 寄存器） |
| 0x2 | AC_ERR_HSM | 主机状态机违规（HSM violation） |
| 0x4 | AC_ERR_TIMEOUT | 命令超时 |
| 0x8 | AC_ERR_MEDIA | 介质错误 |
| 0x10 | AC_ERR_ATA_BUS | ATA 总线错误（常见于 CRC/链路问题） |
| 0x20 | AC_ERR_HOST_BUS | 主机总线错误 |
| 0x40 | AC_ERR_SYSTEM | 系统错误 |
| 0x80 | AC_ERR_INVALID | 无效命令/参数 |
| 0x100 | AC_ERR_OTHER | 其他 |
| 0x200 | AC_ERR_NODEV_HINT | 设备可能已消失 |
| 0x400 | AC_ERR_NCQ | NCQ 相关错误 |

组合常见模式：`Emask 0x4 (timeout)` + 无 SErr → 命令在飞期间设备/链路没回应；`Emask 0x10 (ATA bus error)` + SErr CRC 位 → 链路数据完整性问题。

## 3. SError 寄存器位

SError 是 SATA 接口自身的错误寄存器（32 位），由 PHY/链路层硬件置位，libata 在进入 EH 时读出并打印。位定义（`include/linux/ata.h`）：

| 位 | 值 | 宏 | 含义 |
|---|---|---|---|
| 0 | 0x1 | SERR_DATA | 恢复的数据完整性错误 |
| 1 | 0x2 | SERR_COMM | 恢复的通信错误 |
| 16 | 0x10000 | SERR_PHYRDY_CHG | PHYRDY 状态变化（链路 up/down 抖动） |
| 17 | 0x20000 | SERR_PHY_INT_ERR | PHY 内部错误 |
| 18 | 0x40000 | SERR_COMM_WAKE | 从省电状态唤醒时通信错误 |
| 19 | 0x80000 | SERR_10B_8B_ERR | 10b/8b 解码错误 |
| 20 | 0x100000 | SERR_DISPARITY | Disparity 错误 |
| 21 | 0x200000 | SERR_CRC | CRC 错误 |
| 22 | 0x400000 | SERR_HANDSHAKE | 握手错误 |
| 23 | 0x800000 | SERR_LINK_SEQ_ERR | 链路层状态机序列错误 |
| 24 | 0x1000000 | SERR_TRANS_ST_ERROR | 传输状态机错误 |
| 25 | 0x2000000 | SERR_UNRECOG_FIS | 无法识别的 FIS |
| 26 | 0x4000000 | SERR_DEV_XCHG | 设备被更换/热插拔事件 |

解读要点：

- **展开自检**：在报告中把某个 SErr 值按位展开后，必须把各位的十六进制值加起来核对——和必须等于原值，不等就说明漏位或错位，重新展开。例如 `0x1990000` = 0x1000000 + 0x800000 + 0x100000 + 0x80000 + 0x10000 = bit24 + bit23 + bit20 + bit19 + bit16，漏掉任何一项都会导致和不等。这一规则同样适用于 SAct、Emask 等多位字段。
- **逐周期比较**：多个故障周期的 SErr 值不要只说"含 CRC"一笔带过——对比每个周期哪些位新增、哪些消失（SError 通常在 EH 时被清零，下一周期重新累积），增量规律是判断误码是否持续产生的关键证据。
- `SErr 0x4000000`（DEV_XCHG）：设备被拔插或链路对端复位，常见于热插拔、连接器抖动。
- bit 19/20/21（10B8B/Disparity/CRC）：信号完整性问题的典型指征——线缆、连接器、背板、速率过高。
- bit 16（PHYRDY_CHG）单独出现可能只是正常链路建立/断开；与 CRC/Disparity 同时出现才有诊断意义。
- 16 位以上的位属于 SError 的 DIAG 字段，低位（0~1）属于 ERR 字段。部分控制器驱动只上报部分位，看到全零不代表链路一定干净。

## 4. SStatus / SControl

打印形如 `SATA link up 6.0 Gbps (SStatus 133 SControl 300)`（由 `sata_print_link_status()` 打印）。

SStatus 低 12 位分三个字段：

| 字段 | 位 | 含义 |
|---|---|---|
| DET | 3:0 | 设备检测：0=无设备，1=有设备但未建立通信，3=有设备且通信已建立，4=PHY 离线 |
| SPD | 7:4 | 协商速率：1=Gen1 1.5Gbps，2=Gen2 3.0Gbps，3=Gen3 6.0Gbps |
| IPM | 11:8 | 电源管理状态：1=Active，2=Partial，6=Slumber，8=DevSleep |

所以 `SStatus 133` = IPM 1（Active）、SPD 3（6.0Gbps）、DET 3（链路建立）。`SStatus 0` = 链路 down / 无设备。

常见值速查（避免每次手工拆解）：

| 值 | 含义 |
|---|---|
| SStatus 133 | Active，6.0 Gbps，链路建立 |
| SStatus 123 | Active，3.0 Gbps，链路建立 |
| SStatus 113 | Active，1.5 Gbps，链路建立 |
| SStatus 0 | 链路 down / 无设备 |
| SControl 300 | SPD 上限 Gen3（6.0G），即未限速 |
| SControl 320 | SPD 上限 Gen2（3.0G），多为 EH 降速或 PMP 端口配置 |
| SControl 310 | SPD 上限 Gen1（1.5G），EH 降到底档 |

SControl 与 SStatus 同格式但表示软件设置的**限制**值；低位 DET 字段在 SControl 中是复位动作位而非状态，组合依版本而异——不确定时以源码为准。

## 5. cmd / res / status / error（taskfile）

`cmd` 行的完整格式（libata-eh.c `ata_eh_link_report()` 的打印）：

```
cmd <command>/<feature>:<nsect>:<lbal>:<lbam>:<lbah>/<hob_feature>:<hob_nsect>:<hob_lbal>:<hob_lbam>:<hob_lbah>/<device> tag <n> <协议> <长度> <方向>
```

斜杠把 taskfile 分成三组：当前寄存器组（feature:nsect:lbal:lbam:lbah）、HOB 寄存器组（48 位地址的高 24 位）、device 字节。

字段解读规则：

- 48 位 LBA = `hob_lbah:hob_lbam:hob_lbal` 拼接为高 24 位，`lbah:lbam:lbal` 为低 24 位。
- NCQ 命令（60/61）中，扇区数在 `feature:hob_feature`，`nsect` 字段实际装的是 tag。
- `device` 字节常见 0x40（LBA 模式位）。

完整示例：`cmd 61/08:00:00:08:40/00:00:3a:00:00/40 tag 0 ncq dma 4096 out`

- command=0x61 WRITE FPDMA QUEUED；feature:hob_feature=08:00 → 8 扇区 = 4096 字节，与行尾 `dma 4096` 一致（可用来交叉验证解码是否正确）。
- LBA = 00:00:3a（HOB）+ 40:08:00（低 24 位）= 0x3A400800。
- device=0x40（LBA 模式）。

**不要在没有把握时给出精确 LBA 值。** 如果只需要说明"是哪类命令、哪个 tag、读写方向、多大长度"，不必反推 LBA；反推时必须用行尾的扇区数（`dma <bytes>`）做交叉验证，验证不上就只描述字段含义。

命令号速查：`61` WRITE FPDMA QUEUED、`60` READ FPDMA QUEUED、`25` READ DMA EXT、`35` WRITE DMA EXT、`ec` IDENTIFY DEVICE、`e5`/`e0` 等见 ATA 命令集。
- `res 40/00:...`：命令结束时设备返回的 taskfile，第一个字节是 status、第二个是 error。
- status 位：0x80 BSY、0x40 DRDY、0x20 DF（device fault）、0x10 DSC、0x08 DRQ、0x04 CORR、0x01 ERR。
- 超时场景典型 `res 40/00`：DRDY 置位、无 ERR——设备空闲但没有回完成中断，命令"无声丢失"。
- 设备报错场景典型 `res 51/04`：status 0x51（DRDY+DSC+ERR），error 0x04（ABRT，命令中止）。ABRT 常见于设备固件拒绝命令或 NCQ 内部失败。
- error 寄存器位（ATA）：0x01 AMNF、0x02 TK0NF、0x04 ABRT、0x08 MCR、0x10 IDNF、0x20 MC、0x40 UNC（不可纠正介质错误）、0x80 ICRC/BADBLOCK（UDMA CRC）。IDNF/UNC 指向介质层，ICRC 指向链路层。

## 6. action 标志

`action 0x6` 是 EH 计划执行的动作组合（ATA_EH_* 位），常见位（不同内核版本取值有差异，以源码为准）：reset、revalidate、PM reset 等。报告中引用 action 值时给出按源码解析的结果；版本不确定时只描述其触发的实际行为（后续日志里做了什么），不要硬背位值。

## 7. 常见 failed command 名

| 命令名 | 含义与提示 |
|---|---|
| READ/WRITE FPDMA QUEUED | NCQ 读写，tag 在 SAct 中对应；超时常见于链路/固件 |
| READ/WRITE DMA (EXT) | 非 NCQ 的 DMA 读写 |
| IDENTIFY DEVICE / IDENTIFY PACKET DEVICE | 识别阶段失败 → 设备未就绪或链路根本不通 |
| FLUSH CACHE (EXT) | 关机/卸载路径超时需特别留意 |
| SET FEATURES | 配置阶段失败，常为兼容性问题 |
| READ LOG (DMA) EXT / SMART | SMART 读取失败，单独出现不一定影响数据路径 |

## 8. DID_*（SCSI host byte）

SCSI 层 `result` 高 8 位（host byte），来自 `include/scsi/scsi.h`：

| 值 | 宏 | 含义 |
|---|---|---|
| 0x00 | DID_OK | 成功 |
| 0x01 | DID_NO_CONNECT | 无法连接（设备不在） |
| 0x02 | DID_BUS_BUSY | 总线忙超时 |
| 0x03 | DID_TIME_OUT | 命令超时 |
| 0x05 | DID_ABORT | 命令被中止 |
| 0x08 | DID_RESET | 被 reset 终止 |
| 0x0b | DID_SOFT_ERROR | 驱动层重试后的软错误 |
| 0x0e | DID_TRANSPORT_DISRUPTED | 传输层中断（设备消失） |
| 0x10 | DID_TARGET_FAILURE | 目标故障（设备将被离线） |

SATA 场景里 DID_NO_CONNECT / DID_TRANSPORT_DISRUPTED / DID_TARGET_FAILURE 通常意味着 libata 已判定设备消失并通知 SCSI 层下线，对应用户态看到 `sdX` 消失。

## 9. SAM_STAT_*（SCSI status byte）

| 值 | 宏 |
|---|---|
| 0x00 | SAM_STAT_GOOD |
| 0x02 | SAM_STAT_CHECK_CONDITION |
| 0x08 | SAM_STAT_BUSY |
| 0x28 | SAM_STAT_TASK_SET_FULL |
| 0x30 | SAM_STAT_ACA_ACTIVE |
| 0x40 | SAM_STAT_TASK_ABORTED |

CHECK_CONDITION 伴随 sense data；libata 会把 ATA 错误翻译成 sense（如 Medium Error / Aborted Command）。

## 10. 常见日志短语 → 触发路径

| 短语 | 来源与含义 |
|---|---|
| `hard resetting link` | EH reset 阶段发起硬复位（COMRESET 路径，ahci 下为 `ahci_hardreset` → `sata_link_hardreset`） |
| `softreset failed (device not ready)` | 软复位后设备未就绪；常升级为硬复位 |
| `COMRESET failed (errno=-32)` | 硬复位后链路未能建立，-32 多为 -EPIPE；链路/PHY/供电方向 |
| `link is slow to respond, please be patient (ready=0)` | 复位后等待 PHYRDY 超时前提示；常随后报 COMRESET/softreset failed |
| `failed to IDENTIFY` | 链路 up 但 IDENTIFY 命令失败；链路勉强通、设备异常或信号质量差 |
| `limiting SATA link speed to 1.5 Gbps` | EH 降速策略：连续 reset 失败后限制速率重试 |
| `SATA link up X Gbps` / `SATA link down` | 链路训练结果；反复的 down→up 是链路抖动证据 |
| `configured for UDMA/133` | 重新识别并配置成功，EH 恢复路径的收尾 |
| `EH complete` | 本轮 EH 结束；注意 EH complete ≠ 问题解决，只是恢复动作做完 |
| `revalidation failed (errno=-5)` | reset 成功后重新验证设备参数失败，设备将被摘除 |
| `device offline` / `detaching` | libata 放弃该设备，SCSI 层下线 |
| `host_eh_scheduled` | SCSI 层调度 EH 到 libata（libata-scsi 入口） |
| `NCQ disabled` | 部分版本在反复 NCQ 错误后禁用 NCQ（以源码确认） |
| `qc timeout` / `timeout` | `ata_qc_timeout` 路径，命令超过超时（默认 30s，sysfs 可调） |

## 11. 层级判别速查

| 日志组合 | 倾向层级 |
|---|---|
| SErr CRC/Disparity/10B8B + reset 后恢复 + 介质无错 | 物理链路层 / 信号完整性 |
| timeout + reset 恢复 + SMART 正常 | 链路抖动 / 供电 / 固件，证据不足以定盘坏 |
| COMRESET failed 反复 + link down | PHY / 线缆 / 背板 / 供电 |
| 多盘同控制器同时异常 | AHCI/HBA/驱动/供电 |
| PMP 单一子端口异常、其余正常 | 该子端口链路或该盘 |
| PMP 全部子端口异常 | PMP 芯片 / 主链路 |
| ABRT + IDENTIFY 失败、reset 无效 | 磁盘固件层 |
| UNC/IDNF + sense Medium Error | 磁盘介质层 |
| 仅特定内核版本出现、换内核消失 | libata/平台驱动缺陷 |

## 12. 复发间隔模式与 SMART 清单

**复发间隔本身就是证据。** 多次故障的时间间隔要算出来并解读，不要只写"反复发生"：

| 间隔模式 | 倾向解读 |
|---|---|
| 间隔高度接近（近似周期性，如每次都 ~70 分钟） | 周期性诱因：定时任务（cron/systemd timer/fstrim/巡检）、固件内部周期行为、电源管理定时 |
| 间隔逐渐缩短、恢复耗时逐渐变长 | 进行性恶化：链路质量下降、介质缺陷扩散、供电劣化 |
| 与高 I/O 负载强相关 | 负载触发：NCQ 深度、供电瞬降、温度 |
| 完全随机、无模式 | 外部扰动：振动、热插拔、间歇性接触不良 |

**SMART 关注属性清单**（`smartctl -x` 中优先核对）：

| 属性 | 指向 |
|---|---|
| 199 UDMA_CRC_Error_Count | 链路/信号完整性（随故障增长 → 线缆连接器方向） |
| 188 Command_Timeout | 命令超时次数（与日志 EH 次数对应） |
| 5 Reallocated_Sector_Ct | 介质层 |
| 197 Current_Pending_Sector | 介质层 |
| 198 Offline_Uncorrectable | 介质层 |
| 194 Temperature_Celsius | 过热诱因 |
| 12 Power_Cycle_Count / 4 Start_Stop_Count | 异常断电/重启痕迹 |

注意不同厂商属性 ID 和原始值编码有差异，解读原始值前先确认该型号的习惯用法；拿不准就只报告属性名和变化趋势，不断言绝对含义。
