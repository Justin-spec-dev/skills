#!/bin/bash
# Generate images with ChatGPT through ego-browser (ego lite) and save them locally.
#
# Usage:
#   gen-image.sh [--all] "<image description>" "<absolute output path>"
#   gen-image.sh [--all] --resume "<conversation url>" "<absolute output path>"
#
# --all downloads every generated image (GPT image generation may render one or
# more per prompt), named <base>-1.<ext>, <base>-2.<ext> ... in descending size
# order. Without it, only the largest image is saved.
#
# --resume skips the prompt/submit step and re-collects images from an existing
# ChatGPT conversation. Use it to recover after image_timeout, or to grab images
# from a chat the user already started. In resume mode only the output path is
# positional.
#
# The saved extension always follows the bytes actually downloaded (PNG, JPEG,
# WebP, or GIF). When the requested extension is a conventional image extension
# it is corrected to match the real format, so a WebP/JPEG response is never
# mislabeled .png.
#
# Exit codes:
#   0  success — JSON {"status":"ok", ...} printed via cliLog
#   42 ChatGPT login required — the caller should hand off the task space to the user
#   1  other failure (output not writable, fill failed, submit button not found,
#      image generation timeout, download/invalid image, partial download,
#      internal error) — always a JSON status
#   2  bad arguments — a JSON {"status":"bad_arguments", ...} line on stdout
set -euo pipefail

usage() {
  echo "usage: gen-image.sh [--all] <description> <output-path>" >&2
  echo "       gen-image.sh [--all] --resume <conversation-url> <output-path>" >&2
}

emit_json() {
  printf '{"status":"%s","error":"%s"}\n' "$1" "$2"
}

# ---- arguments -------------------------------------------------------------
ALL=0
RESUME_URL=""
POS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --all)
      ALL=1
      shift
      ;;
    --resume)
      shift
      if [ "$#" -eq 0 ]; then
        usage
        emit_json bad_arguments "--resume requires a conversation url"
        exit 2
      fi
      RESUME_URL="$1"
      shift
      ;;
    --)
      shift
      while [ "$#" -gt 0 ]; do
        POS+=("$1")
        shift
      done
      ;;
    -*)
      usage
      emit_json bad_arguments "unknown option: $1"
      exit 2
      ;;
    *)
      POS+=("$1")
      shift
      ;;
  esac
done

if [ -n "$RESUME_URL" ]; then
  if [ "${#POS[@]}" -ne 1 ]; then
    usage
    emit_json bad_arguments "resume mode takes exactly one argument (output path)"
    exit 2
  fi
  DESC=""
  OUT="${POS[0]}"
else
  if [ "${#POS[@]}" -ne 2 ]; then
    usage
    emit_json bad_arguments "expected <description> <output path>"
    exit 2
  fi
  DESC="${POS[0]}"
  OUT="${POS[1]}"
fi

export PATH="$HOME/.local/bin:$PATH"

# Serialize runs: every run shares one task space and one tab, so concurrent
# invocations would stomp each other's composer/page. Payload files are still
# PID-suffixed (a stale lock can be reclaimed below), but only one run proceeds.
PAYLOAD_DIR="/tmp/chatgpt-image-gen"
LOCK_DIR="$PAYLOAD_DIR/lock"
mkdir -p "$PAYLOAD_DIR"

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    return 0
  fi
  owner=""
  if [ -f "$LOCK_DIR/pid" ]; then
    owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  fi
  if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
    return 1
  fi
  rm -rf "$LOCK_DIR"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    return 0
  fi
  return 1
}

if ! acquire_lock; then
  emit_json busy "another gen-image.sh run is in progress"
  exit 1
fi
trap 'rm -f "$PAYLOAD_DIR"/prompt.$$.txt "$PAYLOAD_DIR"/out.$$.txt "$PAYLOAD_DIR"/all.$$.txt "$PAYLOAD_DIR"/resume.$$.txt; rm -rf "$LOCK_DIR"' EXIT

