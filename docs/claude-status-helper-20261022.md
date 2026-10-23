# Claude background status requests interrupt pending tools

Production audit and gateway logs identify Claude Code 2.1.307 notification
classification requests sharing the main agent session ID. Their instructions
start with “A user kicked off a Claude Code agent”, with variable task context
in one user message and no tools. Both main execution and classification can
use `x-app: cli-bg`; that header alone cannot distinguish them.

At 2026-10-22 03:01:08.320 UTC the main request returned a Write call. At
03:01:08.682 the gateway accepted `/internal/tools/abandon`. At 03:01:09.214
the actual Write output failed with `client_tool_call_unavailable`.
The classification request recorded `context_only_recovery`. The same pattern
appeared around 02:51 and 02:58 UTC. Gateway uptime did not indicate a restart.

The fix recognizes the complete whitespace-normalized classification system
template, excluding the variable billing header. It requires a Claude CLI
header, one user message, no tools and no explicit response binding. Existing
helper isolation disables tools and leaves the main checkpoint untouched.
The fixture contains only the generic instructions, not the user's task text.

As a fallback, a Claude request without tools or pending outputs, lacking the
waiting execution's verified history and an explicit response binding, runs
independently with tools disabled. It cannot abandon the pending execution.
This also means a genuine short text-only request without matching history
does not replace the main pending task. Verified recovery remains unchanged.

Regression tests cover recognized and modified templates, both CLI headers,
unchanged checkpoints and live pending calls, and successful delivery of the
original tool result after the auxiliary request.

Separate observed Codex interruption: at 03:02:37.869 UTC its worker was
quarantined with `failure_kind=limit`, reason `exceeded retry limit, last
status: 429 Too Many Requests`. At 03:02:38.169 its result submission failed
with `tool_worker_unavailable`. A later 03:03:32.618 request was recorded as
`request_interrupted`; this record alone does not identify who disconnected.

The earlier foreground/background session-ID mismatch and duplicate-result
errors are separate from helper cancellation. This change does not relax
call ownership or replay already accepted results. Production has not been
deployed as part of this local fix.
