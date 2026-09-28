"""Idempotent additive upgrade for existing PostgreSQL installations."""
from sqlalchemy import select, text, update
from .models import AdminUser, ApiKey, User, Worker
from .security import hash_password
from .worker_names import archived_worker_name


async def upgrade(connection):
    for table in ("workers", "response_bindings", "execution_sessions", "usage_records", "model_prices"):
        await connection.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS provider VARCHAR(16) NOT NULL DEFAULT 'codex'"))
        await connection.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_provider ON {table} (provider)"))
    await connection.execute(text("ALTER TABLE workers ADD COLUMN IF NOT EXISTS provider_project VARCHAR(180)"))
    for statement in (
        "ALTER TABLE workers ADD COLUMN IF NOT EXISTS execution_generation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE response_bindings ADD COLUMN IF NOT EXISTS worker_generation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE response_bindings ALTER COLUMN expires_at DROP NOT NULL",
        "ALTER TABLE execution_sessions ADD COLUMN IF NOT EXISTS invalid_reason VARCHAR(80)",
        "UPDATE response_bindings SET status='active', invalid_reason=NULL WHERE status='expired' AND invalid_reason='Session TTL expired'",
        "UPDATE response_bindings SET expires_at=NULL WHERE expires_at IS NOT NULL",
        "UPDATE execution_sessions SET expires_at=NULL WHERE state='ready' AND expires_at IS NOT NULL",
    ):
        await connection.execute(text(statement))
    # Only the first quota upgrade grants legacy enabled keys a base allowance.
    has_quota = await connection.scalar(text("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='quota_granted' AND table_schema=current_schema())"))
    await connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_granted INTEGER NOT NULL DEFAULT 0"))
    if not has_quota:
        await connection.execute(text("UPDATE users u SET quota_granted=(SELECT COUNT(*) FROM api_keys k WHERE k.owner_username=u.username AND k.enabled AND k.deleted_at IS NULL)"))
    for name in ("cache_read_price", "cache_write_price"):
        exists = await connection.scalar(text("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='model_prices' AND column_name=:name AND table_schema=current_schema())"), {"name": name})
        await connection.execute(text(f"ALTER TABLE model_prices ADD COLUMN IF NOT EXISTS {name} NUMERIC(18,6) NOT NULL DEFAULT 0"))
        if not exists:
            await connection.execute(text(f"UPDATE model_prices SET {name}=input_price"))
    for statement in (
        "ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS weight NUMERIC(18,6) NOT NULL DEFAULT 1",
        "ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS color VARCHAR(7) NOT NULL DEFAULT '#16734a'",
        "ALTER TABLE workers ALTER COLUMN name TYPE VARCHAR(180)",
        "ALTER TABLE workers ADD COLUMN IF NOT EXISTS owner_username VARCHAR(120) REFERENCES users(username)",
        "ALTER TABLE workers ADD COLUMN IF NOT EXISTS account_email VARCHAR(320)",
        "ALTER TABLE workers ADD COLUMN IF NOT EXISTS account_checked_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS ix_workers_owner_username ON workers(owner_username)",
        "INSERT INTO subscription_plans (name) SELECT DISTINCT CASE WHEN provider='codex' THEN '' ELSE provider || ':' END || lower(trim(plan_type)) FROM workers WHERE plan_type IS NOT NULL AND trim(plan_type) <> '' ON CONFLICT (name) DO NOTHING",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS owner_username VARCHAR(120) REFERENCES users(username)",
        "CREATE INDEX IF NOT EXISTS ix_api_keys_owner_username ON api_keys(owner_username)",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS owner_username VARCHAR(120)",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS request_params JSON",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS response_text TEXT",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS request_observation JSON",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS logical_conversation_id VARCHAR(80)",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS conversation_evidence JSON",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS history_expected_hash VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_usage_records_logical_conversation_id ON usage_records (logical_conversation_id)",
        "CREATE INDEX IF NOT EXISTS ix_usage_records_history_expected_hash ON usage_records (history_expected_hash)",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS input_price NUMERIC(18,6)",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS output_price NUMERIC(18,6)",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(24,12)",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS cache_read_tokens BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS cache_read_price NUMERIC(18,6)",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS cache_write_price NUMERIC(18,6)",
        "CREATE INDEX IF NOT EXISTS ix_usage_records_owner_username ON usage_records(owner_username)",
    ):
        await connection.execute(text(statement))

    await connection.execute(text("""
        UPDATE subscription_plans target SET monthly_price=old.monthly_price, weight=old.weight, color=old.color
        FROM subscription_plans old
        WHERE target.name='gemini:' || old.name AND target.monthly_price IS NULL AND old.name LIKE 'gcp-%'
        AND EXISTS (SELECT 1 FROM workers WHERE provider='gemini' AND lower(trim(plan_type))=old.name)
        AND NOT EXISTS (SELECT 1 FROM workers WHERE provider='codex' AND lower(trim(plan_type))=old.name)
    """))
    await connection.execute(text("""
        DELETE FROM subscription_plans old WHERE old.name LIKE 'gcp-%' AND
        EXISTS (SELECT 1 FROM workers WHERE provider='gemini' AND lower(trim(plan_type))=old.name)
        AND EXISTS (SELECT 1 FROM subscription_plans target WHERE target.name='gemini:' || old.name)
        AND NOT EXISTS (SELECT 1 FROM workers WHERE provider='codex' AND lower(trim(plan_type))=old.name)
    """))
    await migrate_email_usernames(connection)
    await migrate_deleted_worker_names(connection)