# The embedded ego node runtime does not inherit shell env vars, so pass the
# description, output path, and flags through payload files. The PID is
# interpolated into the heredoc below; the JS body deliberately contains no $,
# backtick, or backslash so unquoted-heredoc expansion stays safe.
printf '%s' "$DESC" > "$PAYLOAD_DIR/prompt.$$.txt"
printf '%s' "$OUT" > "$PAYLOAD_DIR/out.$$.txt"
printf '%s' "$ALL" > "$PAYLOAD_DIR/all.$$.txt"
printf '%s' "$RESUME_URL" > "$PAYLOAD_DIR/resume.$$.txt"

ego-browser nodejs <<EOF
const fs = await import('node:fs')
const path = await import('node:path')
const PID = '$$'
const PROMPT = fs.readFileSync('/tmp/chatgpt-image-gen/prompt.' + PID + '.txt', 'utf8')
const OUT = fs.readFileSync('/tmp/chatgpt-image-gen/out.' + PID + '.txt', 'utf8')
const ALL = fs.readFileSync('/tmp/chatgpt-image-gen/all.' + PID + '.txt', 'utf8').trim() === '1'
const RESUME = fs.readFileSync('/tmp/chatgpt-image-gen/resume.' + PID + '.txt', 'utf8')

const report = (status, extra) => cliLog(JSON.stringify({ status, ...extra }))
const fail = (status, extra) => {
  report(status, extra)
  process.exit(status === 'login_required' ? 42 : 1)
}

// Selectors live in one place: when the ChatGPT DOM changes, this is the only
// block to update. The account/profile button only exists once authenticated;
// the anonymous landing page also renders #prompt-textarea, so the composer is
// not a login signal.
const SEL = {
  composer: '#prompt-textarea',
  profile: '[data-testid="accounts-profile-button"], [data-testid^="accounts-profile"]',
  login: '[data-testid="login-button"], [data-testid="signup-button"], a[href*="/auth/login"]',
  submit: "button#composer-submit-button, button.composer-submit-button-color, button[data-testid='composer-submit-button']",
}

const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.webp', '.gif']

// Magic-byte sniffing: trust the bytes, not the CDN content-type or the
// requested extension. ChatGPT's image CDN serves PNG, JPEG, and WebP.
const detectExt = (buf) => {
  if (buf.length > 8 && buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) return '.png'
  if (buf.length > 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) return '.jpg'
  if (buf.length > 12 && buf.slice(0, 4).toString('ascii') === 'RIFF' && buf.slice(8, 12).toString('ascii') === 'WEBP') return '.webp'
  if (buf.length > 6 && buf.slice(0, 3).toString('ascii') === 'GIF') return '.gif'
  return null
}

