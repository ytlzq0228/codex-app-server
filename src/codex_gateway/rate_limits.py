"""Read-only projection of Worker account usage windows."""
from .i18n import t
from datetime import datetime, timezone


def quota_reserve_reached(usage, reserve_percent):
    """Any known account window at the threshold makes a Worker unavailable."""
    return any(isinstance(value, (int, float)) and not isinstance(value, bool)
               and value >= 100 - reserve_percent
               for value in (usage.get('five_hour_used'), usage.get('week_used')))


def account_reserve_reached(payload, reserve_percent):
    return any(quota_reserve_reached({
        'five_hour_used': (bucket.get('five_hour') or {}).get('used'),
        'week_used': (bucket.get('week') or {}).get('used'),
    }, reserve_percent) for bucket in summarize_windows(payload).get('buckets', []))


def provider_usage(payload):
    """Product policy: a confirmed enterprise response without numeric quota is unlimited."""
    if payload.get('available') is False and payload.get('buckets') == []:
        return {**payload, 'unlimited': True, 'message': t('无限制')}
    return payload


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
            windows[slot] = {'used': max(0, min(100, used)),
                             'resets_at': reset if isinstance(reset, (int, float)) and not isinstance(reset, bool) else None}
        result.append({'name': bucket.get('limitName') or (key if key != 'default' else t('账号额度')), **windows})
    return {'buckets': result, 'checked_at': datetime.now(timezone.utc).isoformat()}
