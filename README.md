# skills

自用的 AI Agent 技能（Skills）合集，适用于 Claude Code / Kimi Code 等支持 SKILL.md 规范的 Agent。

## 技能列表

| 技能 | 说明 |
|---|---|
| [chatgpt-image-gen](./chatgpt-image-gen) | 通过 ego-browser 驱动已登录的 ChatGPT 生成图片，并自动保存到当前项目目录 |
| [sata-log-analysis](./sata-log-analysis) | 分析 Linux SATA/ATA 磁盘故障日志（超时、掉盘、libata EH、SATA PMP 等），可选结合内核源码做函数级分析，产出结构化诊断报告 |

---

## chatgpt-image-gen

一句话描述需求即可生成图片：Agent 自动打开 ChatGPT 新会话、发送图片描述、等待生成、下载并以合适的文件名保存到当前项目目录。

### 依赖

- **ego lite 浏览器**：macOS 应用，提供 `ego-browser` CLI。未安装时按 `chatgpt-image-gen/SKILL.md` 中引用的 ego-browser 技能 `references/install.md` 完成安装，或访问 https://lite.ego.app/
- **ChatGPT 账号**：需要在 ego lite 浏览器中已登录 ChatGPT（图片生成需要 Plus 等支持生图的账号）。未登录时脚本会交还浏览器控制权，手动登录后可继续
- 支持 SKILL.md 的 AI Agent（Claude Code、Kimi Code 等）

### 安装

将 `chatgpt-image-gen` 目录复制到 Agent 的技能目录即可：

```bash
# Claude Code / Kimi Code 用户级技能目录
cp -r chatgpt-image-gen ~/.agents/skills/
# 或项目级
cp -r chatgpt-image-gen <your-project>/.agents/skills/
```

### 使用方法

安装后无需手动调用脚本，直接用自然语言描述需求，例如：

- "帮我生成一张西安旅游攻略图"
- "生成一张 LLM 架构图，保存到项目里"

Agent 会自动提炼文件名（如 `西安旅游攻略图.png`）、运行生成脚本、验证图片内容并汇报保存路径。

也可以直接手动运行脚本：

```bash
bash chatgpt-image-gen/scripts/gen-image.sh "<图片描述>" "<输出文件绝对路径>"
```

脚本退出码：`0` 成功；`42` 需要登录 ChatGPT；`1` 其他失败（超时、下载失败等，输出 JSON 状态说明原因）。

### 工作原理

1. `scripts/gen-image.sh` 通过 `ego-browser nodejs` 在独立任务空间中打开 `chatgpt.com` 新会话（复用浏览器中的登录态，不干扰正常浏览）
2. 填入图片描述并发送，轮询页面直到生成好的 `<img>` 出现（最长约 4.5 分钟）
3. 在页面上下文内 `fetch` 图片（携带会话 Cookie），base64 传回并写入本地文件
4. Agent 验证图片内容后运行技能自带的幂等清理脚本关闭任务空间；重复清理会返回 `already_closed`，不会报错

### 注意事项

- 脚本基于 ego lite 0.4.4.x 的 helper API（`useOrCreateTaskSpace`、`cliLog` 等），ego lite 大版本升级后如 API 变化可能需要调整
- ego 内嵌 Node 运行时不继承 shell 环境变量，参数通过 `/tmp/chatgpt-image-gen/` 下的临时文件传递
- 生成耗时通常 1–5 分钟，取决于 ChatGPT 生图速度

---

## sata-log-analysis

分析 Linux 系统中 SATA/ATA 磁盘相关故障日志并产出结构化诊断报告：SATA 链路异常、ATA 命令超时、libata 错误恢复（EH）、SATA Port Multiplier（PMP）、掉盘/重连/降速、CRC/PHY/链路复位、NCQ/DMA/IO 超时、AHCI/HBA 控制器异常。可选结合用户提供的 Linux 内核源码做函数级根因分析。不用于 NVMe/SAS/USB 存储或纯文件系统问题。

### 特点

- 支持粘贴日志、日志文件路径、日志目录三种输入；大日志自动用 `rg`/`sed` 筛选并保留上下文
- 分析前询问一次内核源码目录：提供则验证版本并定位 `drivers/ata/` 等源码做函数级分析；没有则纯日志分析并明确标注证据边界
- 输出固定结构的专业诊断报告（时间线、字段位级解码、EH 流程、P1/P2/P3 根因证据表、分层排查方案、信息缺口）
- 内置反幻觉约束：不断言盘坏、不把 EH 当根因、不引用无出处"型号口碑"、不编造未读取的源码
- 附带两个测试样例日志、示例报告、评估断言集（`evals/`）和自检脚本（`self-check.sh`）
- `dist/` 内含平台无关单文件版，可粘贴到任意 Agent 的系统提示词使用

### 依赖

- 支持 SKILL.md 的 AI Agent（Claude Code、Kimi Code、OpenCode、Codex 等）
- 可选：`rg`（日志筛选，无则回退 grep）

### 安装

将 `sata-log-analysis` 目录复制到 Agent 的技能目录即可：

```bash
# Claude Code / Kimi Code 用户级技能目录
cp -r sata-log-analysis ~/.agents/skills/
# 或项目级
cp -r sata-log-analysis <your-project>/.agents/skills/
```

其他平台安装位置见 `sata-log-analysis/README.md`。

### 使用方法

安装后用自然语言提出分析需求，例如：

- "帮我分析这段日志：（粘贴 dmesg 片段）"
- "分析 /var/log/kern.log.1，机器昨天掉盘了"
- "内核源码在 ~/src/linux-5.15，日志在 /tmp/dmesg.txt，分析 ata3 超时"

Agent 遇到 `ataX`、`exception Emask`、`SError`、`hard resetting link` 等日志特征或掉盘/超时/降速描述时会自动触发本技能。
