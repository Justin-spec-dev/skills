# skills

自用的 AI Agent 技能（Skills）合集，适用于 Codex / Claude Code / Kimi Code 等支持 SKILL.md 规范的 Agent。

## 技能列表

| 技能 | 说明 |
|---|---|
| [chatgpt-image-gen](./chatgpt-image-gen) | 通过 ego-browser 驱动已登录的 ChatGPT 生成图片，并自动保存到当前项目目录 |
| [embedded-device-debugger](./embedded-device-debugger) | 通过 SSH 或串口安全连接嵌入式设备，采集日志、执行诊断命令并分析问题 |

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
# 保存全部生成图（GPT-4o 通常一次出 4 张），文件名自动带 -1/-2/-3/-4 后缀
bash chatgpt-image-gen/scripts/gen-image.sh --all "<图片描述>" "<输出文件绝对路径>"
```

脚本退出码：`0` 成功；`42` 需要登录 ChatGPT；`1` 其他失败（超时、下载失败、无效图片、内部错误等；任何失败都会输出 JSON 状态说明原因）；`2` 参数错误。

### 工作原理

1. `scripts/gen-image.sh` 通过 `ego-browser nodejs` 在独立任务空间中打开 `chatgpt.com` 新会话（复用浏览器中的登录态，不干扰正常浏览）
2. 填入图片描述并发送，轮询页面直到生成好的 `<img>` 出现（最长约 4.5 分钟）；`--all` 模式会等全部生成图到齐
3. 在页面上下文内 `fetch` 图片（携带会话 Cookie），base64 传回并写入本地文件，写前校验 PNG/JPEG 文件头
4. Agent 验证图片内容后运行技能自带的幂等清理脚本关闭任务空间；重复清理会返回 `already_closed`，不会报错

### 注意事项

- 脚本基于 ego lite 0.4.4.x 的 helper API（`useOrCreateTaskSpace`、`cliLog` 等），ego lite 大版本升级后如 API 变化可能需要调整
- ego 内嵌 Node 运行时不继承 shell 环境变量，参数通过 `/tmp/chatgpt-image-gen/` 下的临时文件传递
- 生成耗时通常 1–5 分钟，取决于 ChatGPT 生图速度

---

## embedded-device-debugger

面向嵌入式 Linux、BusyBox 和串口控制台的跨平台诊断技能，Windows 与 Linux 使用同一套 Python 工具。支持：

- SSH 密钥/Agent 登录、交互登录、自定义端口、严格主机密钥校验和命令超时
- 串口枚举、限时日志抓取、自动登录、Shell Prompt 识别和批量命令执行
- JSON 诊断报告（`ssh-run` / `serial-monitor` / `serial-run` 均支持 `--json`）、敏感信息脱敏、输出文件防误覆盖（`--force` / `--append`）
- 退出码可信度标记：SSH 超时会标注 `remote_may_still_run`，`serial-run --mode posix-shell` 未取到退出码时以 `125` 退出并标记 `exit_code_known: false`，不会把未知结果当成功
- 输出可限流：`--max-output` 限制单条命令的采集量并标记 `output_truncated`；解析缓冲区亦有上限，丢弃字节会上报为 `dropped_tail_bytes`
- 构建机 ↔ 目标板双机流程：`inspect` 在构建侧检视产物（架构、动态解释器、依赖库、构建 ID、是否 stripped，并可与设备 `uname -m` 做架构匹配判定），`deploy` 把产物送到设备，`verify` 比对设备上的文件与构建产物，`symbolize` 用交叉工具链把设备报的地址翻回源码行
- 传输自动降级：先 scp（含 legacy `-O`，应对 OpenSSH ≥9 默认走 SFTP 而 BusyBox/Dropbear 无 sftp-server），失败退回只依赖 `cat` 的管道；目标先写 `.part`，哈希校验通过后才原子替换，中途失败保留现场并给出清理命令
- 设备只能从构建机路由时用 `--device-jump [user@]host[:port]`（ssh ProxyJump）
- `deploy --capture-dmesg` 回读 `--run` 期间新增的内核日志；串口证据另起 `serial-monitor`，按时间戳对齐
- 启动、内核、驱动、CPU、内存、存储、网络和服务故障排查手册
- 默认只读诊断；重启、刷写、配置修改等变更操作需要用户明确授权。`deploy` 默认只输出计划（dry-run），不加 `--apply` 不会写入设备；`/sys`、`/proc`、`/dev` 下的目标一律拒绝，`/boot`、`/lib/modules`、`/etc` 需显式 `--unsafe-dest`

### 脱敏

`--redact-env NAME` 指定的环境变量若未设置、为空或短于 4 个字符，会在连接前直接报错而不是静默跳过；含 CR/LF 的密钥在所有传输方式下都会被拒绝，因为输出是逐行产生和保存的。

### 测试

单元测试无第三方依赖（PySerial 为惰性导入并在测试中 mock）：

```bash
cd embedded-device-debugger
python3 -m unittest discover -s tests -v
```

CI 在 Python 3.9 与 3.13 上运行该套件，见 `.github/workflows/tests.yml`。

### 依赖

- Python 3.9+
- SSH 功能：系统 OpenSSH 客户端。产物传输优先用同一套的 `scp`，缺失或设备无 sftp-server 时自动退回只依赖 `cat` 的管道，因此 `scp` 并非必需
- 产物校验：构建机侧需要一个哈希工具（`sha256sum`、`shasum -a 256` 或 `openssl dgst -sha256`）
- 串口功能：PySerial，按需安装：

```bash
python -m pip install -r embedded-device-debugger/requirements.txt
```

Linux 上可将 `python` 替换为 `python3`。

### 安装

```bash
# Codex
cp -r embedded-device-debugger ~/.codex/skills/

