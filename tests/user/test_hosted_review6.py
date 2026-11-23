"""Bounded episode accounting, without an endpoint."""

import threading
import time

from cohbench.user.api import Client, _Attempt
from cohbench.user.config import parse
from .conftest import KEY, config_doc


def test_r6_s2_blocked_completion_cannot_keep_process_alive(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class BlockedLedger:
        def finish(self, *args, **kwargs):
            entered.set()
            release.wait(2)

    client = Client(
        parse(config_doc("http://127.0.0.1:1")), KEY, ledger=BlockedLedger()
    )
    attempt = _Attempt()
    attempt.ticket = 1
    attempt.sent = True
    attempt.reserved.set()
    attempt.submission_done.set()
    try:
        client._record_attempt(attempt, {}, "accepted", 1)
        assert entered.wait(1)
        started = time.monotonic()
        cleanup = client.flush(timeout_s=0.01, require_accounting=False)
        assert time.monotonic() - started < 0.1
        assert cleanup["pending_accounting_threads"] == 1
        assert client._finishes[0].daemon
    finally:
        release.set()
        client.flush()


def test_r6_s2_completion_lock_has_a_deadline(tmp_path):
    import fcntl
    from cohbench.user.ledger import Ledger

    cfg = parse(config_doc("http://127.0.0.1:1"))
    ledger = Ledger(tmp_path / "calls", 2)
    ticket = ledger.start(cfg)
    attempt = _Attempt()
    attempt.ticket = ticket
    attempt.sent = True
    attempt.reserved.set()
    attempt.submission_done.set()
    client = Client(cfg, KEY, ledger=ledger)
    with (tmp_path / "calls.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        client._record_attempt(attempt, {}, "accepted", 1)
        client._finishes[0].join(0.75)
        blocked = client._finishes[0].is_alive()
        fcntl.flock(lock, fcntl.LOCK_UN)
    client._finishes[0].join(1)
    assert not blocked
    assert client.flush(require_accounting=False)["incomplete_accounting_writes"] == 1


def test_r6_s1_readmes_distinguish_new_and_inflight_requests():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for name in ("user", "runner"):
        text = " ".join((root / "cohbench" / name / "README.md").read_text().split())
        assert "a request already being written may finish being sent" in text
