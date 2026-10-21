# Worker availability and conversation recovery

The default quota reserve is 20% (`CODEX_GATEWAY_WORKER_QUOTA_RESERVE_PERCENT=20`).
A known five-hour or weekly usage of **80% or more** excludes the Worker from
new requests and conversation continuation. Monitoring polls usage every five
minutes; Codex also reads account quota immediately before starting or resuming
a thread. Missing upstream usage is unknown, not a measured zero.

Automatic continuation uses the verified history submitted by the client. If the
original Worker is unavailable, the gateway selects another eligible Worker and
starts a new provider thread with that history. Explicit `previous_response_id`
requests can also rebuild when their full history matches the execution checkpoint.
Delta-only requests do not contain enough context to reconstruct a lost thread.
Pinned keys remain restricted to their configured Worker.

Safe failures exclude each failed Worker for the rest of the request. Quota
failures put the account into cooldown immediately; a successful inference probe
does not override that failure. The gateway does not replay a turn after model
content, tool requests, or other execution effects have been observed. A pending
tool result belongs to its original execution: if that Worker becomes unavailable,
send a new user turn with history and confirmed tool results. Never repeat an
external tool merely because the provider connection failed.

| Error | Client handling |
| --- | --- |
| `provider_quota_exhausted` | Account quota is unavailable; honor retry timing. |
| `worker_unavailable` | No eligible replacement remains; retry later. |
| `model_capacity_exceeded` | Upstream model is busy; retry with backoff. |
| `worker_capacity_exceeded` | Worker admission capacity is full; retry with backoff. |
| `input_too_long` | Reduce or compact history; do not resend identical oversized input. |
| `conversation_busy` | Wait for the conversation's active request to finish. |
| `tool_worker_unavailable` / `client_tool_call_unavailable` | Start a new user turn with history and confirmed results; do not replay tools. |
| `claude_process_timeout` | Execution timed out; completion may be uncertain. |
| `claude_process_incomplete` | Process exited without a terminal result; the message includes its exit code. |

Use models returned by `/v1/models` and the supported tool/parameter schema.
Invalid images, unsupported parameters and stale tool bindings require correcting
the request, not retrying another Worker. Build gateway images with
`--build-arg CODEX_GATEWAY_RELEASE=<release-id>` so diagnostics identify the release.
Cross-node diagnostics retain source exception types and stack locations without
exception messages or local variables.
