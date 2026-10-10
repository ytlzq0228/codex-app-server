"""Worker list filters shared by the admin query and header controls."""
from sqlalchemy import String, case, cast, func, or_, select
from .models import Worker


def worker_filter_columns():
    return {
        "provider": Worker.provider,
        "plan": func.lower(func.trim(func.coalesce(Worker.plan_type, ""))),
        "status": case(((Worker.status == "offline") & (func.coalesce(Worker.failure_kind, "") != ""), "error"), else_=cast(Worker.status, String)),
        "node": func.coalesce(func.nullif(Worker.node_id, ""), "__local__"),
        "authentication": case((func.coalesce(Worker.auth_mode, "") != "", "logged_in"), else_="logged_out"),
    }


def worker_query(params):
    query = select(Worker).where(Worker.endpoint != "removed://worker")
    for name, column in worker_filter_columns().items():
        value = params.get(name, "").strip()
        if value:
            query = query.where(column == ("" if name == "plan" and value == "__none__" else value))
    search = params.get("q", "").strip()
    if search:
        query = query.where(or_(*(column.icontains(search, autoescape=True) for column in
            (Worker.name, Worker.owner_username, Worker.account_email))))
    return query.order_by(Worker.created_at, Worker.id)


async def worker_filter_options(session):
    options = {}
    for name, column in worker_filter_columns().items():
        values = (await session.scalars(select(column).where(
            Worker.endpoint != "removed://worker").distinct().order_by(column))).all()
        options[name] = ["__none__" if name == "plan" and not value else value for value in values]
    return options
