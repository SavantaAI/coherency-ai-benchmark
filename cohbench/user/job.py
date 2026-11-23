"""A cancellable per-output judgement with a completion instant."""
from datetime import timedelta
import threading

from .api import DeadlineExceeded


class JudgementJob:
    def __init__(self, policy, card, transcript, output, *, utcnow, timeout_s=None):
        self.utcnow = utcnow
        self.started_at = utcnow()
        limit = policy.verdict_deadline_s
        self.deadline = self.started_at + timedelta(seconds=min(limit, timeout_s) if timeout_s is not None else limit)
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.value = None
        self.completed_at = None
        self.client = policy.client.fork()
        self.timer = None
        self._arm()
        def work():
            try:
                value = policy.judge(card, transcript, output, client=self.client,
                                     deadline=self.client.clock() + max(0, (self.deadline-self.utcnow()).total_seconds()))
            except DeadlineExceeded:
                value = None
            except Exception as exc:
                value = exc
            with self.lock:
                if not self.done.is_set():
                    self.completed_at = self.utcnow()
                    self.value = value if self.completed_at <= self.deadline else None
                    self.done.set()
                    if self.timer is not None:
                        self.timer.cancel()
        if not self.done.is_set():
            worker = threading.Thread(target=work, daemon=True)
            if hasattr(self.client, "_workers"):
                self.client._workers.append(worker)
            worker.start()

    def _expire(self):
        with self.lock:
            if not self.done.is_set():
                self.completed_at = self.deadline
                cancel = getattr(self.client, "cancel", None)
                if cancel is not None:
                    cancel()
                self.done.set()

    def _arm(self):
        seconds = (self.deadline-self.utcnow()).total_seconds()
        if seconds <= 0:
            self._expire()
        else:
            self.timer = threading.Timer(seconds, self._expire)
            self.timer.daemon = True
            self.timer.start()

    def tighten(self, deadline):
        if deadline < self.deadline and not self.done.is_set():
            self.deadline = deadline
            if self.timer is not None:
                self.timer.cancel()
            self._arm()

    def ready(self):
        if self.utcnow() >= self.deadline:
            self._expire()
        return self.done.is_set()

    def wait(self):
        self.done.wait(max(0, (self.deadline-self.utcnow()).total_seconds()))
        if not self.done.is_set():
            self._expire()
