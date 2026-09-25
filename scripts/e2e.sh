#!/bin/sh
set -eu
base_url=${1:-http://127.0.0.1:8000}
admin_password=${CODEX_GATEWAY_ADMIN_PASSWORD:?set CODEX_GATEWAY_ADMIN_PASSWORD}

html=$(curl -fsS -u "admin:${admin_password}" -X POST \
  -d 'name=e2e-persistent&scheduling_mode=pooled&pinned_worker_id=' \
  "${base_url}/admin/keys")
key=$(printf '%s' "$html" | sed -n 's|.*<pre>\(cag_[^<]*\)</pre>.*|\1|p')
test -n "$key"

first=$(curl -fsS -H "Authorization: Bearer ${key}" -H 'Content-Type: application/json' \
  "${base_url}/v1/responses" -d '{"model":"codex","input":"Remember secret BLUE-482. Reply exactly FIRST_OK"}')
response_id=$(printf '%s' "$first" | grep -o '"id":"resp_[^"]*"' | head -1 | cut -d'"' -f4)
printf '%s' "$first" | grep -q 'FIRST_OK'

second=$(curl -fsS -H "Authorization: Bearer ${key}" -H 'Content-Type: application/json' \
  "${base_url}/v1/responses" -d "{\"model\":\"codex\",\"previous_response_id\":\"${response_id}\",\"input\":\"What secret did I ask you to remember? Reply only the secret.\"}")
printf '%s' "$second" | grep -q 'BLUE-482'

events=$(curl -fsS -N -H "Authorization: Bearer ${key}" -H 'Content-Type: application/json' \
  "${base_url}/v1/responses" -d '{"model":"codex","input":"Reply exactly SSE_OK","stream":true}')
printf '%s' "$events" | grep -q 'event: response.created'
printf '%s' "$events" | grep -q 'event: response.output_text.delta'
printf '%s' "$events" | grep -q 'event: response.completed'

printf 'json=ok continuation=ok sse=ok\n'
