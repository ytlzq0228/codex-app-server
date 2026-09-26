from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from codex_gateway.main import app
from codex_gateway.database import engine, SessionLocal
from codex_gateway.migrations import migrate_email_usernames
from codex_gateway.models import User, UserSession, ApiKey, Worker, UsageRecord
from codex_gateway.security import hash_password
from test_self_service import admin_login, AJAX


def test_email_username_creation_and_login():
    name='prefix-'+uuid4().hex[:12]
    email=name+'@example.com'
    with TestClient(app) as client:
        token=admin_login(client)
        response=client.post('/admin/users',data={'csrf_token':token,'username':email},headers=AJAX)
        assert response.status_code==200,response.text
        assert response.json()['username']==name
        password=response.json()['secret']
        for login in [name,email]:
            assert client.post('/user/login',data={'username':login,'password':password},follow_redirects=False).status_code==302
        assert client.post('/user/login',data={'username':name+'@another.test','password':password},follow_redirects=False).status_code==401
        token=admin_login(client)
        assert client.post('/admin/users',data={'csrf_token':token,'username':name+'@another.test'},headers=AJAX).status_code==409


def test_atomic_username_migration_preserves_references():
    name='migrate-'+uuid4().hex[:12]
    old=name+'@example.com'
    key_id,worker_id,usage_id=uuid4(),uuid4(),uuid4()
    async def check():
        async with engine.connect() as conn:
            transaction=await conn.begin()
            try:
                await conn.execute(User.__table__.insert().values(username=old,email=old,google_sub=uuid4().hex,password_hash='preserved',role='user',quota_granted=4))
                await conn.execute(UserSession.__table__.insert().values(token_hash=uuid4().hex,username=old,csrf_token='csrf',session_version=1,expires_at=datetime.now(timezone.utc)+timedelta(hours=1)))
                await conn.execute(ApiKey.__table__.insert().values(id=key_id,owner_username=old,name='my-key',prefix=uuid4().hex[:20],key_hash=uuid4().hex))
                await conn.execute(Worker.__table__.insert().values(id=worker_id,owner_username=old,name=old+'-worker-01',container_name=uuid4().hex,endpoint='ws://unchanged'))
                await conn.execute(UsageRecord.__table__.insert().values(id=usage_id,owner_username=old,request_id=uuid4().hex,model='gpt-6-sol',status_code=200))
                await migrate_email_usernames(conn)
                await migrate_email_usernames(conn)
                user=(await conn.execute(select(User.__table__).where(User.username==name))).one()
                assert user.password_hash=='preserved' and user.email==old and user.quota_granted==4
                for table,col in [(UserSession,'username'),(ApiKey,'owner_username'),(Worker,'owner_username'),(UsageRecord,'owner_username')]:
                    assert await conn.scalar(select(getattr(table,col)).where(getattr(table,col)==name))==name
                worker=(await conn.execute(select(Worker.__table__).where(Worker.id==worker_id))).one()
                assert worker.name==name+'-worker-01' and worker.endpoint=='ws://unchanged'
                assert await conn.scalar(select(User.username).where(User.username==old)) is None
            finally:
                await transaction.rollback()
    with TestClient(app) as client:
        client.portal.call(check)


def test_username_migration_conflict_does_not_merge():
    name='conflict-'+uuid4().hex[:12]
    async def check():
        async with engine.connect() as conn:
            transaction=await conn.begin()
            try:
                for username in [name,name+'@example.com']:
                    await conn.execute(User.__table__.insert().values(username=username,role='user'))
                with pytest.raises(RuntimeError,match='冲突'):
                    await migrate_email_usernames(conn)
                assert await conn.scalar(select(User.username).where(User.username==name+'@example.com'))
            finally:
                await transaction.rollback()
    with TestClient(app) as client:
        client.portal.call(check)
