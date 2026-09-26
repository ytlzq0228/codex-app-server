from contextlib import asynccontextmanager
from fastapi.testclient import TestClient
from codex_gateway.main import app
from codex_gateway.rate_limits import summarize_windows
from test_quota_workers import create_person, contribute, probe, summary, worker_services
from test_self_service import user_login, AJAX


def test_windows_are_identified_by_duration_and_preserve_buckets():
    data=summarize_windows({'rateLimits':{'primary':{'usedPercent':20,'windowDurationMins':10080,'resetsAt':1790929665},'secondary':None}})
    assert data['buckets'][0]['five_hour'] is None
    assert data['buckets'][0]['week']=={'remaining':80,'resets_at':1790929665}
    data=summarize_windows({'rateLimitsByLimitId':{'first':{'secondary':{'usedPercent':100,'windowDurationMins':300}},'second':{'primary':{'usedPercent':0,'windowDurationMins':300}}}})
    assert [b['five_hour']['remaining'] for b in data['buckets']]==[0,100]
    assert all(b['week'] is None for b in data['buckets'])


def test_read_rate_limits_owner_isolation_and_failure(worker_services,monkeypatch):
    import codex_gateway.contributions as contributions
    calls=[]
    fail=False
    class Server:
        async def call(self,method,params):
            calls.append(method)
            if fail:raise RuntimeError('secret upstream failure')
            return {'rateLimits':{'primary':{'usedPercent':20,'windowDurationMins':10080}}}
    @asynccontextmanager
    async def opened(*args,**kwargs):yield Server()
    with TestClient(app) as client:
        alice,pw=create_person(client)
        bob,bpw=create_person(client)
        token=user_login(client,alice,pw)
        worker=contribute(client,token)
        url='/user/workers/'+worker+'/rate-limits'
        assert client.post(url,data={'csrf_token':token},headers=AJAX).status_code==409
        probe(client,token,worker)
        monkeypatch.setattr(contributions,'open_app_server',opened)
        response=client.post(url,data={'csrf_token':token},headers=AJAX)
        assert response.status_code==200 and response.json()['buckets'][0]['week']['remaining']==80
        assert calls==['account/rateLimits/read']
        before=client.portal.call(summary,alice)
        fail=True
        response=client.post(url,data={'csrf_token':token},headers=AJAX)
        assert response.status_code==502 and 'secret upstream' not in response.text
        assert client.portal.call(summary,alice)==before
        assert client.post(url,data={'csrf_token':'bad'},headers=AJAX).status_code==403
        token=user_login(client,bob,bpw)
        count=len(calls)
        assert client.post(url,data={'csrf_token':token},headers=AJAX).status_code==404
        assert len(calls)==count
