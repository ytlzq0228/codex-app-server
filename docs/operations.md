# Operations

## Install

1. Copy the repository to `/opt/codex-app-server` and create `.env` from `.env.example`.
2. Replace every value containing `development` or `change-this`; set `CODEX_GATEWAY_DEV_API_KEY=` outside development.
3. Install `deploy/codex-gateway.service` in `/etc/systemd/system/`, then run `systemctl daemon-reload && systemctl enable --now codex-gateway`.
4. Put Caddy or another TLS reverse proxy in front of port 8000. The example preserves SSE flushing.

## Backup and restore

Back up the PostgreSQL database and each `*-codex-home` Docker volume. Workspace volumes are disposable. Restore the database and account volumes together so thread bindings still point to the correct worker identity.

## Worker lifecycle

Only the internal worker-manager receives the Docker socket. It refuses to remove containers without `io.codex-gateway.managed=true`. Draining a worker removes it from new scheduling; existing response bindings remain pinned to it. Probe the account after device-code login before enabling traffic.

Connection failures, logged-out accounts, and subscription-limit errors quarantine a worker automatically. Pooled stateless requests may retry once on another worker only when the failed turn is known not to have started. Pinned keys and `previous_response_id` continuations never move between workers. The recovery loop probes quarantined workers after their cooldown and returns them to the pool only when the app-server is reachable and the account is logged in. Configure the sweep and cooldowns with `CODEX_GATEWAY_WORKER_RECOVERY_INTERVAL_SECONDS`, `CODEX_GATEWAY_WORKER_FAILURE_COOLDOWN_SECONDS`, and `CODEX_GATEWAY_WORKER_LIMIT_COOLDOWN_SECONDS`.

Both manual probes and automatic recovery probes run a minimal real Codex turn. `account/read` is used only to establish identity; a worker returns to `ready` only after the turn completes successfully. Probes therefore consume a very small amount of subscription usage.

API request sessions release their PostgreSQL transaction before entering a potentially long Codex turn. Configure SQLAlchemy capacity with `CODEX_GATEWAY_DATABASE_POOL_SIZE`, `CODEX_GATEWAY_DATABASE_MAX_OVERFLOW`, and `CODEX_GATEWAY_DATABASE_POOL_TIMEOUT_SECONDS`. An `idle` PostgreSQL connection is a reusable pooled connection; `idle in transaction` during inference indicates a regression.

The admin console provides paginated access to all request records. Deleting an API key is a soft delete: authentication stops immediately and active response bindings are removed, while usage records remain available for audit. Removing a single active session deletes every `previous_response_id` binding for that Key/thread pair, so subsequent continuation attempts return `previous_response_not_found`.

Responses and identified Chat Completions conversations retain response-to-Thread bindings without an idle TTL. Both interfaces can automatically resume after validating client identity, full history and configuration. The Key active-session page shows only conversations used within the last two hours; older bindings and audit records remain stored. Administratively released, removed-Worker or account-changed bindings are invalidated, and explicit invalid previous response IDs return `previous_response_not_found`. See [execution continuation](execution-continuation.md).

The app-server connection pool allows up to 10 WebSockets per API Key/Worker pair and 40 WebSockets per Worker by default. Idle connections are reaped after 600 seconds. Requests wait up to 30 seconds for a slot and then receive HTTP 503 with `worker_capacity_exceeded`. Configure these limits with `CODEX_GATEWAY_MAX_WS_PER_KEY_WORKER`, `CODEX_GATEWAY_MAX_WS_PER_WORKER`, `CODEX_GATEWAY_WS_IDLE_TTL_SECONDS`, and `CODEX_GATEWAY_WS_ACQUIRE_TIMEOUT_SECONDS`.

## Security notes

- Do not expose worker port 4500 or manager port 4600 on the host.
- Rotate the admin password, Key HMAC pepper, worker capability token and manager token.
- Set a unique `CODEX_GATEWAY_ADMIN_SESSION_SECRET`. Enable
  `CODEX_GATEWAY_ADMIN_COOKIE_SECURE=true` when the admin console is served over
  HTTPS. The admin session is an HttpOnly, SameSite=Lax signed cookie with a
  12-hour lifetime; state-changing admin forms also require a CSRF token.
- `CODEX_GATEWAY_ADMIN_PASSWORD` initializes the database credential only when the configured administrator does not yet exist. Later password changes are made from the admin console and persist in PostgreSQL. A password change increments the session version, invalidating every older admin cookie.
- The test deployment listens on `0.0.0.0:8000`; production should firewall that port to the reverse proxy.
- Inputs and outputs are intentionally absent from database records and normal application logs.
