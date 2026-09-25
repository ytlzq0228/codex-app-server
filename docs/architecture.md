# Architecture and implementation plan

## Decisions

1. Public HTTP handlers depend on a `CompletionBackend`; Codex protocol changes stay inside its adapter.
2. The gateway never receives a Docker socket. A restricted worker-manager will validate database ownership and Docker labels.
3. A worker is an isolation boundary; a Key-to-worker WebSocket is a connection boundary; threads are conversation boundaries.
4. Request and response content is transient and never enters database records or application logs.

## Implemented boundaries

1. FastAPI exposes the text subset of `/v1/responses`, `/v1/models`, JSON output, and semantic SSE events.
2. PostgreSQL stores API Keys, worker inventory, response-to-thread bindings, and metadata-only usage records.
3. A persistent app-server connection is keyed by `(api_key_id, worker_id)` and initialized once. Turns on a connection are serialized because the pinned CLI protocol does not attach a routing key to every notification.
4. Pooled Keys select an enabled healthy worker; pinned Keys and continued responses remain on their assigned worker.
5. A separate internal manager owns the Docker socket. It creates and removes only labeled containers and prepares a mode-0700 workspace for each Key. The gateway never mounts worker files or the Docker socket.
6. The worker is non-root, read-only, capability-dropped, resource-limited, and has no host filesystem mount. Approvals are declined and sandbox network access is disabled per turn.

TLS termination remains an operator concern because certificate issuance requires a deployment hostname. A Caddy example and systemd unit are included under `deploy/`.

## App-server request flow

Select a worker; establish/reuse `(api_key_id, worker_id)` connection; initialize once; start/resume a thread; start the turn; translate events; persist only metadata. On disconnect, mark the turn indeterminate and never blindly replay it.
