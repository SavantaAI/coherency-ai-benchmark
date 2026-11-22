"""The driver's collection reader does not write another container file."""

import subprocess
import sys

import pytest

from cohbench.driver.collection import read
from cohbench.trajectory.collection import LIMIT


class Container:
    def __init__(self):
        self.calls = []

    def exec(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        assert kwargs['user'] == 'node'
        return subprocess.run([sys.executable, *argv[1:]], capture_output=True, text=True)


@pytest.mark.parametrize('data,status', [(None, 'absent'), (b'a\nb\n', 'collected'), (b'x' * (LIMIT + 1), 'oversize')])
def test_reader_collects_as_agent_user_with_a_byte_cap(tmp_path, data, status):
    path = tmp_path / 'runs.jsonl'
    if data is not None:
        path.write_bytes(data)
    before = sorted(tmp_path.iterdir())
    container = Container()
    record, content = read(container, str(path))
    assert record == {'status': status, 'bytes': len(data or b'')}
    assert content == (data if status == 'collected' else None)
    assert sorted(tmp_path.iterdir()) == before
    assert len(container.calls) == 1


def test_reader_failure_is_not_absence(tmp_path):
    with pytest.raises(ValueError, match='collection read failed'):
        read(Container(), str(tmp_path))


def test_reader_rejects_workspace_path_before_opening():
    with pytest.raises(ValueError, match='collection read failed'):
        read(Container(), '/work/project/missing.jsonl')
