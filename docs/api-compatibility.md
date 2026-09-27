# OpenAI API compatibility matrix

Audit baseline: 2026-09-25. Scope is the API surface this gateway claims to
provide: Responses, Chat Completions, Models, authentication, errors, HTTP and
SSE behavior. Other OpenAI products (Images, Audio, Files, Embeddings, Batch,
Realtime, Fine-tuning, etc.) are not gateway targets.

Status legend: **Compatible** means both wire format and behavior are provided;
**Partial** means common clients work but semantics differ; **Missing** is a
planned gap; **Out of scope** conflicts with the Codex backend capabilities or the
no-content-retention design.

## Cross-cutting protocol

| Capability | Official API | Gateway now | Status | Resolution |
|---|---|---|---|---|
| Bearer authentication | Bearer API key | Bearer gateway key | Compatible | Done |
| Error envelope | `error.message/type/code/param` | Same top-level shape | Compatible | Done |
| Validation paths | Point to rejected parameter | Field/index path from Pydantic | Partial | P1: normalize union/model errors further |
| Request ID | `x-request-id` response header | Generated on every HTTP response | Compatible | Done |
| Rate limiting | 429 plus request/token limit headers | No per-key RPM/TPM enforcement or headers | Missing | P1: add configurable per-key limiter |
| Request size | Platform-enforced limits | Configurable `Content-Length` guard | Partial | P1: enforce streamed/chunked body size too |
| Idempotency/retries | Endpoint-dependent platform behavior | No idempotency key handling | Missing | P2 |
| Unknown request fields | Validated against official schema | Accepted for forward compatibility | Partial | P1: maintain known-field registry and compatibility telemetry |
| SDK compatibility | Official SDKs | Official Python SDK black-box JSON/SSE suite | Compatible for claimed text surface | Done |

## Models

| Capability | Official API | Gateway now | Status | Resolution |
|---|---|---|---|---|
| List models | `GET /v1/models` | Implemented for configured aliases/models | Compatible | Done |
| Retrieve model | `GET /v1/models/{model}` | Implemented | Compatible | Done |
| Model metadata | Real creation time and owner | Synthetic `created=0`, gateway owner | Partial | P2: expose explicit gateway metadata |
| Delete fine-tuned model | Supported for owned fine-tunes | Not applicable | Out of scope | No fine-tuning backend |

## Responses API

| Capability | Official API | Gateway now | Status | Resolution |
|---|---|---|---|---|
| Create text response | `POST /v1/responses` | Implemented | Compatible | Done |
| String/message input | String or typed input-item union | String, message, direct text item | Compatible for text | Done |
| Tool-call history input | Function/custom call and call-output items | Serialized into textual history | Partial | P1: preserve typed semantics where app-server permits |
| Image input | URL, file ID, base64 | HTTP(S) URLs and base64 data URLs; ordered text/image input and client tool results | Partial | Supported; file IDs are rejected; see [images](images.md) |
| File input | File input items | Explicitly rejected | Out of scope now | Requires file ingestion |
| Instructions | Per-request developer instruction | Flattened into prompt text | Partial | P1: map to app-server developer instructions if exposed |
| `previous_response_id` | Stored response continuation | Private response-to-thread mapping | Compatible for persisted keys | Done |
| Response storage/retrieve/delete | Store plus GET/DELETE response endpoints | Stores only thread binding; no body retrieval | Out of scope by privacy design | Do not store content unless PRD changes |
| Cancel response | Cancel background response | Not implemented | Out of scope now | Requires background execution |
| List input items | Retrieve persisted response input | Not implemented | Out of scope by privacy design | — |
| Background mode | Asynchronous response lifecycle | Rejected | Out of scope now | Requires job runner and persistence |
| Conversation object | Managed conversation membership | Rejected; continuation uses response ID | Missing | P2 if needed |
| Text SSE lifecycle | Typed semantic events with sequence numbers | Core text lifecycle implemented | Compatible for success path | Done |
| Failed/incomplete SSE | `response.failed` / `response.incomplete` | Error plus `response.failed`; incomplete not yet produced | Partial | P1 |
| Client disconnect cancellation | Server stops/reconciles work | No explicit upstream turn interrupt | Missing | P0 |
| Stream obfuscation | `include_obfuscation` controls padding | Accepted, no obfuscation field emitted | Partial | P2 |
| Reasoning effort | `reasoning.effort` affects generation | Mapped to app-server `effort` | Compatible | Done in current change |
| Reasoning summary/items | Optional reasoning output items | Not emitted | Missing | P2; depends on app-server events |
| Structured outputs | `text.format` JSON schema/object | Mapped to app-server `outputSchema` | Compatible for JSON schema | Done in current change |
| Sampling (`temperature`, `top_p`) | Affects sampling on supported models | Accepted but app-server exposes no equivalent | Partial | P1: reject when semantic guarantee is required or document model behavior |
| Output-token limit | `max_output_tokens` enforced | Accepted but not enforced | Missing | P0 |
| Truncation/context management | Configurable context behavior | Accepted but not applied | Missing | P1 |
| Metadata | Stored and returned | Returned and logged without content | Partial | P1: validate official count/length limits |
| `store` | Controls response retention | Gateway always reports `false`; body is never retained | Partial | Truthful by privacy design |
| Service tier | Selects and reports actual tier | Echoed but not selected upstream | Partial | P0: stop claiming an unverified tier |
| Prompt caching hints | Cache key/options/retention | Accepted but not forwarded | Partial | P1 |
| Safety identifier | Stable abuse identifier | Accepted but not forwarded | Partial | P2 |
| Include/logprobs | Optional extra response data | Requested logprobs explicitly rejected | Partial | Done for honest behavior |
| Tools | Built-in, MCP, function and custom tools | Offered tools ignored for text-chat compatibility; forced choice rejected | Partial | P2; intentionally no client tool executor |
| Usage details | Input/output/cached/reasoning breakdown | Totals; cached/reasoning hard-coded zero | Partial | P1: map all available app-server counters |

