#!/bin/bash
# Generate an image with Gemini through ego-browser (ego lite) and save it locally.
#
# Usage: gen-image.sh "<image description>" "<absolute output path>"
#
# Exit codes:
#   0  success - JSON {"status":"ok", ...} printed via cliLog
#   42 Gemini login required - the caller should hand off the task space
#   1  other failure (page state, fill, generation, or download)
#   2  bad arguments
set -euo pipefail

if [ $# -ne 2 ] || [ -z "$1" ] || [[ "$2" != /* ]]; then
  echo "usage: gen-image.sh <description> <absolute-output-path>" >&2
  exit 2
fi

export PATH="$HOME/.local/bin:$PATH"

# The embedded ego node runtime does not inherit shell env vars, so pass the
# description and output path through payload files instead of the environment.
PAYLOAD_DIR="/tmp/gemini-image-gen"
mkdir -p "$PAYLOAD_DIR"
printf '%s' "$1" > "$PAYLOAD_DIR/prompt.txt"
printf '%s' "$2" > "$PAYLOAD_DIR/out.txt"

ego-browser nodejs <<'EOF'
const fs = await import('node:fs')
const path = await import('node:path')

const PAYLOAD_DIR = '/tmp/gemini-image-gen'
const DOWNLOAD_DIR = path.join(PAYLOAD_DIR, 'downloads')
const PROMPT = fs.readFileSync(path.join(PAYLOAD_DIR, 'prompt.txt'), 'utf8').trim()
const OUT = fs.readFileSync(path.join(PAYLOAD_DIR, 'out.txt'), 'utf8').trim()
const IMAGE_PROMPT = 'Generate an image based on this description. Return an image, not just text:\n\n' + PROMPT

const report = (status, extra = {}) => cliLog(JSON.stringify({ status, ...extra }))
const fail = (status, extra = {}) => {
  report(status, extra)
  process.exit(status === 'login_required' ? 42 : 1)
}

fs.rmSync(DOWNLOAD_DIR, { recursive: true, force: true })
fs.mkdirSync(DOWNLOAD_DIR, { recursive: true })

await useOrCreateTaskSpace('gemini image generation')
await openOrReuseTab('https://gemini.google.com/app', { wait: true, timeout: 30 })
await wait(4)

const page = await pageInfo()
const authState = await js(String.raw`(() => {
  if (location.hostname === 'accounts.google.com' ||
      document.querySelector('a[href*="accounts.google.com/ServiceLogin"]')) {
    return 'signed-out'
  }
  if (document.querySelector('a[href*="accounts.google.com/SignOutOptions"]')) {
    return 'signed-in'
  }
  return 'unknown'
})()`)

if (authState === 'signed-out') fail('login_required', { url: page.url })
if (authState !== 'signed-in') fail('login_state_unknown', { url: page.url })

const editorSelector = 'rich-textarea .ql-editor[contenteditable="true"][role="textbox"]'
const editorExists = await js(`!!document.querySelector(${JSON.stringify(editorSelector)})`)
if (!editorExists) fail('composer_not_found', { url: page.url })

const baseline = await js(String.raw`(() => ({
  responses: document.querySelectorAll('model-response').length,
  images: document.querySelectorAll('generated-image').length,
}))()`)

await fillInput(editorSelector, IMAGE_PROMPT)
const typed = await js(`document.querySelector(${JSON.stringify(editorSelector)})?.innerText || ''`)
if (!typed || !typed.includes(PROMPT)) fail('fill_failed', { url: page.url })

const sendSelector = '[data-test-id="send-button-container"] gem-icon-button.send-button.submit[aria-disabled="false"] button'
const canSend = await js(`!!document.querySelector(${JSON.stringify(sendSelector)})`)
if (!canSend) fail('send_unavailable', { url: page.url })

await click(sendSelector, { label: 'send image prompt' })
await wait(4)
const conversation = (await pageInfo()).url

const deadline = Date.now() + 300000
let lastState = null
while (Date.now() < deadline) {
  lastState = await js(`(() => {
    const responses = [...document.querySelectorAll('model-response')]
    const latest = responses.length > ${baseline.responses} ? responses[responses.length - 1] : null
    const generated = latest ? [...latest.querySelectorAll('generated-image')] : []
    const newest = generated[generated.length - 1] || null
    const img = newest?.querySelector('img.image') || null
    const overlay = newest?.querySelector('[data-test-id="image-loading-overlay"]') || null
    const download = newest?.querySelector(
      'download-generated-image-button [data-test-id="download-generated-image-button"] button, ' +
      'button[data-test-id="download-generated-image-button"], ' +
      'download-generated-image-button button'
    ) || null
    const generating = !!document.querySelector(
      '[data-test-id="send-button-container"] gem-icon-button.send-button.stop, ' +
      '[data-test-id="send-button-container"] mat-icon[fonticon="stop"], ' +
      '[data-test-id="stop-button"]'
    )
    const imageReady = !!img && img.complete && img.naturalWidth > 300 &&
      img.classList.contains('loaded') && (!overlay || overlay.classList.contains('done-generating'))
    return {
      responseCount: responses.length,
      generatedCount: generated.length,
      totalImageCount: document.querySelectorAll('generated-image').length,
      generating,
      imageReady,
      hasDownload: !!download,
      width: img?.naturalWidth || 0,
      height: img?.naturalHeight || 0,
      responseText: (latest?.innerText || '').slice(0, 500),
    }
  })()`)

  if (lastState.imageReady && lastState.hasDownload) break
  await wait(5)
}

if (!lastState?.imageReady || !lastState?.hasDownload) {
  fail('image_timeout', { conversation, detail: lastState })
}

const tagged = await js(String.raw`(() => {
  document.querySelectorAll('[data-ego-gemini-target]').forEach(el =>
    el.removeAttribute('data-ego-gemini-target'))
  const responses = [...document.querySelectorAll('model-response')]
  const latest = responses[responses.length - 1]
  const generated = latest ? [...latest.querySelectorAll('generated-image')] : []
  const newest = generated[generated.length - 1]
  const img = newest?.querySelector('img.image')
  const button = newest?.querySelector(
    'download-generated-image-button [data-test-id="download-generated-image-button"] button, ' +
    'button[data-test-id="download-generated-image-button"], ' +
    'download-generated-image-button button'
  )
  if (!img || !button) return false
  img.setAttribute('data-ego-gemini-target', 'image')
  button.setAttribute('data-ego-gemini-target', 'download')
  return true
})()`)

if (!tagged) fail('download_control_not_found', { conversation })

try {
  // Page-scoped download behavior works reliably in ego task spaces. The
  // browser-scoped command may succeed while targeting a different context.
  await cdp('Page.setDownloadBehavior', {
    behavior: 'allow',
    downloadPath: DOWNLOAD_DIR,
  })
} catch (pageError) {
  try {
    await cdp('Browser.setDownloadBehavior', {
      behavior: 'allow',
      downloadPath: DOWNLOAD_DIR,
      eventsEnabled: true,
    })
  } catch (browserError) {
    fail('download_setup_failed', {
      conversation,
      error: String(pageError || browserError),
    })
  }
}

await hover('[data-ego-gemini-target="image"]', { label: 'reveal image download' })
await click('[data-ego-gemini-target="download"]', { label: 'download full size image' })

const downloadDeadline = Date.now() + 60000
let downloaded = null
let previousSize = -1
let stableChecks = 0
while (Date.now() < downloadDeadline) {
  const files = fs.readdirSync(DOWNLOAD_DIR)
    .filter(name => !name.endsWith('.crdownload') && !name.endsWith('.tmp'))
    .map(name => {
      const filePath = path.join(DOWNLOAD_DIR, name)
      const stat = fs.statSync(filePath)
      return { filePath, size: stat.isFile() ? stat.size : 0, mtime: stat.mtimeMs }
    })
    .filter(file => file.size > 1024)
    .sort((a, b) => b.mtime - a.mtime)

  const candidate = files[0] || null
  if (candidate && candidate.size === previousSize) {
    stableChecks += 1
  } else {
    stableChecks = 0
    previousSize = candidate?.size || -1
  }
  if (candidate && stableChecks >= 2) {
    downloaded = candidate
    break
  }
  await wait(1)
}

if (!downloaded) fail('download_failed', { conversation })

fs.mkdirSync(path.dirname(OUT), { recursive: true })
fs.copyFileSync(downloaded.filePath, OUT)

const bytes = fs.statSync(OUT).size
const header = fs.readFileSync(OUT).subarray(0, 12)
let type = 'application/octet-stream'
if (header.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
  type = 'image/png'
} else if (header[0] === 0xff && header[1] === 0xd8) {
  type = 'image/jpeg'
} else if (header.subarray(0, 4).toString() === 'RIFF' && header.subarray(8, 12).toString() === 'WEBP') {
  type = 'image/webp'
}

if (!type.startsWith('image/')) fail('download_not_image', { conversation, bytes, type })

fs.rmSync(DOWNLOAD_DIR, { recursive: true, force: true })
report('ok', {
  path: OUT,
  bytes,
  type,
  width: lastState.width,
  height: lastState.height,
  conversation,
})
EOF
