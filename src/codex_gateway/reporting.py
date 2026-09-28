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
from .config import get_settings
from .models import ApiKey, Worker, ModelPrice, SubscriptionCost, SubscriptionPlan, UsageRecord
from .subscriptions import normalize_plan, normalize_plan_color, subscription_summary
from sqlalchemy.dialects.postgresql import insert
from .self_service import render
from .user_auth import require_user
from .history import conversation_history
from .billing import priced_amount
from .request_detail import readable_fields, observation_fields, last_texts

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


@router.get("/user/usage")
async def usage(request: Request, q: str = "", model: str = "", status: str = "", start: str = "", end: str = "", page: int = 1, identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    filters = []
    if q:
        filters.append(UsageRecord.request_id.contains(q, autoescape=True))
    if model:
        filters.append(UsageRecord.model == model)
    if start:
        filters.append(UsageRecord.created_at >= date_boundary(start, "开始"))
    if end:
        filters.append(UsageRecord.created_at < date_boundary(end, "结束"))
    history = await conversation_history(db,
        owner=request.state.user.username if request.state.user.role == "user" else None,
        page=page, filters=filters, status=status)
    return render(request, identity, page="usage", history_groups=history["groups"],
        total=history["total"], request_total=history["request_total"],
        number=history["page"], pages=history["pages"], show_cost=True)


@router.get("/user/usage/{request_id}")
async def detail(request: Request, request_id: str, identity=Depends(require_user), db: AsyncSession = Depends(get_session)):
    record = await db.scalar(usage_query(request.state.user).where(UsageRecord.request_id == request_id))
    if not record:
        raise HTTPException(404, "请求不存在")
    worker = await db.get(Worker, record.worker_id) if record.worker_id else None
    return render(request, identity, page="detail", record=record, worker=worker,
                  evidence_fields=readable_fields(record.conversation_evidence),
                  observation_fields=observation_fields(record.request_observation),
                  last_texts=last_texts(record.request_params), params=json.dumps(record.request_params, ensure_ascii=False, indent=2), observation=json.dumps(record.request_observation, ensure_ascii=False, indent=2), correlation=json.dumps(record.conversation_evidence, ensure_ascii=False, indent=2))


@router.get("/admin/finance")
async def finance(request: Request, month: str = "", identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    prices = (await db.scalars(select(ModelPrice).order_by(ModelPrice.model))).all()
    price_map = {price.model: price for price in prices}
    price_rows = [{"model": model, "price": price_map.get(model)} for model in sorted(set(get_settings().public_models()) | set(price_map))]
    subscriptions = await subscription_summary(db)
    await db.commit()
    return render(request, identity, page="finance", price_rows=price_rows, subscriptions=subscriptions)


def summarize_latest(rows, prices):
    """Revalue token aggregates without reading or changing historical costs."""
    def empty():
        return dict(requests=0, input_tokens=0, output_tokens=0, cache_read_tokens=0,
                    cache_write_tokens=0, amount=Decimal(0), unpriced=0)
    total, users, workers = empty(), {}, {}
    for row in rows:
        owner, key_id, key_name, worker_id, worker_name, model, count, inp, out, cache_read, cache_write = row
        price = prices.get(model)
        amount = priced_amount(inp, out, cache_read, cache_write, price) if price else Decimal(0)
        key_group = users.setdefault((owner, key_id), {**empty(), "owner": owner or "开发 / 未归属", "key_id": str(key_id) if key_id else "—", "name": key_name or "开发 Key / 未知 Key"})
        worker_group = workers.setdefault(worker_id, {**empty(), "name": worker_name or "未分配 Worker", "worker_id": str(worker_id) if worker_id else "—"})
        for group in (total, key_group, worker_group):
            group["requests"] += count
            group["input_tokens"] += inp
            group["output_tokens"] += out
            group["cache_read_tokens"] += cache_read
            group["cache_write_tokens"] += cache_write
            group["amount"] += amount
            group["unpriced"] += 0 if price else count
    return total, sorted(users.values(), key=lambda g: g["amount"], reverse=True), sorted(workers.values(), key=lambda g: g["amount"], reverse=True)


@router.get("/admin/reports")
async def financial_reports(request: Request, month: str = "", identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    query = select(UsageRecord.owner_username, UsageRecord.api_key_id, ApiKey.name,
                   UsageRecord.worker_id, Worker.name, UsageRecord.model, func.count(),
                   func.sum(UsageRecord.input_tokens), func.sum(UsageRecord.output_tokens),
                   func.sum(UsageRecord.cache_read_tokens), func.sum(UsageRecord.cache_write_tokens))
    query = query.outerjoin(ApiKey, UsageRecord.api_key_id == ApiKey.id).outerjoin(Worker, UsageRecord.worker_id == Worker.id)
    if month != "all":
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month) or not 1 <= int(month[:4]) <= 9998:
            raise HTTPException(400, "月份格式应为 YYYY-MM")
        first = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
        end = first.replace(year=first.year+1, month=1) if first.month == 12 else first.replace(month=first.month+1)
        query = query.where(UsageRecord.created_at >= first, UsageRecord.created_at < end)
    rows = (await db.execute(query.group_by(UsageRecord.owner_username, UsageRecord.api_key_id, ApiKey.name, UsageRecord.worker_id, Worker.name, UsageRecord.model))).all()
    prices = {price.model: price for price in (await db.scalars(select(ModelPrice))).all()}
    total, user_rows, worker_rows = summarize_latest(rows, prices)
    subscriptions = await subscription_summary(db)
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    live_cost = subscriptions["total"] if not subscriptions["unpriced"] else None
    if month == current_month:
        cost_amount = live_cost
        cost_note = "本月按当前已登录 Worker × 最新套餐月费自动重算"
    elif month == "all":
        historical = await db.scalar(select(func.sum(SubscriptionCost.amount)).where(SubscriptionCost.month < current_month))
        cost_amount = (historical or Decimal(0)) + live_cost if live_cost is not None else None
        cost_note = "历史已录入成本 + 本月实时成本；历史未录入月份未计入"
    else:
        cost = await db.get(SubscriptionCost, month)
        cost_amount = cost.amount if cost else None
        cost_note = "所选月份手工录入的历史成本"
    cost_missing = "套餐未定价" if month in (current_month, "all") else "未录入"
    await db.commit()
    return render(request, identity, page="reports", subscriptions=subscriptions, month=month, total=total, user_rows=user_rows,
                  worker_rows=worker_rows, cost_amount=cost_amount, current_month=current_month, cost_note=cost_note, cost_missing=cost_missing,
                  savings=total["amount"]-cost_amount if cost_amount is not None else None)


def valid_amount(value):
    if not value.is_finite() or value < 0 or value > Decimal("999999999"):
        raise HTTPException(400, "金额必须为 0 至 999999999 的有限数字")
    return value


@router.post("/admin/prices")
async def price(request: Request, model: str = Form(..., min_length=1, max_length=120), input_price: Decimal = Form(...), output_price: Decimal = Form(...), cache_read_price: Decimal | None = Form(None), cache_write_price: Decimal | None = Form(None), csrf_token: str = Form(...), identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    model = model.strip()
    if not model:
        raise HTTPException(400, "模型名称不能为空")
    record = await db.get(ModelPrice, model)
    if not record:
        record = ModelPrice(model=model)
        db.add(record)
    record.input_price, record.output_price = valid_amount(input_price), valid_amount(output_price)
    record.cache_read_price = valid_amount(cache_read_price if cache_read_price is not None else input_price)
    record.cache_write_price = valid_amount(cache_write_price if cache_write_price is not None else input_price)
    await db.commit()
    return {"message": "价格已保存，财务报表按最新价格重算；请求记录中的历史快照保持不变"}


@router.post("/admin/subscription-plans")
async def subscription_plan(request: Request, name: str = Form(..., min_length=1, max_length=120), monthly_price: Decimal = Form(...), weight: Decimal = Form(Decimal("1")), color: str | None = Form(None, max_length=7), csrf_token: str = Form(...), identity=Depends(require_admin), db: AsyncSession = Depends(get_session)):
    verify_csrf(request, identity, csrf_token)
    name = normalize_plan(name)
    if not name:
        raise HTTPException(400, "套餐名称不能为空")
    amount = valid_amount(monthly_price)
    weight = valid_amount(weight)
    if weight <= 0 or weight.as_tuple().exponent < -6:
        raise HTTPException(400, "套餐权重必须大于 0，最多六位小数")
    values = {'name': name, 'monthly_price': amount, 'weight': weight}
    updates = {'monthly_price': amount, 'weight': weight}
    if color is not None:
        try:
            values['color'] = updates['color'] = normalize_plan_color(color)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    await db.execute(insert(SubscriptionPlan).values(**values).on_conflict_do_update(
        index_elements=['name'], set_=updates))
    await db.commit()
    return {"message": "套餐月费、权重及胶囊颜色已保存，权重从下一次用量采样生效"}


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


@router.get("/user/debug")
async def debug(request: Request, identity=Depends(require_user)):
    return render(request, identity, page="debug")
