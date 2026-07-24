#!/bin/bash
# Idempotently close the ego task space used by gemini-image-gen.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

ego-browser nodejs <<'EOF'
const name = 'gemini image generation'
const spaces = await listTaskSpaces()
const task = spaces.find(space => space.name === name || space.taskId === name)

if (!task) {
  cliLog(JSON.stringify({ status: 'already_closed', taskSpace: name }))
} else {
  try {
    const result = await completeTaskSpace(task.id, { keep: false })
    if (!result.done) {
      cliLog(JSON.stringify({ status: 'close_failed', taskSpaceId: task.id, ...result }))
      process.exit(1)
    }
    cliLog(JSON.stringify({ status: 'closed', taskSpaceId: task.id }))
  } catch (error) {
    cliLog(JSON.stringify({ status: 'close_failed', taskSpaceId: task.id, error: String(error) }))
    process.exit(1)
  }
}
EOF
