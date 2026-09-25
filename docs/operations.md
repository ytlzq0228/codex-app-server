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

## Security notes

- Do not expose worker port 4500 or manager port 4600 on the host.
- Rotate the admin password, Key HMAC pepper, worker capability token and manager token.
- The test deployment listens on `0.0.0.0:8000`; production should firewall that port to the reverse proxy.
- Inputs and outputs are intentionally absent from database records and normal application logs.
