#!/bin/bash
# Hand the chatgpt-image-gen task space to the user for interactive login, or
# take it back once they are done. Keeps the login handoff off the SKILL's
# inline heredocs so the flow is one command either way.
#
# Usage:
#   login.sh handoff    # give the user control of the browser (start login)
#   login.sh takeover   # take control back after the user confirms login
#
# Both print a single JSON status line and exit 0 on success, 1 on failure.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

ACTION="${1:-}"
if [ "$ACTION" != "handoff" ] && [ "$ACTION" != "takeover" ]; then
  printf '{"status":"bad_arguments","error":"usage: login.sh handoff|takeover"}\n'
  exit 2
fi

if [ "$ACTION" = "handoff" ]; then
  ego-browser nodejs <<'EOF'
const t = await useOrCreateTaskSpace('chatgpt image generation')
const h = await handOffTaskSpace(t.id)
cliLog(JSON.stringify({ status: 'handoff', taskSpaceId: t.id, done: !!(h && h.done) }))
if (!h || !h.done) process.exit(1)
EOF
else
  ego-browser nodejs <<'EOF'
const name = 'chatgpt image generation'
const spaces = await listTaskSpaces()
const found = spaces.find((space) => space.name === name || space.taskId === name)
const result = await takeOverTaskSpace(found ? found.id : name)
const taskSpaceId = (result && (result.id || result.spaceId)) || (found && found.id) || undefined
cliLog(JSON.stringify({ status: 'takeover', taskSpaceId }))
EOF
fi
