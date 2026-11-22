"""Command line.

  python -m cohbench.watcher run --root DIR --out DIR [options]
  python -m cohbench.watcher state-at OUT BATCH|last [--json]
  python -m cohbench.watcher diff OUT BATCH_A BATCH_B [--json]
  python -m cohbench.watcher mark OUT LABEL [--at UTC] [--limit-s S] [--timeout S]
  python -m cohbench.watcher alive OUT [--max-age S]
  python -m cohbench.watcher complete-metrics OUT     (in the watcher image only)
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from datetime import datetime


def _batch(s: str) -> int | str:
    if s == "last":
        return s
    try:
        return int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a batch number: {s!r}") from None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cohbench.watcher")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="watch a tree until SIGTERM")
    r.add_argument("--config", help="JSON config file (keys as in config.py)")
    r.add_argument("--root")
    r.add_argument("--out")
    r.add_argument("--ignore", action="append", default=None, help="glob relative to the root; repeatable")
    r.add_argument("--max-blob-bytes", type=int)
    r.add_argument("--quiet-ms", type=int)
    r.add_argument("--max-delay-ms", type=int)
    r.add_argument("--metric-cmd", help="argv as a JSON list")
    r.add_argument("--metric-timeout-s", type=float)
    r.add_argument("--metric-drain-s", type=float, help="at shutdown, how long the metric queue is drained (default 45)")
    r.add_argument("--metric-copy-mtime", type=int, help="mtime (Unix seconds) of every entry in a metric copy (default 946684800)")
    r.add_argument("--metric-stdout-max", type=int)
    r.add_argument("--metric-stderr-max", type=int)
    r.add_argument("--heartbeat-s", type=float)
    r.add_argument("--work-dir")
    r.add_argument("--ignore-names", help="JSON list of basenames ignored at any depth (default CLAUDE.md, AGENTS.md)")
    r.add_argument("--ignore-name-forms", help="JSON list of name forms ({name} stands for an ignored name; default: the name and its temporary forms)")
    r.add_argument("--ignore-git-refs", help="JSON list of git ref namespaces left out (default refs/t3/)")
    r.add_argument("--workspace-path")
    r.add_argument("--metric-isolation", choices=["auto", "users", "none"])
    r.add_argument("--watcher-user")
    r.add_argument("--metric-user")

    s = sub.add_parser("state-at", help="tree manifest after a batch")
    s.add_argument("out")
    s.add_argument("batch", type=_batch)
    s.add_argument("--json", action="store_true")

    d = sub.add_parser("diff", help="differences between two batch boundaries")
    d.add_argument("out")
    d.add_argument("batch_a", type=_batch)
    d.add_argument("batch_b", type=_batch)
    d.add_argument("--json", action="store_true")

    k = sub.add_parser("mark", help="ask a running watcher which batch was in effect at an instant, once everything written up to it is committed (a mark never closes a batch)")
    k.add_argument("out")
    k.add_argument("label")
    k.add_argument("--at", help="the instant the mark names, UTC ISO 8601 (default: the time the watcher takes the request)")
    k.add_argument("--limit-s", type=float, help="how long the watcher may take to confirm coverage (default 10; above the hold limit)")
    k.add_argument("--timeout", type=float, default=120.0, help="how long this command waits for the acknowledgement")

    a = sub.add_parser("alive", help="exit 0 if the heartbeat is fresh and the watcher is running (reads OUT/heartbeat.json; run it where the output directory is)")
    a.add_argument("out")
    a.add_argument("--max-age", type=float, default=5.0)

    c = sub.add_parser("complete-metrics", help="in the watcher image only: measure, after the episode, every committed batch that has no measured reading, exactly as the live worker would (settings from OUT/config.json)")
    c.add_argument("out")

    args = ap.parse_args(argv)

    if args.cmd == "run":
        from .config import load
        from .watcher import main_run

        metric_cmd = json.loads(args.metric_cmd) if args.metric_cmd is not None else None
        cfg = load(args.config, {
            "root": args.root, "out": args.out, "ignore": args.ignore,
            "max_blob_bytes": args.max_blob_bytes, "quiet_ms": args.quiet_ms, "max_delay_ms": args.max_delay_ms,
            "metric_cmd": metric_cmd, "metric_timeout_s": args.metric_timeout_s, "metric_stdout_max": args.metric_stdout_max,
            "metric_drain_s": args.metric_drain_s, "metric_copy_mtime": args.metric_copy_mtime,
            "metric_stderr_max": args.metric_stderr_max, "heartbeat_s": args.heartbeat_s, "work_dir": args.work_dir,
            "ignore_names": json.loads(args.ignore_names) if args.ignore_names is not None else None,
            "ignore_name_forms": json.loads(args.ignore_name_forms) if args.ignore_name_forms is not None else None,
            "ignore_git_refs": json.loads(args.ignore_git_refs) if args.ignore_git_refs is not None else None,
            "workspace_path": args.workspace_path, "metric_isolation": args.metric_isolation,
            "watcher_user": args.watcher_user, "metric_user": args.metric_user,
        }, validate=False)
        return main_run(cfg)

    if args.cmd == "mark":
        from .records import write_json_atomic
        from .watcher import ACK, CONTROL, REQUEST

        ctl = os.path.join(args.out, CONTROL)
        rid = f"{int(time.time() * 1000):013d}-{secrets.token_hex(4)}"
        req = {"label": args.label}
        if args.at is not None:
            req["at"] = args.at
        if args.limit_s is not None:
            req["limit_s"] = args.limit_s
        write_json_atomic(os.path.join(ctl, rid + REQUEST), req)
        ack = os.path.join(ctl, rid + ACK)
        end = time.monotonic() + args.timeout
        while time.monotonic() < end:
            if os.path.exists(ack):
                with open(ack, encoding="utf-8") as f:
                    obj = json.load(f)
                print(json.dumps(obj))
                return 0 if obj.get("ok") else 1
            time.sleep(0.05)
        print(json.dumps({"ok": False, "error": "no acknowledgement before the timeout", "id": rid}))
        return 2

    from . import replay

    if args.cmd == "state-at":
        st = replay.state_at(args.out, args.batch)
        if args.json:
            json.dump(st.as_dict(), sys.stdout, indent=1, sort_keys=True)
            sys.stdout.write("\n")
        else:
            for p, e in sorted(st.entries.items()):
                print(f"{e.type:7} {e.mode:04o} {e.uid}:{e.gid} {e.sha256 or '-':64} {p}" + (f" -> {e.target}" if e.target is not None else ""))
            if st.git is not None:
                print(f"git     HEAD {st.git.get('head')} ({st.git.get('backend')})")
                for name, v in sorted((st.git.get("refs") or {}).items()):
                    print(f"git     {name} {v}")
        return 0

    if args.cmd == "diff":
        diffs = replay.diff(args.out, args.batch_a, args.batch_b)
        if args.json:
            json.dump([{"path": x.path, "status": x.status, "old_path": x.old_path, "note": x.note, "text": x.text} for x in diffs], sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(replay.render(diffs))
        return 0

    if args.cmd == "complete-metrics":
        from .offline import main_complete

        try:
            res = main_complete(args.out)
        except RuntimeError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
        print(json.dumps(res))
        return 0

    if args.cmd == "alive":
        from .records import HEARTBEAT

        path = os.path.join(args.out, HEARTBEAT)
        try:
            with open(path, encoding="utf-8") as f:
                hb = json.load(f)
            wall = datetime.fromisoformat(hb["wall"].replace("Z", "+00:00")).timestamp()
        except (OSError, ValueError, KeyError) as e:
            print(f"no readable heartbeat: {e}", file=sys.stderr)
            return 2
        age = time.time() - wall
        ok = hb.get("status") == "running" and age <= args.max_age
        print(json.dumps({"ok": ok, "status": hb.get("status"), "age_s": round(age, 3), "batch": hb.get("batch"), "error": hb.get("error")}))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
