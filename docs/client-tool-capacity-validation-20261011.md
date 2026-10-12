# Client tool capacity validation — 2026-10-11

Test release: `tools512-20261011`, based on the deployed
`key-budget-20261010T114934Z` source manifest. Test environment only; production
was not changed.

The gateway, Claude Worker and Gemini Worker now accept up to 512 client tool
declarations. Namespace expansion and additional tools count toward this limit.
The separate 64-character name limit, event queue capacity and pending-call
limits are unchanged. This is an application policy, not an upstream maximum.

## Real model validation

Each case declares the full catalog with automatic tool selection, asks for its
last tool, checks the returned tool name, returns a fresh random marker, and
requires a completed final answer containing that marker. Tools are synthetic;
no model-generated code is executed.

| Model | Interface | Tools | Result / complete round-trip seconds |
| --- | --- | --- | --- |
| claude-sonnet-5-5 | Responses, non-streaming | 65 / 128 / 256 / 321 / 512 | Passed: 4.70 / 4.95 / 5.19 / 5.43 / 6.02 |
| claude-sonnet-5-5 | Chat Completions, non-streaming | 512 | Passed: 6.11 |
| claude-sonnet-5-5 | Native Messages, non-streaming | 512 | Passed: 5.86 |
| claude-sonnet-5-5 | Responses, streaming | 512 | Passed: 6.22 |
| claude-sonnet-5-5 | Chat Completions, streaming | 512 | Passed: 6.24 |
| claude-haiku-5-5 | Chat Completions, non-streaming | 512 | Passed: 4.92 |
| gpt-6-sol | Responses, non-streaming | 512 | Passed: 6.30 |
| gemini-3.8-flash-high | Responses, non-streaming | 512 | Passed: 18.07 |

513 tools were explicitly rejected with HTTP 400, `unsupported_parameter`, and
`At most 512 client tools are supported` for Sonnet, Haiku, Codex and Gemini.
This verifies the configured boundary, not an upstream failure at 513.

All 12 real round trips completed. Five temporary validation keys were revoked
and their response bindings removed; a database check found zero active test keys.

These are individual functional checks with short synthetic tool definitions,
not a load benchmark or proof that arbitrary large schemas fit every context.
512 is the highest count tested; higher counts were not explored.

## Regression and deployment checks

134 local tests passed across client protocol, Claude Worker, Gemini gateway
tools, and Gemini Worker bridge/service/endpoints. New boundary tests cover
512 declarations, last-tool mapping, namespace/additional-tool aggregation,
and rejection of 513 at gateway and Worker layers.

The test gateway and manager run the new gateway image; all three running Claude
Workers and the Gemini Worker run their new provider images. Manager defaults
were updated for new Workers. Existing account identities, volumes, networking
and security settings were preserved. Prior Worker containers remain stopped
and disconnected as rollback copies; the regular test deployment backup was
created before replacement. The Codex Worker image was unchanged.

Verification matched deployed source hashes, gateway/manager installed package
hashes, Worker service/bridge hashes and manager image defaults to the release
manifest. Gateway health passed.

Reproducible non-streaming smoke tests:
`scripts/validate_tool_capacity.py`, with `GATEWAY_URL` and
`GATEWAY_API_KEY` supplied through the environment. The script defaults to Sonnet
and tests Responses at 65, 128, 256, 321 and 512, then Chat Completions and Messages
at 512 and Responses rejection at 513.
