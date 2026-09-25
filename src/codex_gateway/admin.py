import hmac
import html
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .app_server import AppServerError, open_app_server
from .database import get_session
from .models import ApiKey, UsageRecord, Worker
from .models import WorkerStatus
from .security import generate_api_key, hash_api_key

router = APIRouter(prefix="/admin", tags=["admin"])
basic = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(basic), settings: Settings = Depends(get_settings)) -> str:
    valid_user = hmac.compare_digest(credentials.username.encode(), b"admin")
    valid_password = hmac.compare_digest(credentials.password.encode(), settings.admin_password.get_secret_value().encode())
    if not (valid_user and valid_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


@router.get("", response_class=HTMLResponse)
async def dashboard(_: str = Depends(require_admin), session: AsyncSession = Depends(get_session)) -> str:
    keys = (await session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))).all()
    workers = (await session.scalars(select(Worker).order_by(Worker.created_at.desc()))).all()
    requests = await session.scalar(select(func.count()).select_from(UsageRecord)) or 0
    input_tokens = await session.scalar(select(func.coalesce(func.sum(UsageRecord.input_tokens), 0))) or 0
    output_tokens = await session.scalar(select(func.coalesce(func.sum(UsageRecord.output_tokens), 0))) or 0
    options = "".join(f"<option value='{w.id}'>{html.escape(w.name)}</option>" for w in workers if w.enabled)
    key_rows = "".join(f"<tr><td>{html.escape(key.name)}</td><td>cag_{key.prefix}_…</td><td>{'是' if key.enabled else '否'}</td><td>{key.scheduling_mode}</td><td><form method='post' action='/admin/keys/{key.id}/toggle'><button>切换状态</button></form></td></tr>" for key in keys)
    worker_rows = "".join(f"<tr><td>{html.escape(worker.name)}</td><td>{worker.status.value}</td><td>{html.escape(worker.auth_mode or '-')}</td><td>{html.escape(worker.plan_type or '-')}</td><td><form method='post' action='/admin/workers/{worker.id}/probe'><button>探测</button></form><form method='post' action='/admin/workers/{worker.id}/login'><button>登录</button></form><form method='post' action='/admin/workers/{worker.id}/state'><button>排空/启用</button></form>{'' if worker.name == 'worker-1' else f'''<form method='post' action='/admin/workers/{worker.id}/delete'><button>删除</button></form>'''}</td></tr>" for worker in workers)
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>Codex Gateway</title><style>body{{font:15px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;color:#17202a}}table{{border-collapse:collapse;width:100%;margin:16px 0 32px}}td,th{{border:1px solid #ddd;padding:8px;text-align:left}}input,button,select{{padding:8px;margin:3px}}form{{display:inline-block}}pre{{background:#f4f5f6;padding:16px;overflow:auto}}</style></head><body><h1>Codex Gateway</h1><p>累计请求：{requests}　输入 Token：{input_tokens}　输出 Token：{output_tokens}　Keys：{len(keys)}　Workers：{len(workers)}</p><form method='post' action='/admin/workers/probe'><button>探测默认 Worker</button></form><form method='post' action='/admin/workers/login'><button>默认 Worker 登录</button></form><h2>创建 API Key</h2><form method='post' action='/admin/keys'><input name='name' required maxlength='120' placeholder='名称'><select name='scheduling_mode'><option value='pooled'>池化</option><option value='pinned'>固定</option></select><select name='pinned_worker_id'><option value=''>自动选择</option>{options}</select><button>创建</button></form><h2>API Keys</h2><table><tr><th>名称</th><th>前缀</th><th>启用</th><th>调度</th><th>操作</th></tr>{key_rows}</table><h2>创建 Worker</h2><form method='post' action='/admin/workers'><input name='name' required pattern='[a-z][a-z0-9-]{{0,47}}' placeholder='worker-2'><button>创建容器</button></form><h2>Workers</h2><table><tr><th>名称</th><th>状态</th><th>认证</th><th>套餐</th><th>操作</th></tr>{worker_rows}</table></body></html>"""


@router.post("/keys", response_class=HTMLResponse)
async def create_key(name: str = Form(min_length=1, max_length=120), scheduling_mode: str = Form("pooled"), pinned_worker_id: str = Form(""), _: str = Depends(require_admin), session: AsyncSession = Depends(get_session)) -> str:
    if scheduling_mode not in {"pooled", "pinned"}:
        raise HTTPException(400, "Invalid scheduling mode")
    pinned_id = UUID(pinned_worker_id) if pinned_worker_id else None
    if scheduling_mode == "pinned" and not pinned_id:
        raise HTTPException(400, "Pinned scheduling requires a worker")
    raw_key, prefix = generate_api_key()
    settings = get_settings()
    session.add(ApiKey(name=name, prefix=prefix, key_hash=hash_api_key(raw_key, settings.key_pepper.get_secret_value()), scheduling_mode=scheduling_mode, pinned_worker_id=pinned_id))
    await session.commit()
    return f"<!doctype html><meta charset='utf-8'><h1>API Key 已创建</h1><p>只显示一次，请立即保存：</p><pre>{raw_key}</pre><p><a href='/admin'>返回管理页</a></p>"


@router.post("/keys/{key_id}/toggle")
async def toggle_key(key_id: UUID, _: str = Depends(require_admin), session: AsyncSession = Depends(get_session)) -> RedirectResponse:
    record = await session.get(ApiKey, key_id)
    if not record:
        raise HTTPException(404, "API key not found")
    record.enabled = not record.enabled
    await session.commit()
    return RedirectResponse("/admin", status_code=303)


async def default_worker(session: AsyncSession, settings: Settings) -> Worker:
    worker = await session.scalar(select(Worker).where(Worker.name == "worker-1"))
    if not worker:
        worker = Worker(name="worker-1", container_name="codex-worker-1", endpoint=settings.app_server_url)
        session.add(worker)
        await session.flush()
    return worker


@router.post("/workers")
async def create_worker(name: str = Form(min_length=1, max_length=48), _: str = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)) -> RedirectResponse:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{settings.manager_url}/workers", json={"name": name}, headers={"Authorization": f"Bearer {settings.manager_token.get_secret_value()}"})
    if response.status_code >= 400:
        raise HTTPException(502, "Worker manager could not create the container")
    data = response.json()
    session.add(Worker(name=name, container_name=data["name"], endpoint=data["endpoint"], status=WorkerStatus.offline))
    await session.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/workers/{worker_id}/state")
async def toggle_worker_state(worker_id: UUID, _: str = Depends(require_admin), session: AsyncSession = Depends(get_session)) -> RedirectResponse:
    worker = await session.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    worker.status = WorkerStatus.draining if worker.status in {WorkerStatus.ready, WorkerStatus.busy} else WorkerStatus.ready
    worker.enabled = worker.status == WorkerStatus.ready
    await session.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/workers/{worker_id}/delete")
async def delete_worker(worker_id: UUID, _: str = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)) -> RedirectResponse:
    worker = await session.get(Worker, worker_id)
    if not worker or worker.name == "worker-1":
        raise HTTPException(404, "Worker not found")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(f"{settings.manager_url}/workers/{worker.container_name}", headers={"Authorization": f"Bearer {settings.manager_token.get_secret_value()}"})
    if response.status_code >= 400:
        raise HTTPException(502, "Worker manager could not remove the container")
    worker.enabled = False
    worker.status = WorkerStatus.offline
    worker.endpoint = "removed://worker"
    await session.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/workers/probe", response_class=HTMLResponse)