## Chat Completions API

| Capability | Official API | Gateway now | Status | Resolution |
|---|---|---|---|---|
| Create text completion | `POST /v1/chat/completions` | Implemented as Responses adapter | Compatible for text | Done |
| Message roles | developer/system/user/assistant/tool/function | Text roles accepted; prior tool-call messages rejected | Partial | P1 |
| Text content parts | String or typed text parts | Implemented | Compatible | Done |
| Image content | Multimodal image parts | HTTP(S)/base64 `image_url` parts preserved through Responses adapter | Compatible for URL/data images | See [images](images.md) |
| Audio/file content | Audio/file parts | Explicitly rejected | Out of scope now | Requires additional ingestion |
| Data-only SSE | Chunks followed by `[DONE]` | Implemented | Compatible | Done |
| Stream usage | Final empty choices chunk when requested | Implemented | Compatible | Done |
| `n` choices | Multiple choices where model supports it | Only `n=1` | Missing | P2 |
| Max tokens | `max_completion_tokens` enforced | Mapped only as an unenforced Responses hint | Missing | P0 |
| Stop sequences | Stop generation at sequence | Accepted but ignored | Missing | P0 |
| Penalties/seed/sampling | Affect generation where supported | Accepted but mostly ignored | Partial | P1 |
| Log probabilities | Requested token logprobs in response/chunks | Explicitly rejected | Partial | Done for honest behavior |
| Structured outputs | JSON object/schema response format | Mapped to app-server `outputSchema` | Compatible for JSON schema | Done in current change |
| Prediction | Static predicted output | Explicitly rejected | Partial | Done for honest behavior |
| Tools/function calling | Tool definitions, choices and tool-call output | Optional definitions ignored; forced choice rejected | Partial | P2 by product decision |
| Audio output | Audio modality and audio response | Rejected | Out of scope | — |
| Stored completion CRUD | list/get/update/delete/messages when `store=true` | Not implemented | Out of scope by privacy design | — |
| Finish reasons | stop/length/tool_calls/content_filter | Always `stop` on success | Missing | P0: emit `length` when limits are enforced |
| Usage details | Prompt/completion detail objects | Only three totals | Partial | P1 |

## Resolution order

1. **P0 protocol correctness:** official SDK contract tests; truthful response fields;
   token/stop handling; requested logprobs rejection; failed/incomplete stream events;
   client-disconnect cancellation.
2. **P1 semantic coverage:** error-path normalization; rate limiting; metadata bounds;
   usage details; typed history; context/cache/sampling behavior.
3. **P2 optional surface:** conversations, reasoning summaries, stream obfuscation,
   idempotency, multiple choices, and tool execution only if product scope changes.

Official baselines:

- https://developers.openai.com/api/reference/resources/responses/methods/create
- https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
- https://developers.openai.com/api/reference/resources/models
- https://developers.openai.com/api/docs/guides/streaming-responses
- https://developers.openai.com/api/docs/guides/error-codes
- https://developers.openai.com/api/docs/guides/rate-limits
