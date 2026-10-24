# Runtime failure diagnostics

Ingress capacity must be sized for a single active gateway when the proxy uses
active/standby routing. Database budgeting must still include all gateway and
manager processes: standby nodes perform background work and may execute
forwarded requests. Pool limits are per process, not per deployment.

Failure evidence is stored under `conversation_evidence.diagnostic` and emitted
as `Request failure diagnostic` structured log entries. Request IDs correlate
the ingress log with audit records; remote failures preserve the owner's node
and release. The change does not alter public error codes or retry behavior.

| Symptom | Evidence |
| --- | --- |
| Tool session capacity rejection | Global versus per-key scope, counts and limits, waiting/claimed/completed runs, oldest run and pending wait age |
| Conversation busy | Logical conversation, checkpoint response, Worker, lease remaining time, incoming tool count, pending continuation, wait outcome and attempts |
| Cancellation failure | Stable operation ID, submit/poll/acknowledgement phase, attempts, last HTTP status, elapsed time |
| Missing tool owner | Supersede versus abandon path and owner node |
| Claude incomplete/timeout/failure | Exit code when available, event count/type, elapsed and last-progress time, output observed, stderr byte count and bounded categories |
| Database pool exhaustion | Configured pool size/overflow allowance and current checked-out/overflow counts without acquiring a database connection |
| Interrupted HTTP delivery | Observed disconnect, successful send counts/bytes, last send time, elapsed time and exception class |

Worker process evidence is filtered through an allowlist before persistence.
No new diagnostic includes prompts, tool arguments/results, raw stderr, account
credentials, raw request headers or database URLs. Stderr categories are hints,
not proof of a cause; no matching category remains unknown. Last progress time
excludes relay heartbeat events. A disconnect observed by the gateway does not
identify whether the user, proxy, or network initiated it.

Evidence must be captured on deployed gateway and Claude Worker versions.
Historical incidents cannot be retrospectively diagnosed using these new fields.
No database schema migration is required.
