"""Typed failure causes in serial classification, without hosted requests."""

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
        pol.judge({}, (), "Done.", deadline=pol.clock()+1)


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
