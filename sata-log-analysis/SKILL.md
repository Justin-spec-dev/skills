---
name: sata-log-analysis
description: >-
  分析 Linux 系统中 SATA/ATA 磁盘相关故障日志并产出结构化诊断报告。覆盖 SATA 链路异常、ATA
  命令超时、libata 错误恢复(EH)、SATA Port Multiplier(PMP)、掉盘/重连/降速、CRC/PHY/链路复位异常、
  NCQ/DMA/IO 超时、AHCI/HBA/SATA 控制器异常。只要用户粘贴或给出包含 ataX、exception Emask、
  SError、SStatus、hard resetting link、COMRESET、failed command、frozen、link is slow to
  respond、limiting SATA link speed、NCQ disabled 等字样的 dmesg、journalctl -k、/var/log/messages、
  /var/log/kern.log 或串口日志，或用户提到"掉盘""SATA 降速""磁盘超时""硬盘识别不到"，就必须使用本
  Skill；即使用户只说"帮我看看这段日志"，也应先检查其中是否含有 SATA/ATA/libata 相关内容。也可结合
  用户提供的 Linux 内核源码做函数级根因分析。不用于 NVMe、SAS、USB 存储或纯文件系统问题（除非与
  SATA 故障直接相关）。
---

# Linux SATA 日志分析与故障诊断

## 适用范围与边界

只聚焦 SATA、ATA、AHCI、libata、SATA PM/PMP 相关问题。NVMe、SAS、USB 存储、MD/LVM、文件系统问题不属于本 Skill 的分析对象；只有当它们与 SATA 故障存在直接关联（例如 SCSI 层把 libata 错误向上传递为 I/O error）时，才作为辅助信息简要说明，且不展开分析。

牢记三条基本原则，它们决定了报告质量：

1. **libata EH 是恢复机制，不是根因。** "hard resetting link""EH complete" 是内核在收拾残局。根因要往命令超时、链路异常、PHY 训练失败的方向找。
2. **少量日志不能断言硬盘损坏。** 命令超时可能是线缆、背板、供电、控制器、驱动、固件中的任何一环，必须用证据链区分磁盘故障、链路故障、控制器故障和驱动故障。
3. **不确定就说不确定。** 无法确认的寄存器位、无法定位的源码、日志中缺失的信息，一律明确标注，不得编造。

## 输入处理

支持三种输入方式：用户直接粘贴日志；用户给出一个或多个日志文件路径；用户给出日志目录。

处理文件输入时，按以下顺序执行：

1. 先确认路径存在、可读（`test -r`）。文件不存在或无权限时，明确告知用户并请其修正，不要静默跳过。
2. 检查文件大小（`ls -lh` 或 `du -h`）。对于大日志（数 MB 以上）**不要完整加载**，先用筛选命令提取 SATA 相关内容并保留上下文。首选：

   ```bash
   # 提取 SATA/ATA/libata/AHCI 相关行，保留行号
   rg -n -i 'ata[0-9]|ata_piix|libata|ahci|sata|SError|SStatus|COMRESET|Emask|FPDMA|NCQ|hard resetting|softreset|scsi [0-9]|sd [0-9]' <日志文件>

   # 定位到故障行后，取前后上下文（例如第 N 行前后各 80 行）
   sed -n "$((N-80)),$((N+80))p" <日志文件>
   ```

3. 目录输入时，先在目录内查找候选文件：`dmesg*`、`messages*`、`kern.log*`、`syslog*`、串口日志（常见 `console*`、`serial*`、`uart*`）、以及用户自定义名称。用上述 `rg` 模式对每个候选文件试筛，只处理有命中的文件。
4. 注意并显式提示以下情况：日志内容不完整（开头/结尾被截断）、时间戳缺失或被截断（影响时间线重建，需改用相对偏移）、多份日志时间线不一致（分别建立时间线并标注各自来源，不要强行合并）。

## 步骤 0：询问内核源码（只问一次）

开始分析前，向用户询问一次：

> 当前故障系统对应的 Linux 内核源码目录在哪里？如果没有源码目录，可以直接回复"没有"。

处理规则：