async def bootstrap_users(db, settings):
    for old in (await db.scalars(select(AdminUser))).all():
        if not await db.get(User, old.username):
            db.add(User(username=old.username, password_hash=old.password_hash,
                        session_version=old.session_version, role="superadmin" if old.username == settings.admin_username else "admin"))
    await db.flush()
    if not await db.get(User, settings.admin_username):
        db.add(User(username=settings.admin_username, role="superadmin",
                    password_hash=hash_password(settings.admin_password.get_secret_value())))
    await db.flush()
    await db.execute(update(ApiKey).where(ApiKey.owner_username.is_(None)).values(owner_username=settings.admin_username))
    await db.execute(text("UPDATE usage_records u SET owner_username=k.owner_username FROM api_keys k WHERE u.api_key_id=k.id AND u.owner_username IS NULL"))

    await db.execute(update(Worker).where(Worker.owner_username.is_(None)).values(owner_username=settings.admin_username))


async def migrate_deleted_worker_names(connection):
    """Release names held by historical soft-deleted workers."""
    rows = (await connection.execute(text("""
        SELECT id, name, container_name FROM workers
        WHERE endpoint='removed://worker' ORDER BY created_at, id
    """))).all()
    if not rows:
        return

    names = set((await connection.execute(text("SELECT name FROM workers"))).scalars())
    container_names = set((await connection.execute(text("SELECT container_name FROM workers"))).scalars())
    for row in rows:
        names.discard(row.name)
        container_names.discard(row.container_name)
        name = archived_worker_name(row.name, row.id, names)
        container_name = archived_worker_name(row.container_name, row.id, container_names)
        if name != row.name or container_name != row.container_name:
            await connection.execute(text("""
                UPDATE workers SET name=:name, container_name=:container_name WHERE id=:id
            """), {"id": row.id, "name": name, "container_name": container_name})


async def migrate_email_usernames(connection):
    """Rename primary keys and all identity references in one transaction."""
    import re
    from .usernames import username_prefix
    rows = (await connection.execute(text("SELECT username, email, google_sub FROM users"))).all()
    occupied = {row.username for row in rows}
    mapping = {}
    for row in rows:
        source = row.email if row.google_sub and row.email else row.username
        if "@" not in source:
            continue
        target = username_prefix(source)
        if target == row.username:
            continue
        if target in occupied or target in mapping.values():
            raise RuntimeError(f"邮箱前缀用户名冲突：{target}；迁移已取消，不会合并账号")
        mapping[row.username] = target
    if not mapping:
        return
    # Defer existing foreign keys only during this atomic migration. Their
    # original modes are restored before commit.
    constraints = (await connection.execute(text("""SELECT c.conname, c.conrelid::regclass::text AS tab,
        c.condeferrable, c.condeferred FROM pg_constraint c
        WHERE c.contype='f' AND c.confrelid='users'::regclass"""))).all()
    quote = connection.dialect.identifier_preparer.quote
    for constraint in constraints:
        await connection.execute(text(f'ALTER TABLE {constraint.tab} ALTER CONSTRAINT {quote(constraint.conname)} DEFERRABLE INITIALLY DEFERRED'))
    await connection.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    for old, new in mapping.items():
        params = {"old": old, "new": new}
        await connection.execute(text("UPDATE users SET username=:new, email=CASE WHEN email IS NULL AND strpos(:old, '@')>0 THEN :old ELSE email END WHERE username=:old"), params)
        for table, column in (("user_sessions", "username"), ("api_keys", "owner_username"),
                              ("workers", "owner_username"), ("usage_records", "owner_username"),
                              ("admin_users", "username")):
            await connection.execute(text(f"UPDATE {table} SET {column}=:new WHERE {column}=:old"), params)
        # Display names follow the new username; Docker IDs/volumes stay stable.
        workers = (await connection.execute(text("SELECT id, name FROM workers WHERE owner_username=:new"), params)).all()
        prefix = old + "-worker-"
        for worker in workers:
            if worker.name.startswith(prefix) and re.fullmatch(r"[0-9]{1,2}", worker.name[len(prefix):]):
                await connection.execute(text("UPDATE workers SET name=:name WHERE id=:id"),
                    {"name": new + "-worker-" + worker.name[len(prefix):], "id": worker.id})
    await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    for constraint in constraints:
        mode = ('DEFERRABLE INITIALLY DEFERRED' if constraint.condeferred else 'DEFERRABLE INITIALLY IMMEDIATE') if constraint.condeferrable else 'NOT DEFERRABLE'
        await connection.execute(text(f'ALTER TABLE {constraint.tab} ALTER CONSTRAINT {quote(constraint.conname)} {mode}'))
