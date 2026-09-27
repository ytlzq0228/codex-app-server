# Codex App Server Gateway

FastAPI gateway exposing OpenAI-compatible `/v1/responses` and `/v1/chat/completions` APIs with text/image input and text output, backed by isolated Codex app-server workers. Both endpoints support JSON and SSE responses, including text/image results from client tools. See [image input and tool results](docs/images.md). The gateway also provides persistent per-Key app-server connections, Responses thread continuation, pooled or pinned worker scheduling, request-level usage accounting and price snapshots, device-code login, and restricted worker lifecycle management.

```bash
cp .env.example .env
docker compose up --build
```

The test deployment listens on `0.0.0.0:8000`. The admin console is available
at `/login` and uses a database-backed HttpOnly session cookie. For production, disable
the development API key, rotate every secret (including the admin session
secret), enable secure admin cookies, and put a TLS reverse proxy in front of it.

See [PRD.md](PRD.md), [docs/architecture.md](docs/architecture.md), and [docs/operations.md](docs/operations.md).

User self-service is at `/account`; Google OAuth is configured by administrators at `/admin/google`. See [user-service and billing operations](docs/self-service.md).
