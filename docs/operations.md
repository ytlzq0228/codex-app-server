# Operations

## Current dual-active production (2026-10-02)

Production now runs on `<deploy-user>@<app-1>` and `<deploy-user>@<app-2>`,
with the canonical deployment directory `/opt/codex-app-server-ha` and Compose
project `codex-ha`. Both systemd units select that standalone HA composition.
Database writes go through each host’s DB Proxy to the shared Patroni cluster.
The old local PostgreSQL container is stopped and cannot restart automatically.
See [dual-active deployment, validation and rollback](dual-active.md) before
changing these nodes; the historical release instructions below describe the
previous single-node layout. Use `scripts/verify_ha.py` for the current release.

## Release backup policy

Validate each release in the test environment before deploying production.
`scripts/deploy_release.py` backs up test deployments, deleting historical test
backups before creating the latest one. For the canonical production directory
`/opt/codex-app-server-ha` with `.env.ha`, it deploys directly without a new backup.
Existing production backups remain available; this policy does not remove them.

## Admin history data loading

`/admin/history` serves the page shell and filter controls. The browser requests
`/admin/history/data` as JSON with `history_page` (30 conversation summaries per
page) and the existing `conversation`, `key_id`, `endpoint`, `start`, and `end`
filters. Expanding a conversation calls `/admin/history/requests` with its exact
conversation/Key/interface identity and `page` (20 requests per batch). Both
endpoints require an admin session and return `Cache-Control: no-store`.
Summary totals still cover the full matching conversations; time filters select
conversations containing a matching request. Request pages contain display
fields, not the stored request parameters or audit payloads.

## Install

1. Copy the repository to `/opt/codex-app-server` and create `.env` from `.env.example`.
2. Replace every value containing `development` or `change-this`. `CODEX_GATEWAY_DEV_API_KEY` has no default and must stay empty outside development: any value it holds is a full-access API credential that bypasses ownership, provider entitlement and quota.
3. Build/load the release images, install `deploy/codex-gateway.service` in `/etc/systemd/system/`, then run `systemctl daemon-reload && systemctl enable --now codex-gateway`.
4. Put Caddy or another TLS reverse proxy in front of port 8000. The example preserves SSE flushing.

## Release consistency (2026-09-29)

| Environment | SSH target | Application directory | Compose override |
| --- | --- | --- | --- |
| Test | `<deploy-user>@<test-host>` | `/home/<deploy-user>/codex-app-server` | `compose.gemini-test.json` |
| Production | `<deploy-user>@<app-1>` | `/opt/codex-app-server` | `compose.override.yaml` |

The application release is commit `2ee9580e0089abad46b5827a32a77f067008b8f0`.
Both environments use the same three images: `codex-gateway:release-2ee9580`
(gateway and manager), `codex-gateway-worker:release-2ee9580`, and
`codex-antigravity-worker:release-2ee9580`. Build once and promote the exact image
archives; compare archive SHA-256, filesystem layers, and runtime image settings
after loading, rather than rebuilding for production. Different Docker storage
engines may display a config digest versus an OCI manifest digest as the image ID.

Each application directory contains `release-manifest.json` with SHA-256 hashes
of the deployed files, and `release-verification.json` with container verification
results. Run `sudo python3 scripts/verify_deployment.py <application-directory>`
to compare the disk files, installed gateway/manager package, and every running
managed worker against that manifest, then check health and login-origin handling.
Account data and environment-specific secrets are not part of the manifest.

`/etc/systemd/system/codex-gateway.service.d/release.conf` explicitly selects
both Compose files in each environment. Start/reload uses `--no-build` and the
published image tags. Do not use `--remove-orphans`: dynamically created workers
can carry historical Compose labels. Upgraded dynamic workers have those stale
labels removed. Manager configuration pins the same worker images for future
workers. Retained stopped rollback containers are not active release instances.