# Claude Code / Kimi Code 等
cp -r embedded-device-debugger ~/.agents/skills/
```

### 使用示例

安装后可直接告诉 Agent：

- “通过 SSH 连接 `root@192.168.1.100:2222`，只读检查设备启动异常”
- “枚举本机串口，通过 `COM5`、115200 波特率抓取 30 秒启动日志并分析”
- “通过 `/dev/ttyUSB0` 登录设备，检查内存占用和 OOM 日志”
- “把构建机上的 `app` 推到板子 `/tmp` 跑一下自测”（先出计划，授权后才写入）
- “确认板子上 `/usr/bin/app` 跑的就是刚编的那个”
- “把这段 oops 里的地址翻成源码行”

也可以手动调用工具。SSH 端口通过 `--port` 自定义：

```bash
python embedded-device-debugger/scripts/device_console.py ssh-run \
  --host 192.168.1.100 --user root --port 2222 \
  --command "uname -a" --command "uptime"
```

串口日志抓取：

```bash
python embedded-device-debugger/scripts/device_console.py serial-monitor \
  --port /dev/ttyUSB0 --baud 115200 --duration 30 --output boot.log
```

完整参数、安全边界及平台配置说明见 [embedded-device-debugger/SKILL.md](./embedded-device-debugger/SKILL.md)。

### 双机场景：构建机 ↔ 目标板

典型拓扑是**三台机器**：Agent 跑在你的笔记本/WSL 上，**构建机**（编译、产物所在）和**目标板**各自远程可达。说话时把「谁是谁」交代清楚即可：

1. 构建机是谁（地址 + 用户，非 22 端口要给端口）
2. 板子是谁（同上）
3. 产物在构建机上的**绝对路径**
4. 要跑什么、要不要留意内核日志

前两样固定的话可以说「还是那两台」，或写进项目的 `CLAUDE.md`。`--build-host` 缺省时构建侧动作在本机执行；板子只能从构建机路由时加 `--device-jump [user@]构建机`（ssh ProxyJump），之后 scp、校验、`mv`、`--run` 全部自动走跳板。

#### 场景一：改完代码 → 推上去跑 → 崩了看源码行

> 构建机 `root@10.0.0.5`，板子 `root@192.168.1.100` 端口 2222。
> 把构建机上 `/home/me/proj/build/app` 推到板子 `/tmp` 跑一下 `--selftest`，
> 要是崩了把地址翻成源码行。

```bash
# 1. 产物对不对得到这块板（只读）：架构、动态解释器、依赖库、build ID
python embedded-device-debugger/scripts/device_console.py inspect --build-host root@10.0.0.5 --artifact /home/me/proj/build/app \
  --host 192.168.1.100 --user root --port 2222 --json

