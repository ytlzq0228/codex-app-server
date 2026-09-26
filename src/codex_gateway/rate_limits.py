"""Read-only projection of Worker account usage windows."""
from datetime import datetime, timezone


def summarize_windows(payload):
    buckets = payload.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        buckets = {'default': payload.get('rateLimits') or {}}
    result = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        windows = {'five_hour': None, 'week': None}
        for field in ('primary', 'secondary'):
            window = bucket.get(field)
            if not isinstance(window, dict):
                continue
            slot = {300: 'five_hour', 10080: 'week'}.get(window.get('windowDurationMins'))
            used = window.get('usedPercent')
            if not slot or not isinstance(used, (int, float)) or isinstance(used, bool):
                continue
            reset = window.get('resetsAt')
            windows[slot] = {'remaining': max(0, min(100, 100-used)),
                             'resets_at': reset if isinstance(reset, (int, float)) and not isinstance(reset, bool) else None}
        result.append({'name': bucket.get('limitName') or (key if key != 'default' else '账号额度'), **windows})
    return {'buckets': result, 'checked_at': datetime.now(timezone.utc).isoformat()}
