"""Retention uses confirmed account state and actual Docker stop timestamps."""
from datetime import datetime, timedelta, timezone

RETENTION = timedelta(days=7)


def stopped_since(attrs):
    state = attrs.get("State", {})
    if state.get("Status") not in {"exited", "dead"}:
        return None
    value = state.get("FinishedAt", "")
    if not value or value.startswith("0001-"):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
