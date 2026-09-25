# Codex App Server Gateway

FastAPI gateway exposing OpenAI-compatible `/v1/responses` and `/v1/chat/completions` text APIs backed by isolated Codex app-server workers. Both endpoints support JSON and SSE responses. The gateway also provides persistent per-Key app-server connections, Responses thread continuation, pooled or pinned worker scheduling, metadata-only usage accounting, device-code login, and restricted worker lifecycle management.

```bash
cp .env.example .env
docker compose up --build
```

The test deployment listens on `0.0.0.0:8000`. The admin console is available
at `/login` and uses a signed HttpOnly session cookie. For production, disable
the development API key, rotate every secret (including the admin session
secret), enable secure admin cookies, and put a TLS reverse proxy in front of it.

See [PRD.md](PRD.md), [docs/architecture.md](docs/architecture.md), and [docs/operations.md](docs/operations.md).