- **用户回答没有**：直接进行日志分析。报告中明确说明结论基于日志内容、Linux libata 通用机制和已知内核行为，不包含源码级验证。
- **用户提供目录**：
  1. 确认目录存在。
  2. 验证其为有效内核源码目录：读取根目录 `Makefile` 的 `VERSION`/`PATCHLEVEL`/`SUBLEVEL`/`EXTRAVERSION` 字段得到内核版本，并检查 `drivers/ata/`、`init/`、`Kbuild` 是否存在。验证失败则告知用户并按"没有源码"处理。
  3. 比对源码版本与日志中的内核版本（`uname` 行或 `Linux version` 行）。版本不一致时在报告中显著标注，因为行号和行为可能漂移。
  4. 根据日志中的函数名、错误信息字符串、调用路径，用 `rg` 在源码中实际定位。优先搜索的目录和文件：
     - `drivers/ata/`：`libata-core.c`、`libata-eh.c`、`libata-scsi.c`、`libata-sata.c`、`ahci.c`、`libahci.c`、`sata_pmp.c`，以及对应 SoC/平台 SATA 驱动（如 `ahci_*`、`sata_*` 平台文件）
     - `drivers/scsi/`、`block/`、`include/linux/libata.h`、`include/linux/ata.h`、`include/scsi/`
  5. **不得仅凭函数名猜测逻辑。** 必须实际阅读函数实现：打印该日志的语句在哪个函数、进入该分支的条件、err_mask/action 标志如何设置、reset 失败后如何升级恢复级别、最终如何决定重试/离线。每个源码结论都要能回指到具体文件和函数。

## 步骤 1：提取基础信息

从日志中识别并整理为表格：内核版本、操作系统版本、SATA 控制器/HBA 类型、AHCI 控制器信息（`ahci xxxx: ...` 行）、ATA port 编号、link 编号、`ataX` / `ataX.Y` / `hostX` / `sdX` 映射关系、硬盘型号、固件版本、序列号、协商速率（1.5/3.0/6.0 Gbps）、是否使用 SATA Port Multiplier 及 PMP 端口号、SCSI host/target/LUN 映射、故障发生时间与持续时间。

映射关系的线索：`ataX.Y` 中 X 是 libata port 号、Y 是 PMP 端口号（无 PMP 时为 .00）；`scsi hostX` 通常与 `ataX` 对应；`sd X:0:0:0: [sdX]` 行给出块设备名。逐项核对，**必须区分** `ataX`、`ataX.Y`、`hostX`、`sdX`，不要混用。

日志中找不到的字段写"日志中未提供"，不要自行补全。

## 步骤 2：建立事件时间线

按日志原始时间顺序整理关键事件：正常 I/O → ATA 命令超时 → frozen port → exception Emask → hard resetting link → COMRESET → SATA link up/down → 重新识别设备 → EH 重试/降级 → 设备离线/掉盘/恢复。

- **必须保留日志原始时间顺序**，不要按类型重排。
- 多次重复故障合并为"故障周期"呈现，但保留每次的发生时间、持续时间和周期间的差异（例如第一次恢复成功、第三次设备被离线）。

## 步骤 3：解码关键日志字段

逐位解释 `exception Emask`、`SError`、`SStatus`、`SControl`、`failed command`、`cmd`、`res`、`status`、`error`、`action`、`frozen` 等字段，以及 `hard resetting link`、`softreset failed`、`COMRESET failed`、`link is slow to respond`、`device not ready`、`failed to IDENTIFY`、`limiting SATA link speed`、`NCQ disabled`、`revalidation failed`、`DID_*`、`SAM_STAT_*` 等关键短语。

详细的位定义、常见取值含义、以及每个短语对应的 libata 触发路径，见 [references/log-fields.md](references/log-fields.md)。分析具体日志时先读该文件。**无法确认定义的位，明确标注不确定，不得编造。**

## 步骤 4：判断故障层级

将故障归类到一个或多个层级，并给出证据链（不是只给分类名）：

物理链路层（线缆/连接器/背板）、SATA PHY、SATA 协议层、ATA 命令层、SATA PM/PMP 层、AHCI 控制器层、HBA 或驱动层、磁盘固件层、磁盘介质层、电源或背板层、内核 libata 错误恢复层、上层 SCSI/block I/O 层。

典型判别线索（完整版见 references/log-fields.md）：

- `SError` 出现 CRC/Disparity/10B8B 位 + 重试后多能恢复 → 偏向物理链路/信号完整性
- 命令超时且 reset 后设备可重新识别、介质无错 → 偏向链路抖动/供电/固件，不足以断言盘坏
- reset 反复失败、COMRESET failed → 偏向 PHY/线缆/背板/供电
- 同一控制器上多盘同时异常 → 偏向控制器/HBA/驱动/供电，而非单盘介质
- PMP 单一子端口异常且其他子端口正常 → 偏向该子端口后的盘或该段链路；PMP 下所有端口同时异常 → 偏向 PMP 芯片或主链路

## 步骤 5：分析 EH 流程