async def probe_worker(_: str = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)) -> str:
    worker = await default_worker(session, settings)
    return await probe_worker_record(worker, session, settings)


async def probe_worker_record(worker: Worker, session: AsyncSession, settings: Settings) -> str:
    try:
        async with open_app_server(worker.endpoint, settings.app_server_token.get_secret_value(), settings.app_server_timeout_seconds) as app_server:
            result = await app_server.call("account/read", {"refreshToken": False})
        account = result.get("account") or {}
        worker.status = WorkerStatus.ready
        worker.auth_mode = account.get("type")
        worker.plan_type = account.get("planType")
        message = f"Worker 可连接；账户类型：{worker.auth_mode or '未登录'}；套餐：{worker.plan_type or '-'}"
    except Exception as exc:
        worker.status = WorkerStatus.error
        message = f"Worker 探测失败：{exc}"
    await session.commit()
    return f"<!doctype html><meta charset='utf-8'><p>{message}</p><p><a href='/admin'>返回</a></p>"


@router.post("/workers/{worker_id}/probe", response_class=HTMLResponse)
async def probe_selected_worker(worker_id: UUID, _: str = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)) -> str:
    worker = await session.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    return await probe_worker_record(worker, session, settings)


@router.post("/workers/login", response_class=HTMLResponse)
async def login_worker(_: str = Depends(require_admin), settings: Settings = Depends(get_settings)) -> str:
    return await login_worker_endpoint(settings.app_server_url, settings)


async def login_worker_endpoint(endpoint: str, settings: Settings) -> str:
    try:
        async with open_app_server(endpoint, settings.app_server_token.get_secret_value(), settings.app_server_timeout_seconds) as app_server:
            result = await app_server.call("account/login/start", {"type": "chatgptDeviceCode"})
    except AppServerError as exc:
        raise HTTPException(502, str(exc)) from exc
    url = result.get("verificationUrl", "")
    code = result.get("userCode", "")
    return f"<!doctype html><meta charset='utf-8'><h1>登录 Codex Worker</h1><p>访问：<a href='{url}' target='_blank' rel='noopener'>{url}</a></p><p>输入设备码：</p><pre>{code}</pre><p><a href='/admin'>完成后返回并探测</a></p>"


@router.post("/workers/{worker_id}/login", response_class=HTMLResponse)
async def login_selected_worker(worker_id: UUID, _: str = Depends(require_admin), session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)) -> str:
    worker = await session.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    return await login_worker_endpoint(worker.endpoint, settings)
