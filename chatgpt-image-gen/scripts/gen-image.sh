#!/bin/bash
# Generate images with ChatGPT through ego-browser (ego lite) and save them locally.
#
# Usage:
#   gen-image.sh "<image description>" "<absolute output path>"
#   gen-image.sh --all "<image description>" "<absolute output path>"
#
# --all downloads every generated image (GPT-4o usually renders 4 per prompt),
# named <base>-1.png, <base>-2.png ... in descending size order. Without it,
# only the largest image is saved, exactly as before.
#
# Exit codes:
#   0  success — JSON {"status":"ok", ...} printed via cliLog
#   42 ChatGPT login required — the caller should hand off the task space to the user
#   1  other failure (fill failed, submit button not found, image generation
#      timeout, download/invalid image, internal error) — always a JSON status
#   2  bad arguments
set -euo pipefail

usage() {
  echo "usage: gen-image.sh [--all] <description> <output-path>" >&2
}

ALL=0
ARGS=()
for arg in "$@"; do
  if [ "$arg" = "--all" ]; then
    ALL=1
  else
    ARGS+=("$arg")
  fi
done

if [ "${#ARGS[@]}" -ne 2 ]; then
  usage
  exit 2
fi
DESC="${ARGS[0]}"
OUT="${ARGS[1]}"

export PATH="$HOME/.local/bin:$PATH"

# The embedded ego node runtime does not inherit shell env vars, so pass the
# description, output path, and flags through payload files. Files are
# PID-suffixed so two concurrent runs do not clobber each other; the PID is
# interpolated into the heredoc below (the JS body contains no $, backtick, or
# backslash, so unquoted heredoc expansion is safe).
PAYLOAD_DIR="/tmp/chatgpt-image-gen"
mkdir -p "$PAYLOAD_DIR"
printf '%s' "$DESC" > "$PAYLOAD_DIR/prompt.$$.txt"
printf '%s' "$OUT" > "$PAYLOAD_DIR/out.$$.txt"
printf '%s' "$ALL" > "$PAYLOAD_DIR/all.$$.txt"
trap 'rm -f "$PAYLOAD_DIR"/prompt.$$.txt "$PAYLOAD_DIR"/out.$$.txt "$PAYLOAD_DIR"/all.$$.txt' EXIT

ego-browser nodejs <<EOF
const fs = await import('node:fs')
const path = await import('node:path')
const PID = '$$'
const PROMPT = fs.readFileSync('/tmp/chatgpt-image-gen/prompt.' + PID + '.txt', 'utf8')
const OUT = fs.readFileSync('/tmp/chatgpt-image-gen/out.' + PID + '.txt', 'utf8').trim()
const ALL = fs.readFileSync('/tmp/chatgpt-image-gen/all.' + PID + '.txt', 'utf8').trim() === '1'

const report = (status, extra) => cliLog(JSON.stringify({ status, ...extra }))
const fail = (status, extra) => {
  report(status, extra)
  process.exit(status === 'login_required' ? 42 : 1)
}

