---
name: gemini-image-gen
description: Generate an image with Gemini/Nano Banana in the user's logged-in ego lite browser and save the full-size file into the current project directory. Use this skill whenever the user explicitly asks to use Gemini or Nano Banana for image generation - including Gemini 生图、Nano Banana、用 Gemini 画图/做海报/攻略图/插画/封面/信息图. Prefer this skill over chatgpt-image-gen only when the user names Gemini/Nano Banana or specifically wants the logged-in Gemini browser workflow.
---

# gemini-image-gen

Generate an image from a text description with Gemini via ego-browser, download the full-size result, and save it into the project. The bundled script handles the happy path; this skill covers how to call it, handle login, verify the result, and clean up.

## Prerequisites

- ego lite installed and `ego-browser` on PATH (if `ego-browser: command not found`, read the ego-browser skill's `references/install.md` first).
- The user is logged into Gemini inside ego lite and their account can generate images. If not, the script exits with code 42 - follow the login handoff below.

## Runtime notes

The script targets Gemini's current rich-text editor and generated-image controls. It uses structural selectors such as `data-test-id`, not localized button labels. Gemini renders the preview as a temporary `blob:` URL, so the script uses Gemini's own full-size download action instead of fetching the preview image.

The installed ego lite runtime preloads the helper API - `useOrCreateTaskSpace`, `openOrReuseTab`, `fillInput`, `click`, `hover`, `js`, `cdp`, `wait`, `pageInfo`, `cliLog`, `handOffTaskSpace`, `takeOverTaskSpace`, and `completeTaskSpace`. `wait(...)` and `timeout` are in seconds, `cliLog(...)` is the output channel, and the embedded node runtime does not inherit shell environment variables. Run the bundled script as-is rather than rewriting it against another browser API.

## Workflow

1. **Decide the output path.** Save into the current working directory by default. Derive a concise filename from the image description in the user's language, such as `西安旅游攻略图.png`. If the user gave a name or directory, use it. Default extension `.png`.

2. **Run the generator** (usually 1-5 minutes; set the Bash timeout to 420 seconds):

   ```bash
   bash <this-skill-dir>/scripts/gen-image.sh "<image description>" "<absolute output path>"
   ```

   It reuses the task space `gemini image generation`, opens a fresh Gemini chat, asks Gemini to generate the described image, waits for the newest generated image to finish, clicks its full-size download control, and copies the downloaded file to the requested path. The final line of output is a JSON status.

3. **Handle the exit code:**
   - `0` with `{"status":"ok", ...}` - continue to verification.
   - `42` (`login_required`) - hand the browser to the user so they can sign into Gemini:
     ```bash
     ego-browser nodejs <<'EOF'
     const t = await useOrCreateTaskSpace('gemini image generation')
     const h = await handOffTaskSpace(t.id)
     cliLog(JSON.stringify(h))
     EOF
     ```
     Check `h.done`, then tell the user to log into Gemini in the ego lite window and say when done. Only after they confirm, take control back with a new `ego-browser nodejs` heredoc using `takeOverTaskSpace('gemini image generation')`, then rerun the generator.
   - `1` with `login_state_unknown` - inspect the open page before retrying; Gemini loaded but neither a signed-in account link nor a sign-in link was detectable.
   - `1` with `image_timeout` - generation may still be running. Reopen the conversation URL from the output and inspect it visually with `captureScreenshot()`. If the image has appeared, use the newest `download-generated-image-button` to download it; otherwise report the failure.
   - `1` with `download_failed` - the image rendered, but Gemini's full-size download did not complete. Inspect the conversation and retry the visible full-size download once rather than generating another image.
   - `2` - bad arguments; pass a non-empty description and an absolute output path.

4. **Verify the saved image.** Check that it exists, has non-trivial size, and visually inspect it to confirm the content matches the description.

5. **Close the task space** only after verification passed. Use the bundled idempotent cleanup script instead of composing a CLI command or calling `completeTaskSpace` directly:

   ```bash
   bash <this-skill-dir>/scripts/close-task-space.sh
   ```

   `{"status":"closed", ...}` means it closed the task space. `{"status":"already_closed", ...}` is also success: a previous cleanup already removed it. Do not run `ego-browser --help` as a cleanup workaround.

6. **Report** the saved file path and Gemini conversation URL from the script output.

## Notes

- Each run sends exactly one image request. For several images, rerun the script per description so filenames and failure handling stay independent.
- The script opens `https://gemini.google.com/app` for a fresh chat each run. The conversation remains in the user's Gemini history.
- Gemini image generation depends on account age, region, workspace policy, and quota. A signed-in account may still be ineligible.
- Do not retry blindly on failure. Read the JSON status first so a completed image is not regenerated just because its download failed.
