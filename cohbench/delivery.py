"""One T3 receipt timestamp rule for live delivery facts and trajectory export."""
from datetime import datetime


def _time(value):
    if isinstance(value, datetime) or value is None:
        return value
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def delivery_instant(item, *, queued=None, request=None, run=None, recorded=None,
                     native=None, restart=False, strict=False):
    """Return (instant, source). Strict live reads do not infer unseen receipt.

    Historical exports retain their queue-time fallback when no receipt was
    recorded. Live callers wait for a native resolution or a user item.
    """
    queued = _time(queued or item.get('startedAt') or item.get('updatedAt'))
    if recorded is not None:
        t, source = _time(recorded), 'runner'
    elif request is not None:
        t = _time(request.get('resolvedAt') or item.get('completedAt'))
        source = 'request resolution'
        if t is None and not strict:
            t = queued
    elif item.get('inputIntent') == 'steer':
        t = _time(item.get('completedAt'))
        source = 'T3 item completion' if t is not None and (queued is None or t > queued) else 'unknown: queue time used'
        if t is None:
            t, source = _time(native), 'harness session'
        if t is None and not strict:
            t, source = queued, 'unknown: queue time used'
    elif native is not None:
        t, source = _time(native), 'harness session'
    elif restart and (run or {}).get('startedAt'):
        t, source = _time(run['startedAt']), 'restarted run start'
    else:
        t, source = queued, 'unknown: queue time used'
    if t is not None and queued is not None and source != 'runner':
        t = max(t, queued)
    return t, source
