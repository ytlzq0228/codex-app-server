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

Production dual-active topology and recovery: [dual-active deployment](docs/dual-active.md).

Administrators can configure model name mappings under Price Configuration (`/admin/finance`). For example, map the public Gemini model `gemini-3.1-flash-lite-preview` to the upstream model `gemini-3.6-flash-high`. Adding a mapping exposes its source name through the model API. Standard gemini- and claude- prefixes identify the provider; explicit CODEX_GATEWAY_MODEL_PROVIDERS entries take precedence. Other names default to Codex. Both names must identify the same provider, and the target must be supported by its worker. Models without a database mapping are passed through unchanged; `CODEX_GATEWAY_MODEL_ALIASES` is not applied to these requests. Add mappings only for the exceptional models that need replacement. Mappings apply once and take effect for new requests across gateway nodes. Clearing a mapping restores the exact original model name. API responses, authorization, and billing retain the public model name. The mapping table is created by the normal schema initialization on startup.
