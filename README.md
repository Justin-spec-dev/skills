# skills

自用的 AI Agent 技能（Skills）合集，适用于 Claude Code / Kimi Code 等支持 SKILL.md 规范的 Agent。

## 技能列表

| 技能 | 说明 |
|---|---|
| [chatgpt-image-gen](./chatgpt-image-gen) | 通过 ego-browser 驱动已登录的 ChatGPT 生成图片，并自动保存到当前项目目录 |
| [gemini-image-gen](./gemini-image-gen) | 通过 ego-browser 驱动已登录的 Gemini（Nano Banana）生成图片，并自动保存完整尺寸文件到当前项目目录 |

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

## gemini-image-gen

与 ChatGPT 版本保持相同的使用方式和本地交付流程，但生图服务改为已登录的 Gemini（Nano Banana）。Agent 会自动打开 Gemini 新会话、发送图片描述、等待生成、下载完整尺寸图片，并以合适的文件名保存到当前项目目录。

### 依赖

- **ego lite 浏览器**：macOS 应用，提供 `ego-browser` CLI。安装方式与 `chatgpt-image-gen` 相同
- **Gemini 账号**：需要在 ego lite 浏览器中已登录 Gemini，且账号所在地区、年龄、组织策略和配额允许生图。未登录时脚本会交还浏览器控制权，手动登录后可继续
- 支持 SKILL.md 的 AI Agent（Claude Code、Kimi Code 等）

### 安装

```bash
# Claude Code / Kimi Code 用户级技能目录
cp -r gemini-image-gen ~/.agents/skills/
# 或项目级
cp -r gemini-image-gen <your-project>/.agents/skills/
```

也可以同时安装两个生图技能。默认图片需求仍可使用 ChatGPT；当需求明确指定 Gemini 或 Nano Banana 时，Agent 会选择 `gemini-image-gen`。

### 使用方法

安装后直接用自然语言指定 Gemini，例如：

- "用 Gemini 帮我生成一张西安旅游攻略图"
- "用 Nano Banana 生成一张 LLM 架构图，保存到项目里"

也可以直接手动运行脚本：

```bash
bash gemini-image-gen/scripts/gen-image.sh "<图片描述>" "<输出文件绝对路径>"
```

脚本退出码：`0` 成功；`42` 需要登录 Gemini；`1` 其他失败（页面状态、生成超时、下载失败等）；`2` 参数错误。每次运行都会输出 JSON 状态和 Gemini 会话地址。

### 工作原理

1. `scripts/gen-image.sh` 通过 `ego-browser nodejs` 在独立任务空间中打开 `gemini.google.com/app` 新会话，复用 ego lite 中的 Google 登录态
2. 使用 Gemini 富文本编辑器发送生图要求，轮询最新响应，直到图片加载完成（最长约 5 分钟）
3. Gemini 页面中的预览图片是临时 `blob:` 地址；脚本点击 Gemini 自带的“下载完整尺寸”按钮，并通过浏览器下载目录取得真实图片文件
4. Agent 验证图片内容后运行技能自带的幂等清理脚本关闭任务空间；重复清理会返回 `already_closed`，不会报错

### 注意事项

- 脚本使用 Gemini 当前页面提供的结构选择器（如 `data-test-id`）；Gemini 页面大幅更新后可能需要调整
- Gemini 生图能力受账号年龄、地区、组织策略和每日配额限制，已登录不等于一定具备生图权限
- 生成耗时通常 1–5 分钟。下载失败时先检查原会话，不要直接重新生成，以免重复消耗配额
