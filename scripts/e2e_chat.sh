#!/bin/sh
set -eu
base_url=${1:-http://127.0.0.1:8000}
admin_password=${CODEX_GATEWAY_ADMIN_PASSWORD:?set CODEX_GATEWAY_ADMIN_PASSWORD}

html=$(curl -fsS -u "admin:${admin_password}" -X POST \
  -d 'name=e2e-chat&scheduling_mode=pooled&pinned_worker_id=' \
  "${base_url}/admin/keys")
key=$(printf '%s' "$html" | sed -n 's|.*<pre>\(cag_[^<]*\)</pre>.*|\1|p')
test -n "$key"

json=$(curl -fsS -H "Authorization: Bearer ${key}" -H 'Content-Type: application/json' \
  "${base_url}/v1/chat/completions" \
  -d '{"model":"codex","messages":[{"role":"system","content":"Reply exactly as requested."},{"role":"user","content":"Reply exactly CHAT_JSON_OK"}]}')
printf '%s' "$json" | grep -q '"object":"chat.completion"'
printf '%s' "$json" | grep -q '"role":"assistant"'
printf '%s' "$json" | grep -q 'CHAT_JSON_OK'
printf '%s' "$json" | grep -q '"finish_reason":"stop"'

events=$(curl -fsS -N -H "Authorization: Bearer ${key}" -H 'Content-Type: application/json' \
  "${base_url}/v1/chat/completions" \
  -d '{"model":"codex","messages":[{"role":"user","content":"Reply exactly CHAT_SSE_OK"}],"stream":true,"stream_options":{"include_usage":true}}')
printf '%s' "$events" | grep -q '"object":"chat.completion.chunk"'
printf '%s' "$events" | python3 -c 'import json,sys; lines=(line[6:] for line in sys.stdin if line.startswith("data: {") ); chunks=[json.loads(line) for line in lines]; text="".join((choice.get("delta") or {}).get("content", "") for chunk in chunks for choice in chunk.get("choices", [])); assert "CHAT_SSE_OK" in text'
printf '%s' "$events" | grep -q '"usage":{"prompt_tokens"'
printf '%s' "$events" | grep -q 'data: \[DONE\]'

status=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${key}" -H 'Content-Type: application/json' \
  "${base_url}/v1/chat/completions" -d '{"model":"codex","messages":[{"role":"user","content":"hello"}],"n":2}')
test "$status" = 400
printf 'chat_json=ok chat_sse=ok validation=ok\n'
