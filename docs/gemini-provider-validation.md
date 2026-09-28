# Gemini provider: isolated acceptance phase

## Status

Development target: <deploy-user>@<test-host>.

The existing gateway, Codex worker image, configuration and database schema have
not been changed. Gemini is not registered in the worker pool or public model list.

The standalone gemini-provider-probe container runs Gemini CLI **0.61.0**,
without published ports or membership in the gateway Compose network. Credentials
and probe sessions use dedicated Docker volumes. Its root filesystem is read-only.
Built-in tools are excluded; the only trusted tool is the fixed, side-effect-free
gateway_echo MCP tool provided by this repository.

Verified:

- Five account-free tests: interleaved ACP notifications/replies, EOF handling,
  permission denial, sanitized RPC errors, and MCP initialize/list/call/reject.
- Real CLI version is 0.61.0.
- Real ACP initialization advertises Google OAuth, session loading and MCP.
- Existing gateway /healthz still returns HTTP 200.

Not yet verified:

- Google account authentication, paid-plan entitlement and usable models.
- Model text streaming, conversation continuity, restoration after process restart.
- Actual model-to-MCP tool roundtrip, cancellation during generation.
- Upstream quota-exhaustion behavior.
- OpenAI API compatibility for Gemini. An MCP roundtrip alone does not prove this.

No OpenAI regression suite has been run in this phase; its implementation and
deployment were not changed.

## Login and run

Log into the dedicated container using your own terminal:

    ssh -t <deploy-user>@<test-host> 'docker exec -it gemini-provider-probe gemini'

Follow the Google login prompts. NO_BROWSER=true supports login from the remote
machine. After login, exit the CLI before running the acceptance probe.
Do not put passwords, OAuth codes or credential contents in logs or chat.

Run the latest script from the test host staging directory (the running container
can retain an earlier baked-in probe script):

    ssh <deploy-user>@<test-host> \
      'docker exec -i gemini-provider-probe python3 - --authenticated < /home/<deploy-user>/gemini-provider-test/probe.py'

The authenticated probe requires oauth-personal and rejects API-key mode. It
checks streamed text, remembered random content, MCP execution, a fresh-process
session load, and cancellation after output begins. It uses a new workspace per
run and never emits credential contents. Authentication success alone does not
prove a paid subscription tier.

Quota exhaustion is explicitly reported as not tested. Use an already limited
test account to observe real behavior; do not intentionally consume a plan's
remaining quota. Add deterministic adapter error-injection tests when implementing
the backend; mocked errors must be reported separately from real observations.

Run account-free tests:

    python3 -B -m unittest discover -s worker/gemini -p test_probe.py -v

Run real account-free initialization:

    ssh <deploy-user>@<test-host> \
      'docker exec -i gemini-provider-probe python3 - < /home/<deploy-user>/gemini-provider-test/probe.py'

## Gateway integration gates

Proceed from observed protocol behavior, keeping the Gemini feature disabled
until acceptance and OpenAI regression checks pass.

1. Add an explicit model registry: public name, provider, upstream name and
   verified capabilities. Existing model names and aliases retain Codex behavior.
   Do not infer providers from model-name prefixes.
2. Extend CompletionBackend with a separate Gemini implementation. Keep
   AppServerBackend and its Codex RPC behavior intact. ACP connections, process
   cleanup, errors, usage and tool bridges belong to Gemini.
3. Add provider to worker, response binding, execution checkpoint and usage
   records with an additive migration defaulting existing records to Codex.
4. Select provider before choosing a worker. Filter initial selection AND retries;
   validate pinned workers, previous responses, auto-resume and pending tool
   sessions against provider. A provider mismatch must not reset a conversation
   and retry silently.
5. Dispatch login, account refresh, health checks, quota inspection and recovery
   by provider. Codex account/read and healthcheck turns must never reach Gemini.
6. Validate capabilities before opening a stream. Reject unsupported images,
   tools, schemas, reasoning controls and continuation semantics explicitly.
7. Bridge client tools through an authenticated, request-scoped MCP channel.
   Preserve pending tool identity, key ownership and cancellation across HTTP
   requests. Do not expose CLI-internal tool notifications as public tool calls.
8. Test mixed pools, pinned workers, cross-provider continuation rejection,
   tool-result ownership, streaming errors, cancellation and pre/post-start
   retry safety. Run the existing OpenAI tests against a separate test database.
9. Deploy the test gateway with Gemini disabled first, verify legacy behavior,
   then enable only the verified Gemini models.

Do not replace the running gateway or migrate its database as part of the isolated
ACP acceptance probe.