// Every failure path goes through report() so callers always get a JSON status
// line; a bare exception here would leave the caller guessing.
const main = async () => {
  await useOrCreateTaskSpace('chatgpt image generation')

  // chatgpt.com/ always opens a fresh chat when logged in.
  await openOrReuseTab('https://chatgpt.com/', { wait: true, timeout: 30 })

  // Login check: the composer only renders once the session is authenticated,
  // but the first paint can be slow (redirects, slow network). Poll instead of
  // judging once after a fixed sleep, so a slow load is not misread as logout.
  let loggedIn = false
  const loginDeadline = Date.now() + 20000
  while (Date.now() < loginDeadline) {
    if (await js("!!document.querySelector('#prompt-textarea')")) { loggedIn = true; break }
    await wait(1)
  }
  if (!loggedIn) fail('login_required')

  await fillInput('#prompt-textarea', PROMPT)
  const typed = await js("document.querySelector('#prompt-textarea').innerText")
  if (!typed || !typed.trim()) fail('fill_failed')

  // Submit button: the id has survived most redesigns but is not guaranteed;
  // fall back to the class used by current ChatGPT DOM and the data-testid.
  const SUBMIT_SEL = "button#composer-submit-button, button.composer-submit-button-color, button[data-testid='composer-submit-button']"
  const hasSubmit = await js("!!document.querySelector(" + JSON.stringify(SUBMIT_SEL) + ")")
  if (!hasSubmit) fail('submit_not_found')

  await click(SUBMIT_SEL, { label: 'send image prompt' })
  await wait(5)
  const conversation = (await pageInfo()).url

  // Collect generated images. Candidates are <img> from ChatGPT's image CDNs
  // with a real rendered size. Sorted by area descending so the largest is
  // first (that is what single-image mode saves).
  const COLLECT_SRC = "(() => { const imgs = [...document.querySelectorAll('img')].filter(i => i.src && i.naturalWidth > 300 && (i.src.includes('estuary') || i.src.includes('oaiusercontent') || i.src.includes('oaistatic'))).sort((a, b) => b.naturalWidth * b.naturalHeight - a.naturalWidth * b.naturalHeight); return imgs.map(i => i.src) })()"

  // GPT-4o renders its grid progressively, but several images can also land in
  // the DOM in the same frame, so single-image mode must keep only the first
  // (largest) source regardless of how many appeared in one poll; --all waits
  // until 4 are up or the set stops growing for ~18s (3 polls).
  const deadline = Date.now() + 270000
  let srcs = []
  let stableRounds = 0
  while (Date.now() < deadline) {
    const cur = await js(COLLECT_SRC)
    if (cur.length > srcs.length) {
      srcs = cur
      stableRounds = 0
      if (!ALL) break
      if (srcs.length >= 4) break
    } else {
      stableRounds++
      if (ALL && srcs.length >= 1 && stableRounds >= 3) break
    }
    await wait(6)
  }
  if (srcs.length === 0) fail('image_timeout', { conversation })
  const chosen = ALL ? srcs : srcs.slice(0, 1)

  // Download inside the page context so ChatGPT's session cookies apply, then
  // sanity-check the bytes: a 200 from a CDN can still be an error page.
  const download = async (src) => {
    const dl = await js("(async () => { const resp = await fetch(" + JSON.stringify(src) + ", { credentials: 'include' }); if (!resp.ok) return { error: resp.status }; const bytes = new Uint8Array(await resp.arrayBuffer()); let bin = ''; for (let i = 0; i < bytes.length; i += 0x8000) { bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000)) } return { type: resp.headers.get('content-type'), size: bytes.length, b64: btoa(bin) } })()")
    if (dl.error) fail('download_failed', { http: dl.error, conversation })
    const buf = Buffer.from(dl.b64, 'base64')
    const isPng = buf.length > 8 && buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47
    const isJpeg = buf.length > 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff
    if (!isPng && !isJpeg) fail('download_failed', { reason: 'not_an_image', type: dl.type, conversation })
    return buf
  }

  // Multi-image naming: out.png -> out-1.png (largest) ... ; a single saved
  // image keeps the exact requested path for backward compatibility.
  fs.mkdirSync(path.dirname(OUT), { recursive: true })
  const ext = path.extname(OUT)
  const base = ext ? OUT.slice(0, -ext.length) : OUT
  const targets = chosen.length === 1 ? [OUT] : chosen.map((s, i) => base + '-' + (i + 1) + (ext || '.png'))
  const sizes = []
  for (let i = 0; i < chosen.length; i++) {
    const buf = await download(chosen[i])
    fs.writeFileSync(targets[i], buf)
    sizes.push(buf.length)
  }

  if (targets.length === 1) {
    report('ok', { path: OUT, bytes: sizes[0], conversation })
  } else {
    report('ok', { paths: targets, bytes: sizes, conversation })
  }
}

main().catch((err) => fail('internal_error', { error: String(err && err.stack ? err.stack : err) }))
EOF
