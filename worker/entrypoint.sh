#!/bin/sh
set -eu
: "${CODEX_WORKER_TOKEN:?CODEX_WORKER_TOKEN is required}"
umask 077
printf '%s' "$CODEX_WORKER_TOKEN" > /run/codex/ws-token
exec codex app-server \
  --listen ws://0.0.0.0:4500 \
  --ws-auth capability-token \
  --ws-token-file /run/codex/ws-token
