# Gemini subscription integration (test deployment)

The gateway keeps the existing Codex AppServerBackend and dispatches through
ProviderBackend. Workers, response bindings, execution checkpoints, prices and
usage records have a provider column; existing rows default to codex.

Public model registration is explicit:
CODEX_GATEWAY_ALLOWED_MODELS includes gemini-3.8-flash-high
CODEX_GATEWAY_MODEL_PROVIDERS=gemini-3.8-flash-high:gemini

Only verified models should be exposed. Claude is a reserved provider name, not
an implemented adapter. An enabled Claude model fails capability validation.

## Supported Gemini surface

Both /v1/chat/completions and /v1/responses support text and streaming. Responses
continuations preserve provider, worker generation and native conversation ID.
Explicit chat execution identities also carry provider; Gemini continuation
failures do not silently create a new execution on a different worker.

Client tools support function declarations, namespaces and custom text/grammar
tools through a per-execution MCP relay. Calls and text results use the same
public protocol and bounded continuation registry as Codex. The CLI stays alive
while awaiting a result; no client code executes in the worker.
JSON structured output is prompted and validated by the gateway before delivery; it cannot be combined with tool declarations.
Images (including tool results), reasoning overrides and
unsupported sampling options return a parameter error. Parallel tool calls are
not enabled; omit parallel_tool_calls or set it to false.
Every gateway turn installs explicit deny rules for native file reads/writes,
commands and web access. Only the server-owned MCP relay is available, with a
random per-execution capability URL. Original settings and MCP configuration
are restored during cancellation-shielded cleanup. The worker runs in its own
workspace without host mounts or Docker access.

Per-turn usage is summed from step_update usage, not cumulative result usage.
Cached input is added to uncached input to produce the total required by OpenAI
usage and gateway billing.
Client disconnect sends SIGINT to the CLI process group, waits for exit and
escalates to SIGKILL after five seconds. Cleanup is cancellation-shielded and
releases the execution slot. Capacity is one active request per Gemini worker, with a 30-second bounded wait for its execution slot.

Quota failures are classified separately, put that Gemini worker into cooldown,
and do not retry an already-started request. Nonstreaming quota errors are HTTP
429; streaming quota errors carry provider_quota_exhausted. Real subscription
exhaustion has not been induced; only injected error behavior is validated.
Usage exhaustion (`error` with `failure_kind=limit`) retains paid-account Key
Quota for both Codex and Gemini. Routing cooldown does not revoke key capacity.
Logout, connection failure, disablement and deletion still recalculate capacity;
restoring capacity never automatically re-enables disabled keys.

## Web login

Open /user/workers, create a Worker with Gemini / Antigravity, then click login.
The page shows structured choice buttons, a validated Google OAuth link and a
password-style authorization-code input; raw terminal output is never returned
to the browser. Choose Google
Cloud, complete SSO in the external browser, paste its code, and select the
available license/project. The project is not configured globally or baked into
the image. Login metadata comes from the official CLI header.

Each worker uses its own home volume. Only owners can access the login routes;
POSTs require CSRF. The private worker requires a bearer token and has no public
port. Login sessions expire after ten minutes. Codes are not stored in the
gateway DB/audit. Menu submissions validate the current menu before sending input.
Closing the dialog cancels an active login.

Existing signed-in workers support explicit logout and re-login through the official
CLI /logout command. Logout invalidates native conversation bindings and removes
contribution credit; it never silently continues a prior user's conversation.
The UI asks for confirmation because keys may be disabled by the existing quota rule.
Re-login starts only after logout succeeds. An already signed-out official CLI
counts as successful logout, allowing stale gateway account metadata to be cleared.

Paid Gemini subscription accounts contribute one key slot under the existing
health/enabled/account-check requirements. Free tiers contribute no slots.
Deduplication uses (owner, provider, normalized email). Grant/consume/automatic
key disabling semantics remain unchanged.

Every persisted key shares its owner's provider permissions, derived from owned,
non-deleted workers (including temporarily unhealthy/disabled workers). Both APIs
reject unauthorized providers with 403 provider_not_allowed; model discovery is
filtered the same way. Admin-granted capacity does not grant model permissions.
The development key keeps its existing unrestricted test behavior.

Subscription plan keys for Codex remain unchanged; other providers use a namespace
(e.g. gemini:gcp-ge-plus-tier). The UI displays OpenAI / Gemini / Claude prefixes.
Existing unambiguous Gemini plan prices/colors/weights are preserved during migration.

Gemini usage queries call the official /usage panel. Enterprise Business accounts
currently return only the enterprise consumption documentation link, with no
numeric remaining/used quota. Display this as unavailable, never zero or an
unexplained read failure.

## Deployment and rollback

