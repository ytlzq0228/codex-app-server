#!/bin/sh
set -eu
base_url=${1:-http://127.0.0.1:8000}
model=${2:-gpt-5.6-sol}
admin_password=${CODEX_GATEWAY_ADMIN_PASSWORD:?set CODEX_GATEWAY_ADMIN_PASSWORD}

html=$(curl -fsS -u "admin:${admin_password}" -X POST \
  -d 'name=e2e-model&scheduling_mode=pooled&pinned_worker_id=' \
  "${base_url}/admin/keys")
key=$(printf '%s' "$html" | sed -n 's|.*<pre>\(cag_[^<]*\)</pre>.*|\1|p')
test -n "$key"
body=$(printf '{"model":"%s","messages":[{"role":"user","content":"Reply exactly MODEL_OK"}]}' "$model")
json=$(curl -fsS -H "Authorization: Bearer ${key}" -H 'Content-Type: application/json' \
  "${base_url}/v1/chat/completions" -d "$body")
printf '%s' "$json" | grep -q '"object":"chat.completion"'
printf '%s' "$json" | grep -q "\"model\":\"${model}\""
printf '%s' "$json" | grep -q 'MODEL_OK'
models=$(curl -fsS -H "Authorization: Bearer ${key}" "${base_url}/v1/models")
printf '%s' "$models" | grep -q "\"id\":\"${model}\""
printf 'model=%s json=ok list=ok\n' "$model"
