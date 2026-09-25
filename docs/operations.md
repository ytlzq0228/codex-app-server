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

The admin console provides paginated access to all request records. Deleting an API key is a soft delete: authentication stops immediately and active response bindings are removed, while usage records remain available for audit. Removing a single active session deletes every `previous_response_id` binding for that Key/thread pair, so subsequent continuation attempts return `previous_response_not_found`.

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
