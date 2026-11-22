"""Settle gateway calls canceled by a recorded driver interrupt.

Only the main gateway's accounting rows are returned to the host. The log
is parsed inside the gateway. No request bodies or headers are returned;
configuration and key files are not opened.
"""

import json
from datetime import datetime, timedelta

from cohbench.env.docker import DockerError

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")

# Runs inside the gateway so its private log bind mount stays there. Return
# only usage fields and the request id needed to identify a canceled call.
READ_USAGE = '''
import json
from pathlib import Path
from gateway.summary import read_log, summarize, episode
path = Path("/var/log/gateway/requests.jsonl")
records = read_log(path) if path.exists() else []
calls = []
for record in records:
    rows = summarize([record])
    if rows:
        calls.append({"id": record.get("id"), **rows[0]})
print(json.dumps({"summary": episode(records), "calls": calls}))
'''


def read_calls(env):
    proc = env.site.gateway.exec(["python", "-c", READ_USAGE], check=False, timeout=60)
    if proc.returncode:
        raise DockerError(f"gateway usage rows failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout)


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def settled_usage(snapshot, interrupts):
    """Unknown-outcome calls overlapping a confirmed interrupt are settled.

    A completed call with missing usage is still incomplete. So is an
    unknown-outcome call that ended before, or started after, the interrupt.
    Tokens remain exactly what the gateway recorded, with zero charged for
    a canceled call that recorded none.
    """
    summary, calls = snapshot["summary"], snapshot["calls"]
    cancelled = []
    pending = []
    for call in calls:
        start = instant(call["start"]) if call.get("start") else None
        duration = call.get("duration_ms")
        end = start + timedelta(milliseconds=duration) if start is not None and isinstance(duration, (int, float)) else None
        window = next((w for w in interrupts if start is not None and end is not None
                       and start <= instant(w["end"]) and end >= instant(w["start"])), None)
        if window is not None and call.get("outcome") == "unknown":
            cancelled.append({"id": call.get("id"), "start": call["start"], "interrupt": window,
                              **{k: call.get(k) for k in TOKEN_FIELDS}})
        else:
            pending.append(call)
    out = {**summary, "recorded": dict(summary), "runner_cancelled_calls": cancelled,
           "interrupts": list(interrupts)}
    out["calls_not_complete"] = sum(c.get("usage_status") != "complete" for c in pending)
    out["usage_status"] = "complete" if out["calls_not_complete"] == 0 else summary.get("usage_status", "unavailable")
    if cancelled and not pending:
        for key in TOKEN_FIELDS:
            if out.get(key) is None:
                out[key] = 0
    return out
