"""Typed failure causes in batch classification, without hosted requests."""

import pytest

from cohbench.user.api import Client, EndpointError
from cohbench.user.classifier import Classifier
from cohbench.user.config import parse
from .conftest import KEY, config_doc


@pytest.mark.parametrize("message", ["request timeout after 2 attempts", "ledger unavailable before deadline"])
def test_r4_s1_failure_with_time_left_is_not_an_unjudged_output(message):
    class Failed(Classifier):
        def judge(self, *args, **kwargs):
            raise EndpointError(message)
    cfg = parse(config_doc())
    pol = Failed(cfg, Client(cfg, KEY))
    with pytest.raises(EndpointError, match=message):
        pol.judge_batch({}, (), ["Done."], timeout_s=1)


@pytest.mark.parametrize("deadline,expected", [(25, EndpointError), (10, None)])
def test_r4_s1_attempt_timeout_carries_shared_deadline_cause(deadline, expected):
    from cohbench.user.api import DeadlineExceeded, _Transient
    from .conftest import Clock
    clock = Clock()
    cfg = parse(config_doc(limits={"request_timeout_s": 10, "backoff_s": 0.5, "retries": 1}))
    client = Client(cfg, KEY, clock=clock, sleep=lambda seconds: setattr(clock, "t", clock.t + seconds))
    def timeout(data, cutoff, attempt):
        clock.t = cutoff
        raise _Transient("request timeout")
    client._post = timeout
    with pytest.raises(DeadlineExceeded if expected is None else expected) as caught:
        client.choose([], ["A", "B"], clock.t + deadline)
    assert isinstance(caught.value, DeadlineExceeded) is (expected is None)


def test_r4_s1_typed_deadline_is_an_unjudged_output():
    from cohbench.user.api import DeadlineExceeded
    class Expired(Classifier):
        def judge(self, *args, **kwargs):
            raise DeadlineExceeded("shared cutoff reached")
    cfg = parse(config_doc())
    assert Expired(cfg, Client(cfg, KEY)).judge_batch({}, (), ["Done."], timeout_s=1) == [None]


def test_r4_job_tightened_deadline_cancels_and_discards_late_result():
    import threading
    from datetime import datetime, timezone, timedelta
    from cohbench.runner.policy import Judgement
    entered, release = threading.Event(), threading.Event()
    class Blocked(Classifier):
        def judge(self, *args, **kwargs):
            entered.set()
            release.wait(1)
            return Judgement('no_request')
    cfg = parse(config_doc())
    pol = Blocked(cfg, Client(cfg, KEY))
    now = lambda: datetime.now(timezone.utc)
    job = pol.start_judgement({}, (), 'Done.', utcnow=now)
    assert entered.wait(1)
    cutoff = now() + timedelta(milliseconds=20)
    job.tighten(cutoff)
    job.wait()
    assert job.ready() and job.value is None and job.completed_at == cutoff
    assert job.client._cancelled.is_set()
    release.set()
    assert job.value is None


def test_r4_job_retains_an_endpoint_failure_before_cutoff():
    from datetime import datetime, timezone
    class Failed(Classifier):
        def judge(self, *args, **kwargs):
            raise EndpointError('request timeout with time left')
    cfg = parse(config_doc())
    job = Failed(cfg, Client(cfg, KEY)).start_judgement({}, (), 'Done.', utcnow=lambda: datetime.now(timezone.utc))
    job.wait()
    assert isinstance(job.value, EndpointError)