For subsequent releases, synchronize the complete tracked source trees, including
deleting obsolete source files, and reset gateway/manager build contexts to the
application root. Do not leave build contexts pointing at an old single-file
hotfix directory. Back up test, validate there, then update production directly. Keep
existing `.env`, model overrides, account volumes, and container network identities.
Only the latest test deployment backup is retained. Use
`sudo python3 scripts/deploy_release.py <application-directory> <artifact-directory>`
for subsequent gateway/manager releases. Artifacts must contain `image.tar`,
`source.tar.gz` and `release-manifest.json`. The deployment script automatically
runs `backup_release.py` before replacing test application files or containers.
The canonical production HA directory skips this step after test validation.
For a standalone backup use
`sudo python3 scripts/backup_release.py <application-directory>`; add
`--historical-root /opt/codex-app-server/deploy-backups` for the former layout. The script checks database access, deletes historical backups
**before** creating the new backup, and saves configuration, a consistent database
dump, gateway/manager images and container settings. Account volumes and Worker
images remain in place for application-only releases; back them up separately
when a release changes Worker data or images. Rollback must restore the prior overrides and
container configuration as well as images. Never restore an older database over
new user writes without reconciling those writes.

Deployment backups and release archives are excluded from Docker build contexts.

## Backup and restore

Back up the PostgreSQL database and each `*-codex-home` Docker volume. Workspace volumes are disposable. Restore the database and account volumes together so thread bindings still point to the correct worker identity.

## Worker lifecycle

Only the internal worker-manager receives the Docker socket. It refuses to remove containers without `io.codex-gateway.managed=true`. Draining a worker removes it from new scheduling; existing response bindings remain pinned to it. Probe the account after device-code login before enabling traffic.

Connection failures, logged-out accounts, and subscription-limit errors quarantine a worker automatically. Pooled stateless requests may retry once on another worker only when the failed turn is known not to have started. Pinned keys and `previous_response_id` continuations never move between workers. The recovery loop probes quarantined workers after their cooldown and returns them to the pool only when the app-server is reachable and the account is logged in. Configure the sweep and cooldowns with `CODEX_GATEWAY_WORKER_RECOVERY_INTERVAL_SECONDS`, `CODEX_GATEWAY_WORKER_FAILURE_COOLDOWN_SECONDS`, and `CODEX_GATEWAY_WORKER_LIMIT_COOLDOWN_SECONDS`.

Both manual probes and automatic recovery probes run a minimal real Codex turn. `account/read` is used only to establish identity; a worker returns to `ready` only after the turn completes successfully. Probes therefore consume a very small amount of subscription usage.

API request sessions release their PostgreSQL transaction before entering a potentially long Codex turn. Configure SQLAlchemy capacity with `CODEX_GATEWAY_DATABASE_POOL_SIZE`, `CODEX_GATEWAY_DATABASE_MAX_OVERFLOW`, and `CODEX_GATEWAY_DATABASE_POOL_TIMEOUT_SECONDS`. An `idle` PostgreSQL connection is a reusable pooled connection; `idle in transaction` during inference indicates a regression.

The admin console provides paginated access to all request records. Deleting an API key is a soft delete: authentication stops immediately and active response bindings are removed, while usage records remain available for audit. Removing a single active session deletes every `previous_response_id` binding for that Key/thread pair, so subsequent continuation attempts return `previous_response_not_found`.

Responses and identified Chat Completions conversations retain response-to-Thread bindings without an idle TTL. Both interfaces can automatically resume after validating client identity, full history and configuration. The Key active-session page shows only conversations used within the last two hours; older bindings and audit records remain stored. Administratively released, removed-Worker or account-changed bindings are invalidated, and explicit invalid previous response IDs return `previous_response_not_found`. See [execution continuation](execution-continuation.md).

The app-server connection pool allows up to 10 WebSockets per API Key/Worker pair and 40 WebSockets per Worker by default. Idle connections are reaped after 600 seconds. Requests wait up to 30 seconds for a slot and then receive HTTP 503 with `worker_capacity_exceeded`. WebSocket keepalive pings retain the library's 20-second interval, while the pong timeout is extended to 300 seconds so long-running client tools don't lose their pending RPC. Configure these limits with `CODEX_GATEWAY_MAX_WS_PER_KEY_WORKER`, `CODEX_GATEWAY_MAX_WS_PER_WORKER`, `CODEX_GATEWAY_WS_IDLE_TTL_SECONDS`, `CODEX_GATEWAY_WS_ACQUIRE_TIMEOUT_SECONDS`, and `CODEX_GATEWAY_WS_PING_TIMEOUT_SECONDS`.

