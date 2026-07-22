#!/bin/bash
# Generate an image with ChatGPT through ego-browser (ego lite) and save it locally.
#
# Usage: gen-image.sh "<image description>" "<absolute output path>"
#
# Exit codes:
#   0  success — JSON {"status":"ok", ...} printed via cliLog
#   42 ChatGPT login required — the caller should hand off the task space to the user
#   1  other failure (fill failed, image generation timeout, download failed)
#   2  bad arguments
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: gen-image.sh <description> <output-path>" >&2
  exit 2
fi

export PATH="$HOME/.local/bin:$PATH"

# The embedded ego node runtime does not inherit shell env vars, so pass the
# description and output path through payload files instead of the environment.
PAYLOAD_DIR="/tmp/chatgpt-image-gen"
mkdir -p "$PAYLOAD_DIR"
printf '%s' "$1" > "$PAYLOAD_DIR/prompt.txt"
printf '%s' "$2" > "$PAYLOAD_DIR/out.txt"

ego-browser nodejs <<'EOF'
const fs = await import('node:fs')
const PROMPT = fs.readFileSync('/tmp/chatgpt-image-gen/prompt.txt', 'utf8')
const OUT = fs.readFileSync('/tmp/chatgpt-image-gen/out.txt', 'utf8').trim()

const report = (status, extra) => cliLog(JSON.stringify({ status, ...extra }))
const fail = (status, extra) => {
  report(status, extra)
  process.exit(status === 'login_required' ? 42 : 1)
}

await useOrCreateTaskSpace('chatgpt image generation')

// chatgpt.com/ always opens a fresh chat when logged in.
await openOrReuseTab('https://chatgpt.com/', { wait: true, timeout: 30 })
await wait(3)

if (!(await js(`!!document.querySelector('#prompt-textarea')`))) {
  fail('login_required')
}

await fillInput('#prompt-textarea', PROMPT)
const typed = await js(`document.querySelector('#prompt-textarea').innerText`)
if (!typed || !typed.trim()) fail('fill_failed')

await click('button#composer-submit-button', { label: 'send image prompt' })
await wait(5)
const conversation = (await pageInfo()).url

// Poll for the generated image. While ChatGPT is still rendering, the message
// shows an animated placeholder with no <img>; the final image appears as an
// <img> served from the estuary/oaiusercontent endpoints. Allow ~4.5 minutes.
const deadline = Date.now() + 270000
let src = null
while (Date.now() < deadline) {
  src = await js(String.raw`(() => {
    const imgs = [...document.querySelectorAll('img')]
      .filter(i => i.src && i.naturalWidth > 300 &&
        (i.src.includes('estuary') || i.src.includes('oaiusercontent')))
      .sort((a, b) => b.naturalWidth * b.naturalHeight - a.naturalWidth * a.naturalHeight)
    return imgs.length ? imgs[0].src : null
  })()`)
  if (src) break
  await wait(6)
}
if (!src) fail('image_timeout', { conversation })

// Download inside the page context so ChatGPT's session cookies apply.
const dl = await js(`(async () => {
  const resp = await fetch(${JSON.stringify(src)}, { credentials: 'include' })
  if (!resp.ok) return { error: resp.status }
  const bytes = new Uint8Array(await resp.arrayBuffer())
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000))
  }
  return { type: resp.headers.get('content-type'), size: bytes.length, b64: btoa(bin) }
})()`)
if (dl.error) fail('download_failed', { http: dl.error, conversation })

fs.writeFileSync(OUT, Buffer.from(dl.b64, 'base64'))
report('ok', { path: OUT, bytes: dl.size, type: dl.type, conversation })
EOF
