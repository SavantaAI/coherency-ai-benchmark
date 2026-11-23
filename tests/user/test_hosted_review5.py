"""Bounded transport cleanup and abandoned work, using only a local fake."""
import http.client
import json
import socket
import threading
import time

import pytest

from cohbench.user.api import Client, DeadlineExceeded
from cohbench.user.classifier import Classifier
from cohbench.user.config import parse
from cohbench.user.ledger import Ledger
from .conftest import KEY, config_doc


@pytest.mark.parametrize('phase', ['lookup', 'connect'])
def test_r5_abandoned_transport_has_bounded_flush_and_no_late_submission(tmp_path, fake, monkeypatch, phase):
    cfg=parse(config_doc(fake.base_url, limits={'request_timeout_s':.05,'verdict_deadline_s':.05,'retries':0}))
    client=Client(cfg, KEY, ledger=Ledger(tmp_path/'calls',1))
    release=threading.Event()
    target=socket if phase=='lookup' else http.client.HTTPConnection
    name='getaddrinfo' if phase=='lookup' else 'connect'
    original=getattr(target,name)
    def blocked(*args,**kwargs):
        release.wait(2)
        return original(*args,**kwargs)
    monkeypatch.setattr(target,name,blocked)
    try:
        started=time.monotonic()
        with pytest.raises(DeadlineExceeded):
            Classifier(cfg,client).judge({},(), 'Done.')
        assert time.monotonic()-started < .2
        before=time.monotonic()
        cleanup=client.flush(timeout_s=.02)
        assert time.monotonic()-before < .1
        assert cleanup['blocked_transport_threads']==1
        assert client._previous_served is None
        assert fake.requests==[]
    finally:
        release.set()
        client.flush()
    assert fake.requests==[]
    assert client._previous_served is None
    rows=[json.loads(x) for x in (tmp_path/'calls').read_text().splitlines()]
    assert sum(x['event']=='start' for x in rows)==1
    assert sum(x['event']=='finish' for x in rows)==1
    assert rows[-1]['outcome']=='deadline_exceeded'
