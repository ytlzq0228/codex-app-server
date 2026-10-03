"""Authenticated JSON page responses. ORM objects use explicit display allowlists."""
from datetime import datetime
from decimal import Decimal
from enum import Enum
from functools import wraps
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from . import models

FIELDS = {
    models.User: "username email role enabled must_change_password created_at provider_grants",
    models.ApiKey: "id name prefix owner_username enabled scheduling_mode pinned_worker_id created_at last_used_at deleted_at",
    models.Worker: "id name node_id provider owner_username account_email account_checked_at status auth_mode plan_type enabled created_at last_seen_at failure_kind failure_reason retry_after",
    models.ResponseBinding: "response_id api_key_id worker_id thread_id last_used_at",
    models.ModelPrice: "model input_price output_price cache_read_price cache_write_price",
    models.SubscriptionPlan: "name monthly_price weight color",
    models.GoogleAuthConfig: "enabled client_id redirect_uri trusted_domains",
    models.UsageRecord: "request_id model endpoint status_code created_at duration_ms owner_username cost_usd input_tokens output_tokens cache_read_tokens cache_write_tokens input_price output_price cache_read_price cache_write_price error_code previous_response_id logical_conversation_id thread_id",
}


def display_data(value):
    if type(value) in FIELDS:
        result = {name: display_data(getattr(value, name)) for name in FIELDS[type(value)].split()}
        if isinstance(value, models.User):
            result.update(has_password=bool(value.password_hash), has_google=bool(value.google_sub))
        if isinstance(value, models.GoogleAuthConfig):
            result["has_secret"] = bool(value.client_secret)
        return result
    if isinstance(value, dict):
        return {str(k): display_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [display_data(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported page data: {type(value).__name__}")


def page_response(request, template, context):
    if getattr(request.state, "json_page", False):
        identity = request.state.user
        context = {**context, "identity": {name: display_data(getattr(identity, name, None))
            for name in FIELDS[models.User].split()}}
        return JSONResponse(display_data(context), headers={"Cache-Control": "no-store"})
    return request.app.state.templates.TemplateResponse(request, template, context)


def shell_context(page):
    """Layout-only values: no page data queries are needed to paint the shell."""
    from datetime import timezone
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return {
        "loading": True, "page": page,
        "stats": {"requests": "—", "input_tokens": None, "output_tokens": None},
        "quota": dict.fromkeys(("total", "granted", "contributed", "used", "available"), "—"),
        "total": {"requests": "—", "unpriced": "—", **dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "amount"))},
        "subscriptions": {"count": "—", "total": None, "rows": []},
        "cost_amount": None, "savings": None, "cost_missing": "—",
        "month": month, "current_month": month, "config": None,
        "record": dict.fromkeys(FIELDS[models.UsageRecord].split()),
        "evidence_fields": [], "observation_fields": [],
        "last_texts": {"input_text": "—", "output_text": "—"},
    }


def data_page(router, path, template, page):
    """Register identical auth/query dependencies for the shell and /data route."""
    def decorate(handler):
        @wraps(handler)
        async def endpoint(*args, **kwargs):
            request = kwargs["request"]
            identity = kwargs.get("identity") or kwargs.get("admin")
            if request.url.path.endswith("/data"):
                request.state.json_page = True
                return await handler(*args, **kwargs)
            return request.app.state.templates.TemplateResponse(request, "data-page.html", {
                **shell_context(page), "page_template": template,
                "identity": request.state.user, "csrf_token": identity.csrf_token,
                "show_worker": request.state.user.role in ("admin", "superadmin"),
            }, headers={"Cache-Control": "no-store"})
        router.get(path + "/data")(endpoint)
        router.get(path)(endpoint)
        return endpoint
    return decorate


def page_number(request, name="page"):
    try:
        number = int(request.query_params.get(name, "1"))
        if number < 1:
            raise ValueError()
        return number
    except ValueError:
        raise HTTPException(422, "页码必须为正整数")


async def paginate(db, query, request, *, size=30, name="page"):
    total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    pages = max(1, (total + size - 1) // size)
    page = min(page_number(request, name), pages)
    rows = (await db.scalars(query.offset((page - 1) * size).limit(size))).all()
    return rows, {"page": page, "pages": pages, "total": total, "page_size": size, "parameter": name}


def paginate_list(rows, request, *, size=30, name="page"):
    total = len(rows)
    pages = max(1, (total + size - 1) // size)
    page = min(page_number(request, name), pages)
    return rows[(page - 1) * size:page * size], {
        "page": page, "pages": pages, "total": total, "page_size": size, "parameter": name}
