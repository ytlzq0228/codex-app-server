"""Service-key approval and administration; secrets use the normal rotation flow."""
from uuid import UUID
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from .admin_auth import require_admin, verify_csrf
from .user_auth import require_user
from .database import get_session
from .models import ApiKey, ServiceKeyRequest, ServiceKeyEvent, User, UserSession, Worker, AdminUser
from .page_data import data_page, paginate
from .self_service import render, authorize_role
from .security import generate_api_key, hash_api_key
from .config import get_settings
from .quota import quota_lock

router = APIRouter()
SERVICE = "project_service_key"


def log_event(db, actor, action, *, key=None, application=None, detail=None):
    db.add(ServiceKeyEvent(actor=actor, action=action, key_id=key.id if key else None,
        request_id=application.id if application else None, detail=detail or {}))


@router.post("/user/service-key-requests")
async def apply(request: Request, name: str = Form(min_length=1, max_length=120),
                reason: str = Form(min_length=1, max_length=2000), csrf_token: str = Form(...),
                identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    if not name.strip() or not reason.strip():
        raise HTTPException(400, "请填写名称和申请理由")
    await quota_lock(db)
    owner = await db.get(User, identity.username)
    if not owner:
        raise HTTPException(409, "申请用户已不存在")
    application = ServiceKeyRequest(owner_username=identity.username, name=name.strip(), reason=reason.strip())
    db.add(application)
    await db.flush()
    log_event(db, identity.username, "submitted", application=application)
    await db.commit()
    return {"message": "申请已提交，审批通过后可在我的账户刷新并获取密钥", "request_id": str(application.id)}


@data_page(router, "/admin/service-keys", "account.html", "service_keys")
async def service_keys(request: Request, identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    from .key_limits import key_budget_display
    tab = request.query_params.get("tab", "keys")
    if tab not in {"keys", "requests"}:
        raise HTTPException(422, "无效页签")
    context = {"page": "service_keys", "tab": tab}
    if tab == "requests":
        rows, pagination = await paginate(db, select(ServiceKeyRequest).order_by(
            ServiceKeyRequest.created_at.desc(), ServiceKeyRequest.id), request)
        events = (await db.scalars(select(ServiceKeyEvent).where(ServiceKeyEvent.request_id.in_(
            [r.id for r in rows])).order_by(ServiceKeyEvent.created_at, ServiceKeyEvent.id))).all()
        context.update(applications=rows, events=events, pagination=pagination)
    else:
        keys, pagination = await paginate(db, select(ApiKey).where(ApiKey.key_type == SERVICE,
            ApiKey.deleted_at.is_(None)).order_by(ApiKey.created_at.desc(), ApiKey.id), request)
        context.update(keys=keys, key_budgets=await key_budget_display(db, keys), pagination=pagination,
            users=(await db.scalars(select(User).where(User.enabled.is_(True)).order_by(User.username))).all(),
            workers=(await db.scalars(select(Worker).where(Worker.enabled.is_(True),
                Worker.endpoint != "removed://worker").order_by(Worker.name))).all())
    return render(request, identity, **context)


@router.post("/admin/service-key-requests/{request_id}/review")
async def review(request: Request, request_id: UUID, decision: str = Form(...),
                 note: str = Form("", max_length=2000), csrf_token: str = Form(...),
                 identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    if decision not in {"approve", "reject"}:
        raise HTTPException(400, "无效审批操作")
    await quota_lock(db)
    application = await db.scalar(select(ServiceKeyRequest).where(
        ServiceKeyRequest.id == request_id).with_for_update())
    if not application:
        raise HTTPException(404, "申请不存在")
    if application.key_id or application.status not in {"pending", "rejected"}:
        raise HTTPException(409, "该申请已处理，不能重复创建 Key")
    if not await db.get(User, application.owner_username):
        raise HTTPException(409, "申请用户已删除")
    key = None
    if decision == "approve":
        raw, prefix = generate_api_key()
        key = ApiKey(owner_username=application.owner_username, name=application.name,
            key_type=SERVICE, prefix=prefix,
            key_hash=hash_api_key(raw, get_settings().key_pepper.get_secret_value()))
        db.add(key)
        await db.flush()
        application.key_id = key.id
        application.status = "approved"
    else:
        if not note.strip():
            raise HTTPException(400, "请填写驳回原因")
        application.status = "rejected"
    log_event(db, identity.username, application.status, application=application, key=key,
              detail={"note": note.strip()})
    await db.commit()
    return {"message": "审批已保存；通过后请配置 Provider 权限，用户可在我的账户刷新获取密钥",
            "key_id": str(key.id) if key else None}


@router.post("/admin/service-keys/{key_id}/providers")
async def providers(request: Request, key_id: UUID, claude: bool = Form(False),
                    codex: bool = Form(False), gemini: bool = Form(False), csrf_token: str = Form(...),
                    identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    key = await db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.key_type == SERVICE,
        ApiKey.deleted_at.is_(None)).with_for_update())
    if not key:
        raise HTTPException(404, "Service Key 不存在")
    grants = [name for name, enabled in (("claude", claude), ("codex", codex), ("gemini", gemini)) if enabled]
    log_event(db, identity.username, "providers_updated", key=key,
              detail={"from": key.provider_grants, "to": grants})
    key.provider_grants = grants
    await db.commit()
    return {"message": "Provider 权限已更新"}


@router.post("/admin/users/{username}/delete")
async def delete_user(request: Request, username: str, csrf_token: str = Form(...),
                      identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    await quota_lock(db)
    user = await db.scalar(select(User).where(User.username == username).with_for_update())
    if not user:
        raise HTTPException(404, "用户不存在")
    authorize_role(request.state.user, user.role, user.role)
    if username == identity.username or username == get_settings().admin_username:
        raise HTTPException(400, "不能删除自己或系统初始管理员")
    keys = (await db.scalars(select(ApiKey).where(ApiKey.owner_username == username).with_for_update())).all()
    for key in keys:
        key.owner_username = None
        if key.key_type == SERVICE:
            log_event(db, identity.username, "owner_deleted", key=key, detail={"owner": username})
        else:
            key.enabled = False
    # Retain history and contributions, detach their live identity references.
    await db.execute(update(Worker).where(Worker.owner_username == username).values(owner_username=None))
    await db.execute(delete(UserSession).where(UserSession.username == username))
    await db.execute(delete(AdminUser).where(AdminUser.username == username))
    applications = (await db.scalars(select(ServiceKeyRequest).where(
        ServiceKeyRequest.owner_username == username,
        ServiceKeyRequest.status.in_(["pending", "rejected"])).with_for_update())).all()
    for application in applications:
        application.status = "cancelled"
        log_event(db, identity.username, "owner_deleted", application=application)
    await db.flush()
    await db.delete(user)
    await db.commit()
    return {"message": "用户已删除，Service Key 已失效，可由管理员重新归属"}


@router.post("/user/account/keys/{key_id}/name")
async def rename_key(request: Request, key_id: UUID, name: str = Form(min_length=1, max_length=120),
                     csrf_token: str = Form(...), identity=Depends(require_user),
                     db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    if not name.strip():
        raise HTTPException(400, "名称不能为空")
    key = await db.scalar(select(ApiKey).where(ApiKey.id == key_id,
        ApiKey.owner_username == identity.username, ApiKey.deleted_at.is_(None)).with_for_update())
    if not key:
        raise HTTPException(404, "Key 不存在")
    key.name = name.strip()
    await db.commit()
    return {"message": "Key 名称已更新"}
