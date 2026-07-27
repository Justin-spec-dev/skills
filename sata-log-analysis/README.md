# sata-log-analysis — Linux SATA 日志分析与故障诊断 Skill

一个面向 AI Agent 的可复用 Skill：分析 Linux 系统中 SATA/ATA 磁盘故障日志，产出结构化诊断报告，并可选地结合用户提供的 Linux 内核源码做函数级根因分析。

## 目录结构

```
sata-log-analysis/
├── SKILL.md                          # Skill 定义（含 frontmatter，平台通用格式）
├── README.md                         # 本文件：安装、启用、调用说明
├── references/
│   ├── log-fields.md                 # 关键日志字段位级解码（Emask/SError/SStatus/res/DID_*/SAM_STAT_*）
│   ├── eh-flow.md                    # libata EH 机制、关键函数、调试手段、sysfs 节点
│   └── report-template.md            # 输出报告模板（十章结构）
├── examples/
│   ├── sample-ata-timeout-eh.log     # 测试样例 1：ATA 命令超时触发 EH（可恢复）
│   ├── sample-sata-pmp-failure.log   # 测试样例 2：SATA PM/PMP 下挂磁盘故障
│   └── sample-report.md              # 由样例 1 生成的示例分析报告
├── evals/
│   ├── evals.json                    # 评估用测试提示与断言（路径相对于本目录）
│   └── fixtures/mock-kernel-source/  # eval-4 用模拟内核源码树（5.15.87，故意与样例日志版本不一致）
└── dist/
    └── sata-log-analysis.generic.md  # 平台无关单文件版（内联核心参考内容）
```

## 能力范围

- SATA 链路异常、ATA 命令超时、libata 错误恢复（EH）、SATA Port Multiplier（PMP）
- 掉盘、重连、降速、CRC/PHY/链路复位异常、NCQ/DMA/IO 超时、AHCI/HBA/控制器异常
- 支持粘贴日志、日志文件路径、日志目录三种输入；大日志自动用 `rg`/`sed` 筛选
- 可选：结合用户提供的内核源码目录做函数级分析（drivers/ata/ 等）

**不做**：NVMe、SAS、USB 存储、纯文件系统问题的泛化分析（除非与 SATA 故障直接相关）。

## 输入规范

| 输入方式 | 说明 |
|---|---|
| 直接粘贴 | 任意长度的日志片段；Skill 会先筛选 SATA 相关内容 |
| 文件路径 | 一个或多个 `dmesg`、`journalctl -k`、`/var/log/messages`、`/var/log/kern.log`、串口日志等；先检查存在性与权限 |
| 目录 | 自动在目录内查找 `dmesg*`、`messages*`、`kern.log*`、`syslog*`、`console*`、`serial*` 等候选文件 |
| 内核源码目录（可选） | 分析开始前询问一次；提供后按根目录 Makefile 识别版本并做源码级定位；回复"没有"则纯日志分析 |

## 输出规范

Markdown 报告，固定十章结构（详见 `references/report-template.md`）：

1. 分析结论 2. 环境与设备信息 3. 故障时间线 4. 关键日志解析 5. libata EH 流程分析
6. 内核源码分析（仅提供源码时） 7. 根因推测（P1/P2/P3 证据表） 8. 排查与验证方案（三层）
9. 信息缺口 10. 最终判断（已确认事实 / 高概率判断 / 尚未验证的推测）

## 安装与启用

### 平台通用（Claude Code 风格目录约定）

把 `sata-log-analysis/` 整个目录放入对应工具的 skills 目录即可。常见位置（以各工具当前文档为准）：

| 工具 | 用户级 | 项目级 |
|---|---|---|
| Claude Code | `~/.claude/skills/sata-log-analysis/` | `<项目>/.claude/skills/sata-log-analysis/` |
| Kimi Code CLI | `~/.agents/skills/sata-log-analysis/` | `<项目>/.agents/skills/sata-log-analysis/` |
| OpenCode | `~/.config/opencode/skill/sata-log-analysis/` | `<项目>/.opencode/skill/sata-log-analysis/` |
| Codex CLI | `~/.codex/skills/sata-log-analysis/` | 项目内对应 skills 目录 |

安装后重启或重载工具，Agent 会在日志中出现 `ataX`、`exception Emask`、`SError`、`hard resetting link` 等特征或用户描述掉盘/超时/降速时自动触发本 Skill。

### 导出到其他 Agent 环境（平台无关版）

对于不支持 skills 目录机制、只支持自定义指令/系统提示的 Agent 环境（如自定义 Agent 框架、CI 机器人、web 端助手），使用单文件导出：

```
dist/sata-log-analysis.generic.md
```

该文件内联了 SKILL.md 的工作流程与两份 references 的核心内容（字段解码表、EH 流程要点、报告模板），可直接粘贴到目标 Agent 的系统提示词、AGENTS.md、`instructions` 字段或自定义工具描述中。

### 调用示例

```
# 粘贴日志
"帮我分析这段日志：<粘贴 dmesg 片段>"

# 文件路径
"分析 /var/log/kern.log.1，机器昨天掉盘了"

# 目录
"这个目录里有串口日志和 dmesg，帮我查 SATA 相关问题：/tmp/logs/"

# 带源码
"内核源码在 ~/src/linux-5.15，日志在 /tmp/dmesg.txt，分析 ata3 超时"
```

## 自检

```bash
# 验证目录结构与内部引用有效
bash self-check.sh
```

## 设计约束（摘要）

- EH 是恢复机制不是根因；少量日志不断言硬盘损坏
- 区分磁盘/链路/控制器/驱动故障；区分 `ataX`/`ataX.Y`/`hostX`/`sdX`
- 源码分析必须基于用户实际提供的源码；不确定的结论明确标注
- 破坏性操作（设备离线、热插拔、写寄存器、刷固件等）必须先警示风险
