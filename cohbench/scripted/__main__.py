"""Command line.

    python -m cohbench.scripted serve [--api HOST:PORT] [--admin HOST:PORT]
                                      [--log-dir DIR] [--script FILE]
    python -m cohbench.scripted validate FILE...
    python -m cohbench.scripted admin [--url URL] state | reset | load FILE|- | next INDEX [TEXT]
                                     | export | import FILE|- [--next-script FILE]

Exit codes: 0 ok, 1 a check or admin call failed, 2 bad usage or a script
that does not validate.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import threading
import urllib.error
import urllib.request

from .engine import Episode
from .script import ScriptError, load_script, load_script_file
from .server import App


def _addr(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not re.fullmatch(r"[0-9]{1,5}", port) or not 0 <= int(port) <= 65535:
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {value!r}")
    return host, int(port)


def cmd_serve(a: argparse.Namespace) -> int:
    episode = Episode()
    if a.script:
        try:
            episode.load(load_script_file(a.script))
        except ScriptError as e:
            print(e, file=sys.stderr)
            return 2
    app = App(episode, a.log_dir)
    api, admin = app.start(a.api, None if a.admin is None else a.admin)
    print(f"scripted: api on {api[0]}:{api[1]}, admin on "
          f"{'none' if admin is None else f'{admin[0]}:{admin[1]}'}", file=sys.stderr, flush=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    app.stop()
    return 0


def cmd_validate(a: argparse.Namespace) -> int:
    bad = 0
    for path in a.files:
        try:
            s = load_script_file(path)
            print(f"ok  {path}  handlers={len(s.handlers)} children={ {k: len(v) for k, v in s.children.items()} }")
        except ScriptError as e:
            bad += 1
            print(f"bad {path}\n{e}")
        except OSError as e:
            bad += 1
            print(f"bad {path}: {e}")
    return 2 if bad else 0


def _read_json(path: str, what: str) -> tuple[bytes | None, str | None]:
    """The bytes of a JSON file (or stdin for "-"), or an error message."""
    try:
        raw = sys.stdin.buffer.read() if path == "-" else open(path, "rb").read()
    except OSError as e:
        return None, f"{what}: cannot read {path}: {e.strerror or e}"
    try:
        json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        return None, f"{what}: {path} is not JSON: {e}"
    return raw, None


def cmd_admin(a: argparse.Namespace) -> int:
    base = a.url.rstrip("/")
    if a.action == "state":
        req = urllib.request.Request(base + "/state")
    elif a.action == "export":
        req = urllib.request.Request(base + "/export")
    elif a.action == "reset":
        req = urllib.request.Request(base + "/reset", data=b"", method="POST")
    elif a.action == "next":
        if a.file is None or not re.fullmatch(r"[0-9]{1,9}", a.file):
            print("next needs INDEX, a non-negative integer", file=sys.stderr)
            return 2
        body = {"index": int(a.file)}
        if a.text is not None:
            body["text"] = a.text
        req = urllib.request.Request(base + "/next", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    elif a.action == "import":
        if a.file is None:
            print('import needs FILE (or - for stdin): an export, or {"state": ..., "next_script": "<script text>"}',
                  file=sys.stderr)
            return 2
        raw, err = _read_json(a.file, "import")
        if err:
            print(err, file=sys.stderr)
            return 2
        # The bytes are forwarded as read, never parsed and written again, so
        # no number in them is rounded on the way.
        doc = json.loads(raw)
        wrapper = isinstance(doc, dict) and "state" in doc
        body = raw if wrapper else b'{"state": ' + raw + b"}"
        if a.next_script:
            if wrapper and "next_script" in doc:
                print("import: the file already carries a next_script; give it once", file=sys.stderr)
                return 2
            nraw, err = _read_json(a.next_script, "--next-script")
            if err:
                print(err, file=sys.stderr)
                return 2
            try:
                ntext = nraw.decode("utf-8")
            except UnicodeDecodeError as e:
                print(f"--next-script: {a.next_script} is not UTF-8: {e}", file=sys.stderr)
                return 2
            body = body.rstrip()
            body = body[:-1] + b', "next_script": ' + json.dumps(ntext).encode() + b"}"
        req = urllib.request.Request(base + "/import", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    else:  # load
        if a.file is None:
            print("load needs a FILE (or - for stdin)", file=sys.stderr)
            return 2
        raw, err = _read_json(a.file, "load")
        if err:
            print(err, file=sys.stderr)
            return 2
        req = urllib.request.Request(base + "/script", data=raw, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        if a.action == "export":
            # The server's bytes unchanged, so the export imports exactly as large as it was checked.
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
            return 0
        body = json.loads(raw)
    except urllib.error.HTTPError as e:
        print(e.read().decode("utf-8", "replace"))
        return 1
    except OSError as e:
        print(f"admin call failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=1, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m cohbench.scripted")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the API and admin listeners")
    s.add_argument("--api", type=_addr, default=("0.0.0.0", 8080))
    s.add_argument("--admin", type=_addr, default=("127.0.0.1", 8081))
    s.add_argument("--log-dir", default=None)
    s.add_argument("--script", default=None)
    v = sub.add_parser("validate", help="check script files")
    v.add_argument("files", nargs="+")
    ad = sub.add_parser("admin", help="talk to a running server's admin listener")
    ad.add_argument("--url", default="http://127.0.0.1:8081")
    ad.add_argument("action", choices=["state", "reset", "load", "next", "export", "import"])
    ad.add_argument("file", nargs="?", help="load and import: FILE or -; next: INDEX")
    ad.add_argument("--next-script", help="import: a replacement script for the next user message on")
    ad.add_argument("text", nargs="?", help="next: the message text, optional")
    a = p.parse_args(argv)
    return {"serve": cmd_serve, "validate": cmd_validate, "admin": cmd_admin}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