const main = async () => {
  // Fail fast on a bad output path instead of after minutes of generation.
  try {
    fs.mkdirSync(path.dirname(OUT), { recursive: true })
    fs.accessSync(path.dirname(OUT), fs.constants.W_OK)
  } catch (err) {
    fail('output_not_writable', { path: path.dirname(OUT), error: String(err) })
  }

  await useOrCreateTaskSpace('chatgpt image generation')

  if (RESUME) {
    await openOrReuseTab(RESUME, { wait: true, timeout: 30 })
  } else {
    // chatgpt.com/ always opens a fresh chat when logged in.
    await openOrReuseTab('https://chatgpt.com/', { wait: true, timeout: 30 })
  }

  // Login check: poll for the authenticated profile button rather than judging
  // once, so a slow load is not misread as logout. A visible login/signup
  // control means the session really is gone, so fail fast in that case.
  let loggedIn = false
  const loggedOut = "!!document.querySelector(" + JSON.stringify(SEL.login) + ")"
  const hasProfile = "!!document.querySelector(" + JSON.stringify(SEL.profile) + ")"
  const loginDeadline = Date.now() + 20000
  while (Date.now() < loginDeadline) {
    if (await js(hasProfile)) { loggedIn = true; break }
    if (await js(loggedOut)) break
    await wait(1)
  }
  if (!loggedIn) fail('login_required')

  let conversation = (await pageInfo()).url
  if (!RESUME) {
    await fillInput(SEL.composer, PROMPT)
    const typed = await js("document.querySelector(" + JSON.stringify(SEL.composer) + ").innerText")
    if (!typed || !typed.trim()) fail('fill_failed')

    // Submit button: the id has survived most redesigns but is not guaranteed;
    // fall back to the class used by current ChatGPT DOM and the data-testid.
    const hasSubmit = await js("!!document.querySelector(" + JSON.stringify(SEL.submit) + ")")
    if (!hasSubmit) fail('submit_not_found')

    await click(SEL.submit, { label: 'send image prompt' })

    // The conversation URL is assigned by the SPA after submit; a fixed sleep
    // can race it and record the pre-chat URL. Poll for the /c/<id> URL.
    const convDeadline = Date.now() + 15000
    while (Date.now() < convDeadline) {
      const u = (await pageInfo()).url
      if (u.indexOf('/c/') >= 0) { conversation = u; break }
      conversation = u
      await wait(1)
    }
  }

  // Collect generated images. Candidates are <img> from ChatGPT's image CDNs
  // with a real rendered size. Sorted by area descending so the largest is
  // first (that is what single-image mode saves).
  const COLLECT_SRC = "(() => { const imgs = [...document.querySelectorAll('img')].filter(i => i.src && i.naturalWidth > 300 && (i.src.includes('estuary') || i.src.includes('oaiusercontent') || i.src.includes('oaistatic'))).sort((a, b) => b.naturalWidth * b.naturalHeight - a.naturalWidth * a.naturalHeight); return imgs.map(i => i.src) })()"

  // Generation renders progressively and more than one image can land in the
  // DOM in a single frame, so single-image mode keeps only the first (largest)
  // source regardless of how many appeared in one poll; --all waits until 4 are
  // up or the set stops growing for ~18s (3 polls).
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
  // sanity-check the bytes: a 200 from a CDN can still be an error page. Errors
  // are returned (not thrown) so a partial set can still be reported.
  const download = async (src) => {
    const dl = await js("(async () => { const resp = await fetch(" + JSON.stringify(src) + ", { credentials: 'include' }); if (!resp.ok) return { error: resp.status }; const bytes = new Uint8Array(await resp.arrayBuffer()); let bin = ''; for (let i = 0; i < bytes.length; i += 0x8000) { bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000)) } return { type: resp.headers.get('content-type'), size: bytes.length, b64: btoa(bin) } })()")
    if (dl.error) return { error: 'http_' + dl.error }
    const buf = Buffer.from(dl.b64, 'base64')
    if (buf.length === 0) return { error: 'empty' }
    if (!detectExt(buf)) return { error: 'not_an_image', type: dl.type }
    return { buf }
  }

  const got = []
  const errs = []
  for (const src of chosen) {
    const result = await download(src)
    if (result.buf) got.push(result.buf)
    else errs.push(result)
  }
  if (got.length === 0) {
    fail('download_failed', { reason: errs[0] && errs[0].error, type: errs[0] && errs[0].type, conversation })
  }

  // Name files from the real format so the extension never lies. A conventional
  // requested extension is corrected to the detected one; anything else is kept.
  const reqExt = path.extname(OUT)
  const reqLower = reqExt.toLowerCase()
  const detected = detectExt(got[0])
  const finalExt = (reqExt === '' || IMAGE_EXTS.indexOf(reqLower) >= 0) ? detected : reqExt
  const base = reqExt ? OUT.slice(0, -reqExt.length) : OUT
  const targets = got.length === 1 ? [base + finalExt] : got.map((buf, i) => base + '-' + (i + 1) + finalExt)

  const written = []
  const sizes = []
  try {
    for (let i = 0; i < got.length; i++) {
      fs.writeFileSync(targets[i], got[i])
      written.push(targets[i])
      sizes.push(got[i].length)
    }
  } catch (err) {
    fail('write_failed', { paths: written, error: String(err), conversation })
  }

  if (errs.length) {
    report('partial', { paths: written, bytes: sizes, failed: errs.length, reasons: errs.map(e => e.error), conversation })
    process.exit(1)
  }
  if (written.length === 1) {
    report('ok', { path: written[0], bytes: sizes[0], conversation })
  } else {
    report('ok', { paths: written, bytes: sizes, conversation })
  }
}

main().catch((err) => fail('internal_error', { error: String(err && err.stack ? err.stack : err) }))
EOF
