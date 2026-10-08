from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from codex_gateway.models import Worker, WorkerStatus
from codex_gateway.worker_filters import worker_query


def test_worker_filters_combine_before_pagination_and_match_display():
    engine = create_engine("sqlite://")
    Worker.__table__.create(engine)
    with Session(engine) as db:
        def add(name, **values):
            db.add(Worker(name=name, container_name=name, endpoint="http://worker",
                          **values))
        for index in range(35):
            add(f"match-{index}", provider="claude", plan_type=" Max ",
                node_id="app-2", status=WorkerStatus.offline,
                failure_kind="auth", auth_mode="oauth", account_email="owner@example.com")
        add("offline", provider="claude", status=WorkerStatus.offline)
        add("empty-auth", provider="codex", auth_mode="", node_id="")
        add("literal_%", provider="gemini")
        removed = Worker(name="removed", container_name="removed", endpoint="removed://worker")
        db.add(removed)
        db.commit()
        filters = dict(provider="claude", plan="max", node="app-2",
                       status="error", authentication="logged_in", q="OWNER@")
        query = worker_query(filters)
        assert len(db.scalars(query).all()) == 35
        first = db.scalars(query.limit(30)).all()
        second = db.scalars(query.offset(30).limit(30)).all()
        assert len(first) == 30 and len(second) == 5
        assert not {w.id for w in first} & {w.id for w in second}
        assert not db.scalars(worker_query({**filters, "node": "app-1"})).all()
        offline = db.scalars(worker_query({"status": "offline"})).all()
        assert "match-0" not in {w.name for w in offline}
        empty = db.scalars(worker_query(dict(provider="codex", plan="__none__",
                    node="__local__", authentication="logged_out"))).all()
        assert [w.name for w in empty] == ["empty-auth"]
        assert [w.name for w in db.scalars(worker_query({"q": "_%"}))] == ["literal_%"]
        assert "removed" not in {w.name for w in db.scalars(worker_query({}))}


def test_browser_header_filters_keep_selected_values():
    import json
    import subprocess
    from pathlib import Path
    payload = {
        "url": "http://testserver/admin/workers?provider=claude&plan=max&authentication=logged_in&q=alice",
        "pages": [{"page": "admin_workers", "workers": [], "users": [],
                   "display_names": {}, "plan_styles": {}, "default_plan_style": "",
                   "worker_filters": dict(provider="claude", plan="max", authentication="logged_in", q="alice"),
                   "worker_filter_options": {"provider": ["claude"], "plan": ["max", "__none__"],
                       "status": ["error", "offline"], "node": ["app-1", "__local__"],
                       "authentication": ["logged_in", "logged_out"]}}],
    }
    result = subprocess.run(["node", str(Path(__file__).with_name("page_render.cjs"))],
                            input=json.dumps(payload), capture_output=True, text=True, check=True)
    html = json.loads(result.stdout)[0]
    assert 'value="claude" selected' in html
    assert 'value="max" selected' in html
    assert 'value="logged_in" selected' in html
    assert 'value="alice"' in html
    assert html.count('form="worker-filters"') == 5
    assert 'name="page"' not in html
