# Request rate limits

Generation endpoints share rolling 60-second limits: 60 attempts per API key and
30 attempts per Worker by default. Workers also have a rolling 10-second burst
limit of ceil(RPM / 6), i.e. 5 attempts with the default configuration. Responses, Chat Completions and Anthropic
Messages use the same counters across models, providers and gateway nodes.
Configure CODEX_GATEWAY_KEY_REQUESTS_PER_MINUTE and
CODEX_GATEWAY_WORKER_REQUESTS_PER_MINUTE consistently on every node; 0 disables
that dimension. These defaults are gateway policy, not upstream account quotas.

The Key is charged after authentication, before monthly-budget and generation
validation. Each selected Worker is charged before dispatch, including tool-result
continuations. Failed calls never refund capacity. Client retries are new attempts;
automatic failover charges the Key again and charges the replacement Worker.
Requests rejected before Worker selection consume only Key capacity. Invalid
credentials and requests rejected before authentication are outside this counter.
Model listing, token estimation and health probes are not generation requests.

Over-limit attempts return HTTP 429 with type rate_limit_error and code
key_rate_limit_exceeded or worker_rate_limit_exceeded, plus Retry-After in seconds.
Rejected retries still count against the Key. Worker rejections never reach the
Worker and do not extend its shared cooldown, preventing one client from starving
other clients. Clients must respect Retry-After and add randomized backoff. A saturated selected Worker returns 429
instead of automatically hopping to another Worker.
For streams, initial admission happens before HTTP headers; if a later failover
hits a limit after streaming began, an error event terminates the existing stream.

Automatic failover is limited to two retries (three total attempts), with randomized
exponential backoff of 0.5–1 seconds then 1–2 seconds. Unsafe turns are never
replayed. Exhausting the retry budget returns worker_retry_exhausted (503,
Retry-After: 5); a rate-limit denial is never automatically retried.

An independent final dispatch gate applies the same minute and burst limits after
local queueing: before Codex Worker RPCs, and before Claude/Gemini POST /turn.
Thus requests admitted earlier cannot bypass limits by draining a queue together.
A failed transport still consumes its reservation. The ingress and dispatch
counters are independent, conservative budgets, not two debits on one counter.
These limits cover gateway generation attempts; administrative/health RPCs,
tool-result protocol messages and provider-internal SDK retries are not separately
metered generation attempts.

PostgreSQL row locks and database time serialize admissions across nodes.
Counters commit independently of request outcomes, survive gateway restarts and
fail closed when the database is unavailable. The request_rate_windows table
keeps one row per Key and two rows per Worker and at most N timestamps per row, rather than an
ever-growing request log. Stale timestamps are pruned when that identity is used.

For deployments with AUTO_CREATE_SCHEMA=false, apply this additive migration
before starting the new gateway version:

```sql
CREATE TABLE IF NOT EXISTS request_rate_windows (
    scope VARCHAR(100) PRIMARY KEY,
    attempts JSON NOT NULL
);
```

All nodes must run the new version for cluster-wide enforcement. Rollback may
leave this small table in place.


A Worker rate-limit rejection also stores a per-Key cooldown deadline. All gateway
nodes check this deadline immediately after authentication and Key accounting,
before model validation, conversation preparation or Worker selection. Early
retries return 429 key_retry_cooldown with the remaining Retry-After. Repeated
polling does not move this deadline, though it still consumes the Key's original
RPM budget and can trigger that separate limit. The final dispatch gate rechecks
the deadline for queued work; already dispatched requests are not cancelled.
Other Keys are unaffected. The deadline survives restart and automatically expires
using database time. This uses one additional key-cooldown row per affected Key,
with one timestamp, in the existing table; no schema migration is needed.
