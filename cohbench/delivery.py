"""One T3 receipt timestamp rule for live delivery facts and trajectory export."""
from datetime import datetime


def _time(value):
    if isinstance(value, datetime) or value is None:
        return value
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def delivery_instant(item, *, queued=None, request=None, run=None, recorded=None,
                     native=None, restart=False, strict=False, kind="correction", previous=None):
    """Return (instant, source). Strict live reads do not infer unseen receipt.

    Historical exports retain their queue-time fallback when no receipt was
    recorded. Live callers wait for a native resolution or a user item.
    """
    queued = _time(queued or item.get('startedAt') or item.get('updatedAt'))
    inferred_restart = ((previous or {}).get('status') == 'interrupted'
                        and not item.get('freshContext') and not (run or {}).get('freshContext'))
    if kind is None:
        kind = 'correction' if item.get('inputIntent') == 'steer' or inferred_restart else 'reply'
    restart = restart or inferred_restart
    done = _time(item.get('completedAt'))
    if request is not None:
        t, source = _time(request.get('resolvedAt')) or done, 'request resolution'
        if t is None and not strict:
            t = queued
        if recorded is not None:
            t, source = _time(recorded), 'runner'
    elif kind not in ('correction', 'reset_notice'):
        # Main dates an explicit reply at its item timestamp, for every route.
        t, source = queued, 'unknown: queue time used'
    elif recorded is not None:
        t, source = _time(recorded), 'runner'
    elif item.get('inputIntent') == 'steer' and done is not None and (queued is None or done > queued):
        t, source = done, 'T3 item completion'
    elif native is not None:
        t, source = _time(native), 'harness session'
    elif restart and (run or {}).get('startedAt'):
        t, source = _time(run['startedAt']), 'restarted run start'
    else:
        t, source = queued, 'unknown: queue time used'
        if strict and item.get('inputIntent') == 'steer' and done is None:
            t = None
    if t is not None and queued is not None and source != 'runner':
        t = max(t, queued)
    return t, source
