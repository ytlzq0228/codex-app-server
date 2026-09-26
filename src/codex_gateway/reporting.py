"""Usage inspection and historical price snapshots, independent of forwarding."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .admin_auth import require_admin, verify_csrf
from .database import get_session
from .models import ModelPrice, SubscriptionCost, UsageRecord
from .self_service import render
from .user_auth import require_user

router = APIRouter()


def usage_query(user):
    query = select(UsageRecord)
    if user.role == "user":
        query = query.where(UsageRecord.owner_username == user.username)
    return query


def date_boundary(value, label):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(400, f"{label}日期格式应为 YYYY-MM-DD")


@router.get("/usage")
async def usage(request: Request, q: str = "", model: str = "", status: str = "", start: str = "", end: str = "", page: int = 1, identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    query = usage_query(request.state.user)
    if q:
        query = query.where(UsageRecord.request_id.contains(q, autoescape=True))
    if model:
        query = query.where(UsageRecord.model == model)
    if status == "error":
        query = query.where(UsageRecord.status_code >= 400)
    if start:
        query = query.where(UsageRecord.created_at >= date_boundary(start, "开始"))
    if end:
        query = query.where(UsageRecord.created_at < date_boundary(end, "结束"))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    page = max(1, min(page, max(1, (total + 29)//30)))
    records = (await db.scalars(query.order_by(UsageRecord.created_at.desc()).offset((page-1)*30).limit(30))).all()
    return render(request, identity, page="usage", records=records, total=total, number=page, pages=max(1,(total+29)//30))


@router.get("/usage/{request_id}")
async def detail(request: Request, request_id: str, identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    record = await db.scalar(usage_query(request.state.user).where(UsageRecord.request_id == request_id))
    if not record:
        raise HTTPException(404, "请求不存在")
    return render(request, identity, page="detail", record=record, params=json.dumps(record.request_params, ensure_ascii=False, indent=2))


@router.get("/admin/finance")
async def finance(request: Request, month: str = "", identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        first = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(400, "月份格式应为 YYYY-MM")
    next_month = first.replace(year=first.year+1, month=1) if first.month == 12 else first.replace(month=first.month+1)
    rows = (await db.execute(select(UsageRecord.owner_username, UsageRecord.model, func.count(), func.sum(UsageRecord.input_tokens), func.sum(UsageRecord.output_tokens), func.sum(UsageRecord.cost_usd), func.count().filter(UsageRecord.cost_usd.is_(None))).where(UsageRecord.created_at >= first, UsageRecord.created_at < next_month).group_by(UsageRecord.owner_username, UsageRecord.model))).all()
    revenue = sum((row[5] or Decimal(0) for row in rows), Decimal(0))
    cost = await db.get(SubscriptionCost, month)
    prices = (await db.scalars(select(ModelPrice).order_by(ModelPrice.model))).all()
    return render(request, identity, page="finance", month=month, rows=rows, revenue=revenue, cost=cost, savings=revenue-cost.amount if cost else None, prices=prices)


def valid_amount(value):
    if not value.is_finite() or value < 0 or value > Decimal("999999999"):
        raise HTTPException(400, "金额必须为 0 至 999999999 的有限数字")
    return value


@router.post("/admin/prices")
async def price(request: Request, model: str = Form(..., min_length=1, max_length=120), input_price: Decimal = Form(...), output_price: Decimal = Form(...), csrf_token: str = Form(...), identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    model = model.strip()
    if not model:
        raise HTTPException(400, "模型名称不能为空")
    record = await db.get(ModelPrice, model)
    if not record:
        record = ModelPrice(model=model)
        db.add(record)
    record.input_price, record.output_price = valid_amount(input_price), valid_amount(output_price)
    await db.commit()
    return {"message": "价格已保存，仅影响后续请求，历史费用保持不变"}


@router.post("/admin/subscription-cost")
async def subscription_cost(request: Request, month: str = Form(...), amount: Decimal = Form(...), csrf_token: str = Form(...), identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(400, "月份无效")
    record = await db.get(SubscriptionCost, month)
    if not record:
        record = SubscriptionCost(month=month)
        db.add(record)
    record.amount = valid_amount(amount)
    await db.commit()
    return {"message": "当月订阅成本已保存"}


@router.get("/debug")
async def debug(request: Request, identity=Depends(require_user)):
    return render(request, identity, page="debug")
