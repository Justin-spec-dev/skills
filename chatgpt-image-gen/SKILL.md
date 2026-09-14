---
name: chatgpt-image-gen
description: Generate an image with ChatGPT (GPT image generation) in the user's logged-in ego lite browser and save the file into the current project directory. Use this skill whenever the user wants an AI-generated picture — 生成图片、画一张图、做海报/攻略图/插画/封面/信息图, "generate an image of...", "make a poster/infographic/illustration" — especially when they give an image description and want the result saved locally for project use. Prefer this skill whenever ego-browser + ChatGPT image generation is the intended pipeline, even if the user doesn't name ChatGPT explicitly.
---

# chatgpt-image-gen

Generate image(s) from a text description with ChatGPT via ego-browser, download them, and save them into the project. The whole happy path is one bundled script; this skill mainly covers how to call it, how to handle login, and how to finish up cleanly.

## Prerequisites

- ego lite installed and `ego-browser` on PATH (if `ego-browser: command not found`, read the ego-browser skill's `references/install.md` first).
- The user is logged into ChatGPT inside ego lite. If not, the script exits with code 42 — follow the login handoff below.

## Runtime notes (important)

The installed ego lite runtime preloads the **helper API** the bundled scripts use: `useOrCreateTaskSpace`, `openOrReuseTab`, `handOffTaskSpace`, `takeOverTaskSpace`, `listTaskSpaces`, `fillInput`, `click`, `js`, `wait`, `pageInfo`, `captureScreenshot`, `cliLog`, `completeTaskSpace` — not the `taskSpace` / `task.page` / `page.*` facades some ego-browser docs describe. Both currently coexist, but these scripts intentionally target the helper API, so run them as-is instead of rewriting them against another API. Also: `wait(...)` and `openOrReuseTab`'s `timeout` are in **seconds**, `cliLog(...)` is the output channel, and the embedded node runtime does **not** inherit shell environment variables (pass data via files, like the script does, or interpolate into the heredoc).

## Workflow

1. **Decide the output path.** Save into the current working directory by default (the user said "保存到项目目录"). Derive a concise filename from the image description in the user's language, e.g. `西安旅游攻略图.png` for "帮我生成一张西安旅游攻略图". If the user gave a name or directory, use that. Default extension `.png` (ChatGPT usually returns PNG, but may return JPEG/WebP — the script corrects the extension to the real format, see Notes).

2. **Run the generator** (takes 1–5 minutes; image generation is slow — set the Bash timeout to 420s to cover the script's worst case):

   ```bash
   bash <this-skill-dir>/scripts/gen-image.sh "<image description>" "<absolute output path>"
   # or, to save the whole grid instead of just the largest image:
   bash <this-skill-dir>/scripts/gen-image.sh --all "<image description>" "<absolute output path>"
   # or, to recover/grab images from an existing conversation (no new prompt):
   bash <this-skill-dir>/scripts/gen-image.sh [--all] --resume "<conversation url>" "<absolute output path>"
   ```

   It reuses the task space `chatgpt image generation`, opens a fresh ChatGPT chat, sends the description, polls until the image(s) appear, downloads them with the page's session cookies, and writes the file(s). Runs are serialized by a lock: if another run is already active the script exits immediately with `{"status":"busy", ...}`. The final line of output is **always a JSON status — including on failure** — so read it before deciding what to do.

3. **Handle the exit code:**
   - `0` with `{"status":"ok", ...}` — `path` (single image) or `paths` (with `--all`) lists the saved file(s). Continue to step 4.
   - `2` (`bad_arguments`) — the invocation was wrong (missing/extra args, unknown option). Fix the command; no browser run happened.
   - `1` (`busy`) — another `gen-image.sh` run holds the lock. Wait for it to finish, then retry.
   - `42` (`login_required`) — hand the browser to the user so they can log in:
     ```bash
     bash <this-skill-dir>/scripts/login.sh handoff
     ```
     The JSON has `done: true` when the handoff succeeded. Then tell the user to log into ChatGPT in the ego lite window and say when done. Only after they confirm, take control back with:
     ```bash
     bash <this-skill-dir>/scripts/login.sh takeover
     ```
     then rerun the generator.
   - `1` (`output_not_writable`) — the output directory could not be created or written; the JSON has `path` and `error`. Pick a writable path and retry. This fails fast, before generation.
   - `1` (`image_timeout`) — generation may still be running. Open the `conversation` URL from the output to check, and if the image has since appeared, recover it without re-prompting:
     ```bash
     bash <this-skill-dir>/scripts/gen-image.sh --resume "<conversation url>" "<absolute output path>"
     ```
     Otherwise report the failure.
   - `1` (`partial`) — some but not all `--all` images downloaded; the JSON lists the saved `paths` plus `failed`/`reasons`. Keep the saved files and report which failed (retry with `--resume` to fetch the rest). Do not treat this as full success.
   - `1` (`download_failed`) — every download failed; the JSON has the HTTP status (`http_*`) or `reason: 'not_an_image'` / `'empty'`. Retry once; if it repeats, report it.
   - `1` (`write_failed`) — images downloaded but could not be written; the JSON has `paths` (already written) and `error`. Usually a disk/permission problem.
   - `1` (`internal_error`) — the automation itself broke (element not found, CDP hiccup, ...); the JSON includes the underlying `error`. Retry once. If the same selector error repeats, the ChatGPT DOM likely changed — update the single `SEL` block at the top of the script's JS (composer, profile, login, submit selectors).
   - `1` (`fill_failed` / `submit_not_found`) — the page rendered but the expected composer elements were missing; retry once, then treat as a DOM change (see `internal_error`).

4. **Verify the saved image(s)** — each saved file exists, has non-trivial size (the script already rejects non-image bytes), and visually inspect them (e.g. read the image file) to confirm the content matches the description.

5. **Close the task space** only after verification passed. Use the bundled idempotent cleanup script instead of composing a CLI command or calling `completeTaskSpace` directly:

   ```bash
   bash <this-skill-dir>/scripts/close-task-space.sh
   ```

   `{"status":"closed", ...}` means it closed the task space. `{"status":"already_closed", ...}` is also success: a previous cleanup already removed it. Do not run `ego-browser --help` as a cleanup workaround.

6. **Report** the saved file path and the ChatGPT conversation URL (from the script output) to the user.

## Notes

- Each run sends exactly one image request. GPT image generation may return one or several images: the default saves only the largest, `--all` saves every one as `<name>-1.<ext>` (largest) through `<name>-N.<ext>`, named in descending size order. For several *different* images, rerun the script per description rather than batching prompts in one chat — separate runs keep filenames and failure handling clean.
- The script probes image magic bytes and writes the file with the extension that matches the bytes. If the requested extension is a conventional image extension (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`), it is corrected to the real format — so a WebP response is never mislabeled `.png`. Files returned in the JSON `path`/`paths` are the authoritative names.
- The script opens `https://chatgpt.com/` fresh each run, so every image starts a new chat. `--resume` instead reuses the given conversation URL. The ChatGPT conversation remains in the user's history either way.
- Runs share one task space and tab, so they are serialized: do not launch concurrent `gen-image.sh` runs (the second exits `busy`).
- Do not retry blindly on transient failure: read the JSON status first — it distinguishes login, busy, timeout, download, partial, write, and internal problems.