当日志触发 libata EH 时，逐项回答：EH 的触发原因（哪个命令超时/失败）、port 是否冻结（frozen）、EH action 包含哪些操作、是否执行 soft reset / hard reset / PMP reset、是否重新识别设备、是否重新验证容量参数、是否降低链路速率、是否禁用 NCQ、是否重试命令、最终是否将设备离线。

libata EH 的内部机制、关键函数、恢复级别升级路径和常用动态调试手段，见 [references/eh-flow.md](references/eh-flow.md)。需要深入 EH 行为或给出调试建议时先读该文件。

若提供了内核源码，每一步都要引用具体函数和代码路径：日志对应哪个 `ata_port`、哪个函数打印该日志、哪个条件进入当前分支、action 标志如何设置、reset 失败后如何升级、最终如何决定恢复/重试/detach。

## 步骤 6：根因推测与诊断建议

根因按 P1/P2/P3 输出（表格列：优先级、可能原因、支持证据、反向证据、置信度）。候选池：SATA 线缆/连接器/背板接触不良、信号完整性、PHY 训练失败、电源跌落、硬盘响应超时、磁盘固件异常、磁盘介质错误、控制器/HBA 异常、SATA PM 芯片异常、PMP 下挂单盘故障影响整条链路、libata/平台驱动缺陷、中断丢失、DMA 卡死、NCQ 异常、热插拔/机械抖动、链路速率过高导致不稳定。**只保留与当前日志证据相关的原因，不要罗列全部。**

诊断建议分三层，按优先级排列，每条给出操作方法、预期结果、以及如何根据结果决定下一步：

1. **低风险检查**：完整 `dmesg -T`、`journalctl -k`、`/sys/class/ata_link/`、`/sys/class/ata_port/`、`/sys/class/scsi_host/`、`smartctl`、协商速率与错误计数、故障盘与正常盘对比、不同 SATA 端口对比、重启前后对比。
2. **硬件交叉验证**：更换 SATA 线缆、背板槽位、SATA 端口、电源通道；单盘直连绕过 PMP；故障盘/正常盘对调端口；临时降速到 3.0/1.5 Gbps。
3. **软件或内核验证**：对比内核版本、检查相关驱动补丁、libata 动态调试（dynamic debug）、tracepoint、关键函数加日志、超时参数、EH action 与 reset 返回值、中断与 DMA 状态。

**破坏性操作必须先警示风险、征得用户同意后再给出或执行**，包括：设备离线（`echo offline`）、删除 SCSI 设备、reset 控制器、热插拔、写寄存器、修改内核参数、更新磁盘固件。不要在建议中把这类操作伪装成无风险步骤。

## 输出报告

最终报告**必须**使用 [references/report-template.md](references/report-template.md) 中的结构（十个章节：分析结论、环境与设备信息、故障时间线、关键日志解析、libata EH 流程分析、内核源码分析、根因推测、排查与验证方案、信息缺口、最终判断；未提供源码时按模板规则删除内核源码章并以一句话说明）。生成报告前先读该模板，并遵守其中的去重原则（§1 只给一句话摘要、§5 简单 EH 可压缩、§10 不复述证据链）。要点：

- 关键日志逐段引用并解释，不要大段粘贴无关日志。
- "已确认事实 / 高概率判断 / 尚未验证的推测"三者在最终判断章中严格分开。
- 给出的命令尽量可直接复制执行；筛选命令优先复用"输入处理"节的 rg 模式，保证覆盖一致。
- 专业、简洁、有证据链，避免泛泛而谈。

## 行为约束汇总

1. 只聚焦 SATA/ATA/AHCI/libata/SATA PM/PMP。
2. 不得根据少量日志直接断言硬盘损坏。
3. 不得把 EH 误判为根因。
4. 必须区分磁盘故障、链路故障、控制器故障、驱动故障。
5. 必须区分 `ataX`、`ataX.Y`、`hostX`、`sdX`。
6. 必须保留日志原始时间顺序。
7. 源码分析必须基于用户实际提供的源码；未提供时不得假装读过源码。
8. 不能确认的结论明确说明不确定性。
9. 命令可直接复制执行。
10. 报告有证据链，不空谈。
11. 不得把型号/固件的"口碑""已知故障率""已知问题版本"作为支持证据，除非来自用户提供的材料；这类信息只能以"建议查询厂商公告/固件更新日志"的形式出现在排查建议中。
12. 日志中未出现的角色属性（如"系统盘""数据盘""启动盘"）不得推断添加；任何推断性标注必须显式写明"推断"。
13. 无 PMP 时 `ataX.00` 的 `.00` 表示唯一设备，不要称之为"PMP 端口 0"。