## Security notes

- Do not expose worker port 4500 or manager port 4600 on the host.
- Rotate the admin password, Key HMAC pepper, worker capability token and manager token.
- The session cookie is HttpOnly and SameSite=Lax with a 12-hour lifetime.
  `CODEX_GATEWAY_ADMIN_COOKIE_SECURE` marks it Secure: leave it unset to detect
  HTTPS from the request, `true` to force it on, `false` for plain HTTP. Detection
  depends on uvicorn rewriting the scheme from `X-Forwarded-Proto`, which it only
  does for a peer listed in `--forwarded-allow-ips` (see
  `CODEX_GATEWAY_TRUSTED_PROXY_IPS` in `compose.yaml`); set the flag to `true`
  when the TLS terminator is not in that list. Verify after deploying:
  `curl -sk -X POST https://<host>/auth/login -d 'username=x&password=y' -D - -o /dev/null | grep -i set-cookie`
  on a successful login must show `Secure`. Sessions live in PostgreSQL, so
  `CODEX_GATEWAY_ADMIN_SESSION_SECRET` is no longer read.
- State-changing forms require a CSRF token. Login cannot carry one, so every
  cookie-authenticated POST additionally rejects a cross-origin `Origin` header.
  A redacted `Origin: null` is accepted only when the browser also sends
  `Sec-Fetch-Site: same-origin`; missing metadata, `same-site`, and `cross-site`
  remain rejected. Proxies must preserve this browser header, never synthesize it.
  Pages use `Referrer-Policy: same-origin` to preserve the Origin on same-origin
  form submissions while hiding cross-origin referrers. Do not override it with
  `no-referrer`: that redacts form Origin, breaking plain HTTP/IP login where
  browsers omit Fetch Metadata. Reload the login page after changing this policy.
  Bearer API paths `/v1/` and `/v1beta/models/` are exempt from this form-only
  check and still require a valid API key.
- Password login is rate limited: five failures per account and twenty per
  client address trigger an escalating lock, from 15 seconds up to 15 minutes.
  A successful login clears both counters, and counters idle for an hour expire.
- `/docs`, `/redoc` and `/openapi.json` are disabled; the schema described every
  admin form to anonymous callers.
- One account may contribute at most `CODEX_GATEWAY_MAX_WORKERS_PER_USER`
  Workers (10 by default), because each one runs a container.
- `CODEX_GATEWAY_ADMIN_PASSWORD` initializes the database credential only when the configured administrator does not yet exist. Later password changes are made from the admin console and persist in PostgreSQL. A password change increments the session version, invalidating every older admin cookie.
- The test deployment listens on `0.0.0.0:8000`; production should firewall that port to the reverse proxy.
- Model outputs are never stored; only a SHA-256 digest is retained. Request bodies, including prompts, ARE stored in `usage_records.request_params` so the console can show request details, and any administrator can read every user's prompts. Treat the database accordingly.

## Provider permissions and monitoring update (test rollout)

The test rollout adds `users.provider_grants` (JSON, default `[]`) through the
idempotent startup migration. `/admin/users` provides independent Codex and Gemini
manual-grant switches. Effective model access is the union of manual grants and
providers from existing Workers; revoking a manual grant does not revoke automatic
Worker access or change the user's Key quota. Only administrators may grant access;
ordinary administrators cannot manage other administrators.

Subscription snapshots version 4 contain independent `providers.codex` and
`providers.gemini` aggregates. Old mixed-provider history is not reused as a
provider-specific series. The current hourly bucket is refreshed once if it predates
version 4. A confirmed Gemini enterprise response without numeric quota is displayed
as unlimited and contributes 0% usage; transport/CLI errors remain unknown.

A worktree release records its explicit `release` and `component_images` in the
manifest, alongside the base commit and file hashes. Unchanged Worker images can
remain pinned to the prior release while gateway and manager use the new full image.
This update is deployed to test first; production promotion is a separate action.
