# Service Keys

API Keys have two types: `general_user_key` and `project_service_key`.
Existing keys become `general_user_key` during the additive, idempotent startup
migration. The migration preserves existing limits, ownership, secrets and usage.
New service keys use the same configured monthly budget as ordinary keys
(default: USD 3000). Service keys never consume enabled-key count quota or
participate in automatic quota-driven disabling.

Users apply from **我的账户 → Service Key 申请**. Administrators review applications
under **Service Keys → 申请审批**, including directly reconsidering rejected
applications. Approval creates exactly one key, including under concurrent
approval requests. The user obtains a usable secret through the existing
**刷新 Key** action; the generated initial secret is not retained or disclosed
during approval. Rejected applications retain their review history.

The administrator's **Key 列表** contains service keys only. Provider permissions
are configured using a checkbox dialog for Claude, Codex and Gemini. Empty
permissions deny all providers. Account grants and contributed workers do not
grant permissions to service keys. Scheduling and monthly limits are configured
by administrators. Users can rename, rotate, toggle and delete their own keys.

Administrator suspension is independent of the user's enable/disable setting.
Users cannot clear it, including by rotating a key. Disabling an owner account
does not invalidate service keys. Deleting an account through user management
detaches service keys and makes them unusable; an administrator can transfer
them to an existing enabled account without changing the secret, usage, budget,
Provider permissions or administrator suspension. Deleted keys remain deleted.
Ordinary keys are disabled when their owner is deleted.

Approval, Provider authorization, administrator suspension, owner deletion and
ownership transfer are recorded in `service_key_events`. Application history is
displayed in the approval tab. Secrets are never included in those events.

With `auto_create_schema` enabled, the existing startup schema process creates
the request/event tables and upgrades key columns in a transaction. Deployments
which disable automatic schema creation must run the existing schema upgrade
process before running the new application code.

Validation covers migration idempotence, quota exclusion, permission isolation,
account disable/delete behavior, transfer with the same secret, owner boundaries,
user deletion, concurrent approvals and browser form interactions.
