from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient

from codex_gateway.database import SessionLocal
from codex_gateway.main import app
from codex_gateway.models import UsageRecord
from codex_gateway.user_usage import recent_user_usage
from page_helpers import rendered_pages
from test_self_service import admin_login, new_user, user_login


def test_summary_and_browser_templates_without_database():
    import asyncio
    import json
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from codex_gateway.page_data import display_data

    now = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    rows = [SimpleNamespace(day=now.date(), tokens=320, amount=Decimal('1.25'), unpriced=1)]
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
    summary = asyncio.run(recent_user_usage(db, 'alice', now=now))
    assert summary['start'] == '2026-01-31'
    assert len(summary['daily']) == 30
    assert summary['tokens'] == 320 and summary['amount'] == Decimal('1.25')
    assert summary['unpriced'] == 1
    assert all(day['tokens'] == 0 for day in summary['daily'][:-1])
    query = db.execute.call_args.args[0].compile()
    assert 'alice' in query.params.values()
    assert now in query.params.values()
    assert datetime(2026, 1, 31, tzinfo=timezone.utc) in query.params.values()
    common = dict(identity=dict(username='alice', role='user'), recent_usage=display_data(summary),
                  quota=dict(total=0, granted=0, contributed=0, used=0, available=0), keys=[], account_providers=[])
    result = subprocess.run(['node', str(Path(__file__).with_name('page_render.cjs'))],
        input=json.dumps(dict(url='http://testserver/user/account',
            pages=[dict(common, page=page) for page in ['account', 'usage']])),
        text=True, capture_output=True, check=True)
    account, usage = json.loads(result.stdout)
    for html in [account, usage]:
        assert '订阅账号不会按照实际用量收费' in html
        assert '1.2500' in html and '320' in html
        assert '1 条请求未定价' in html
    assert account.count('data-date=') == 30
    assert 'token-trend.js' in account
    assert 'data-token-trend' not in usage
    rows.clear()
    empty = asyncio.run(recent_user_usage(db, 'alice', now=now))
    assert empty['tokens'] == 0 and empty['amount'] == 0 and empty['unpriced'] == 0
    assert len(empty['daily']) == 30


def test_personal_thirty_day_usage():
    with TestClient(app) as client:
        token = admin_login(client)
        username, password = new_user(client, token)
        user_login(client, username, password)

        async def check():
            now = datetime.now(timezone.utc)
            start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)
            async with SessionLocal() as db:
                empty = await recent_user_usage(db, username, now=now)
                assert empty['tokens'] == 0 and empty['amount'] == 0
                assert len(empty['daily']) == 30
                for owner, date, tokens, amount in [
                    (username, start, 100, Decimal('1.25')),
                    (username, now, 200, None),
                    (username, start - timedelta(microseconds=1), 1000, Decimal('50')),
                    (username, now + timedelta(days=1), 2000, Decimal('60')),
                    ('other-' + uuid4().hex, now, 3000, Decimal('70')),
                ]:
                    db.add(UsageRecord(request_id='resp_' + uuid4().hex, owner_username=owner,
                        created_at=date, input_tokens=tokens, output_tokens=10,
                        cache_read_tokens=50, model='test-model', status_code=200, cost_usd=amount))
                await db.commit()
                result = await recent_user_usage(db, username, now=now)
                assert result['tokens'] == 320
                assert result['amount'] == Decimal('1.25')
                assert result['unpriced'] == 1
                assert result['daily'][0]['tokens'] == 110
                assert result['daily'][-1]['tokens'] == 210
                assert all(day['tokens'] == 0 for day in result['daily'][1:-1])
        client.portal.call(check)
        account = client.get('/user/account/data').json()
        usage = client.get('/user/usage/data?model=does-not-exist&page=2').json()
        assert account['recent_usage'] == usage['recent_usage']
        assert account['recent_usage']['tokens'] == 320
        html = rendered_pages(client, '/user/account').text
        assert '订阅账号不会按照实际用量收费' in html
        assert '过去30天每日 Token 趋势' in html
        assert html.count('data-date=') == 30
        assert 'token-trend.js' in html
        shell = client.get('/user/usage').text
        assert '订阅账号不会按照实际用量收费' in shell