Build worker/antigravity as codex-antigravity-worker:1.2.12 and set
GEMINI_WORKER_IMAGE in worker-manager. Test deployment uses
/home/<deploy-user>/codex-app-server/compose.gemini-test.json as an overlay:
docker compose -f compose.yaml -f compose.gemini-test.json up -d --no-deps --no-build gateway worker-manager

The original Codex worker image, container and home volume are unchanged.
Database migrations are additive; rollback to the backed-up gateway/manager
images can retain the new columns. Disable Gemini workers before rolling back
to a version which does not filter providers. Preserve Gemini home volumes.

## Validation

- Existing full suite: 201 passed on a separate PostgreSQL test database.
- Added provider and affected API/session tests: 68 passed.
- Worker transport tests: 5 passed (usage, quota error, EOF, code redaction, auth classification).
- Live Enterprise Plus: credential reuse, model list, streaming, native resume.
- Clean login: navigation through Google Cloud menus, clean OAuth URL and code prompt.
- Full new-account browser callback still requires user completion.
- At the initial text-only deployment, client tools and real quota exhaustion
  had not been verified. Client-tool validation is tracked below; real quota
  exhaustion remains unverified.

- Additional provider/Chat option validation: 17 passed.
- Deployed gateway: Responses text and streaming, Chat streaming, native resume,
  cross-provider rejection, unsupported tools and persisted provider bindings passed.
- Live cancellation followed by a successful new request passed after fixing slot cleanup.
- Chrome Web UI: login dialog displayed the existing Enterprise Plus account/project;
  no JavaScript errors.
- Test Codex workers were already disabled before deployment (verified against DB
  backup). Live Codex inference was skipped; their original states remain unchanged.

Test site: http://<test-host>:8000/user/workers
Registered Gemini worker: gemini-enterprise-test.
Rollback backup: /home/<deploy-user>/gemini-backup-20260928-012350.
Repeat end-to-end verification with scripts/validate_gemini.py inside the gateway
container. It creates an isolated test user/Key and disables both afterwards.


## Provider quota and account management update (2026-09-28)

Test deployment uses codex-gateway:quota-test and
codex-antigravity-worker:quota-test. Backup:
 /home/<deploy-user>/gemini-quota-backup-20260928.
The original Codex container ID, image and start time were verified unchanged.

Validation: 218 regression tests passed on the first full run; the two old
self-service fixtures were updated to own a Codex Worker under the new access
rule, and all 8 affected/new account-management and subscription tests passed.
Worker service tests: 7 passed. Browser checks confirmed contribution +1,
Enterprise quota explanation, provider-prefixed plans, worker type selector,
and logout/re-login controls with confirmation cancellation. A live logout of
the existing Enterprise account was intentionally not performed; completing
a new Google SSO login requires the account holder.
The validation script now uses a temporary key under the existing administrator,
checks available capacity, and disables/deletes only that key on completion.

Live post-deployment smoke: GPT and Gemini Chat Completions / Responses streams passed. Temporary keys were disabled and soft-deleted.

## Client-tool bridge

Upgrade both the gateway and the Antigravity worker image (including existing
worker containers). Updating only the manager image setting affects future
workers. The gateway checks the private /capabilities protocol before sending
tools; an older worker fails explicitly rather than silently ignoring tools.

The in-memory registry holds the HTTP execution stream and worker slot across
client results. Each pending result expires after 300 seconds. Heartbeats keep
the transport alive; the worker separately limits active inference to 300
seconds. Native conversation ID, API Key, model, tools and worker generation
remain bound. Only one outstanding result is returned at a time. Restarting
either process expires pending calls.

Run scripts/validate_gemini_tools.py with GATEWAY_URL and GATEWAY_API_KEY
(an existing persisted key) to check JSON/SSE Responses and Chat tool
roundtrips, previous_response_id, namespaced grammar input and replay rejection.
The script returns synthetic text results and executes no generated code.

Validated on an isolated test gateway and worker: JSON/SSE tool roundtrips for
both public APIs, Responses previous_response_id, namespaced custom Lark input,
replay rejection and multiple sequential Chat calls. Cancelling a pending tool
call released the worker slot and a subsequent text request succeeded.
A live adversarial prompt
could not read a test secret, write a file or execute a command; the client MCP
tool still completed. Unit tests also cover cross-Key access, invalid arguments,
grammar correction, expired calls, protocol version checks and configuration
restoration. This validation does not itself upgrade existing deployed workers.

Each execution uses a distinct MCP server registration as well as a random
capability URL, preventing tool schemas from one execution being reused by the
CLI for another. Invalid custom arguments or grammar input receive at most two
internal correction replies and are never returned as valid client calls.

Official transport and permission references:
https://antigravity.google/docs/mcp/
https://antigravity.google/docs/permissions/
https://antigravity.google/docs/cli/headless/
