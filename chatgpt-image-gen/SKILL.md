---
name: chatgpt-image-gen
description: Generate an image with ChatGPT (GPT image generation) in the user's logged-in ego lite browser and save the file into the current project directory. Use this skill whenever the user wants an AI-generated picture — 生成图片、画一张图、做海报/攻略图/插画/封面/信息图, "generate an image of...", "make a poster/infographic/illustration" — especially when they give an image description and want the result saved locally for project use. Prefer this skill whenever ego-browser + ChatGPT image generation is the intended pipeline, even if the user doesn't name ChatGPT explicitly.
---

# chatgpt-image-gen

Generate an image from a text description with ChatGPT via ego-browser, download it, and save it into the project. The whole happy path is one bundled script; this skill mainly covers how to call it, how to handle login, and how to finish up cleanly.

## Prerequisites

- ego lite installed and `ego-browser` on PATH (if `ego-browser: command not found`, read the ego-browser skill's `references/install.md` first).
- The user is logged into ChatGPT inside ego lite. If not, the script exits with code 42 — follow the login handoff below.

## Runtime notes (important)

The installed ego lite runtime (0.4.4.x) preloads the **helper API** — `useOrCreateTaskSpace`, `openOrReuseTab`, `fillInput`, `click`, `js`, `wait`, `pageInfo`, `cliLog`, `completeTaskSpace` — not the `page` / `browser` / `taskSpaces` facades some ego-browser docs describe. Also: `wait(...)` and `timeout` are in **seconds**, `cliLog(...)` is the output channel, and the embedded node runtime does **not** inherit shell environment variables (pass data via files, like the script does, or interpolate into the heredoc). The bundled script already follows these rules — run it as-is instead of rewriting it against another API.

## Workflow

1. **Decide the output path.** Save into the current working directory by default (the user said "保存到项目目录"). Derive a concise filename from the image description in the user's language, e.g. `西安旅游攻略图.png` for "帮我生成一张西安旅游攻略图". If the user gave a name or directory, use that. Default extension `.png` (ChatGPT returns PNG).

2. **Run the generator** (takes 1–5 minutes; image generation is slow — set the Bash timeout to 300s):

   ```bash
   bash <this-skill-dir>/scripts/gen-image.sh "<image description>" "<absolute output path>"
   ```

   It reuses the task space `chatgpt image generation`, opens a fresh ChatGPT chat, sends the description, polls until the image appears, downloads it with the page's session cookies, and writes the file. Final line of output is a JSON status.

3. **Handle the exit code:**
   - `0` with `{"status":"ok", ...}` — continue to step 4.
   - `42` (`login_required`) — hand the browser to the user so they can log in:
     ```
     const t = await useOrCreateTaskSpace('chatgpt image generation')
     const h = await handOffTaskSpace(t.id)   // check h.done
     ```
     Tell the user to log into ChatGPT in the ego lite window and say when done. Only after they confirm, run `await takeOverTaskSpace(t.id)` in a new heredoc, then rerun the script.
   - `1` with `image_timeout` — generation may still be running; open the conversation URL from the output, check visually with `captureScreenshot()`, and if the image has since appeared, fetch it manually (the poll + download snippet in the script is the reference). Otherwise report the failure.

4. **Verify the saved image** — check it exists, has non-trivial size, and visually inspect it (e.g. read the image file) to confirm the content matches the description.

5. **Close the task space** in a dedicated final heredoc, only after verification passed:

   ```
   const c = await completeTaskSpace('chatgpt image generation', { keep: false })
   // check c.done === true
   ```

6. **Report** the saved file path and the ChatGPT conversation URL (from the script output) to the user.

## Notes

- Each run sends exactly one image request and waits for it. If the user wants several images, rerun the script per description rather than batching prompts in one chat — separate runs keep filenames and failure handling clean.
- The script opens `https://chatgpt.com/` fresh each run, so every image starts a new chat. The ChatGPT conversation remains in the user's history.
- Do not retry blindly on transient failure: read the JSON status first, it distinguishes login, timeout, and download problems.
