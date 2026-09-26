"""Idempotent additive upgrade for existing PostgreSQL installations."""
from sqlalchemy import select, text, update
from .models import AdminUser, ApiKey, User
from .security import hash_password


async def upgrade(connection):
    for statement in (
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS owner_username VARCHAR(120) REFERENCES users(username)",
        "CREATE INDEX IF NOT EXISTS ix_api_keys_owner_username ON api_keys(owner_username)",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS owner_username VARCHAR(120)",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS request_params JSON",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS input_price NUMERIC(18,6)",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS output_price NUMERIC(18,6)",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(24,12)",
        "CREATE INDEX IF NOT EXISTS ix_usage_records_owner_username ON usage_records(owner_username)",
    ):
        await connection.execute(text(statement))


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
