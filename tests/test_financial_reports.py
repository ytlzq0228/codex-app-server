from decimal import Decimal
from types import SimpleNamespace
from codex_gateway.reporting import summarize_latest


def test_latest_price_revaluation_and_dimension_totals():
    rows = [('alice','key1','Key 1','w1','Worker 1','model',2,1000000,500000),
            ('bob','key1','Key 1','w2','Worker 2','model',1,2000000,0),
            ('alice','key2','Key 2',None,None,'unknown',3,100,50)]
    prices = {'model':SimpleNamespace(input_price=Decimal('2'),output_price=Decimal('8'))}
    total, users, workers = summarize_latest(rows, prices)
    assert total['amount'] == Decimal('10')
    assert total['requests'] == 6 and total['unpriced'] == 3
    assert len(users) == 3 and len(workers) == 3
    for groups in (users,workers):
        for metric in ('amount','requests','input_tokens','output_tokens','unpriced'):
            assert sum(g[metric] for g in groups) == total[metric]
    prices['model'].input_price = Decimal('4')
    assert summarize_latest(rows,prices)[0]['amount'] == Decimal('16')
    assert rows[0][6:] == (2,1000000,500000)


def test_empty_and_zero_priced_usage():
    assert summarize_latest([], {})[0]['amount'] == 0
    total,_,_ = summarize_latest([(None,None,None,None,None,'free',1,10,20)], {'free':SimpleNamespace(input_price=Decimal(0),output_price=Decimal(0))})
    assert total['amount'] == 0 and total['unpriced'] == 0


def test_report_recalculates_live_subscription_cost(monkeypatch):
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from codex_gateway.main import app
    from codex_gateway.database import SessionLocal
    from codex_gateway.models import SubscriptionCost
    from test_self_service import admin_login
    import codex_gateway.reporting as reporting

    current = datetime.now(timezone.utc).strftime('%Y-%m')
    live = dict(total=Decimal('60'), unpriced=0, rows=[], count=3, unknown=0)
    async def summary(db):
        return live
    monkeypatch.setattr(reporting, 'subscription_summary', summary)
    async def old_manual_cost():
        async with SessionLocal() as db:
            record = await db.get(SubscriptionCost, current)
            if not record:
                record = SubscriptionCost(month=current)
                db.add(record)
            record.amount = Decimal('999')
            await db.commit()
    with TestClient(app) as client:
        admin_login(client)
        client.portal.call(old_manual_cost)
        for price in ['60','75']:
            live['total'] = Decimal(price)
            response = client.get('/admin/reports?month='+current)
            assert response.status_code == 200
            assert f'<strong>{price}.00 / ' in response.text
            assert '本月按当前已登录 Worker' in response.text
            assert 'action="/admin/subscription-cost"' not in response.text
        response = client.get('/admin/reports?month=all')
        assert '历史已录入成本 + 本月实时成本' in response.text
        live['unpriced'] = 1
        for month in [current, 'all']:
            assert '<strong>套餐未定价 / —</strong>' in client.get('/admin/reports?month='+month).text
        live.update(total=Decimal(0), unpriced=0, count=0)
        assert '<strong>0.00 / ' in client.get('/admin/reports?month='+current).text
        assert 'action="/admin/subscription-cost"' in client.get('/admin/reports?month=2000-01').text