# 2. 出计划并停下来等你确认（默认 dry-run，不写设备）
python embedded-device-debugger/scripts/device_console.py deploy --build-host root@10.0.0.5 --host 192.168.1.100 --user root --port 2222 \
  --artifact /home/me/proj/build/app --dest /tmp --run "/tmp/app --selftest" --capture-dmesg

# 3. 你说「可以」之后，才加 --apply 真执行
python embedded-device-debugger/scripts/device_console.py deploy --build-host root@10.0.0.5 --host 192.168.1.100 --user root --port 2222 \
  --artifact /home/me/proj/build/app --dest /tmp --apply --chmod 0755 \
  --run "/tmp/app --selftest" --capture-dmesg --json

# 4. 崩了：把日志里的地址翻成源码行
python embedded-device-debugger/scripts/device_console.py symbolize --build-host root@10.0.0.5 --binary /home/me/proj/build/app \
  --toolchain aarch64-linux-gnu- --addresses-from oops.log --json
```

第 2 步会**主动停下来**：不加 `--apply` 绝不写设备，所以你不必特意叮嘱「别乱写」。你只需看过计划后说一句「可以，执行」。

#### 场景二：板子上报错 → 拉回构建机的代码里分析

> 板子 `root@192.168.1.100` 上报了个 oops，日志我贴在下面。
> 代码和编译产物都在构建机 `root@10.0.0.5` 的 `/home/me/proj`，
> 内核是 `/home/me/proj/build/vmlinux`，工具链 `aarch64-linux-gnu-`。

```bash
# 0. 先确认板子上跑的就是这份源码编出来的（跳过这步最容易白干一下午）
python embedded-device-debugger/scripts/device_console.py verify --build-host root@10.0.0.5 --host 192.168.1.100 \
  --build-path /home/me/proj/build/app --device-path /usr/bin/app

# 1. 板子侧只读取证
python embedded-device-debugger/scripts/device_console.py ssh-run --host 192.168.1.100 --command "dmesg | tail -60"

# 2. 地址 → 源码行
python embedded-device-debugger/scripts/device_console.py symbolize --build-host root@10.0.0.5 --binary /home/me/proj/build/vmlinux \
  --toolchain aarch64-linux-gnu- --addresses-from oops.log --json
#    → do_thing  /home/me/proj/drivers/foo.c:412

# 3. 读构建机上的源码（代码不在本机，走 build-run）
python embedded-device-debugger/scripts/device_console.py build-run --build-host root@10.0.0.5 --command "sed -n '395,425p' /home/me/proj/drivers/foo.c"
python embedded-device-debugger/scripts/device_console.py build-run --build-host root@10.0.0.5 --command "grep -rn 'foo_dma_map' /home/me/proj | head"
```

这条链路全程**只读**（`inspect` / `symbolize` / `build-run` 都不写设备），可以放心用。日志直接贴进对话即可——`--addresses-from` 收的是本机文件，Agent 会把粘贴内容落地后再抽取地址。

三个决定成败的前提：**先 `verify`**（跑的若是另一个 build，后面所有行号都是自信的错误答案）；**二进制要有调试信息**（stripped 只会给出 `??:0`，`--binary` 可指向同 build-id 的未 strip 副本）；**内核地址要减 KASLR 偏移**（`--kaslr-offset`，报告会写清用没用）。

更细的传输梯度、串口证据时间戳对齐、故障对照表见 [references/build-and-deploy.md](./embedded-device-debugger/references/build-and-deploy.md)。
