"""The environment passes the metric command through a private startup file."""

import json
from types import SimpleNamespace

from cohbench.env import environment as env


def test_watcher_start_has_no_metric_command_in_argv_or_environment(tmp_path, monkeypatch):
    marker = 'private-metric-seed-4267'
    command = ('python3', '/opt/task/metric.py', marker)
    out = tmp_path / 'out'
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert marker not in json.dumps([args, kwargs])
        if args[0] == 'run':
            assert '--metric-cmd' not in args and '--metric-cmd-file' in args
            p = out / '.metric-command.json'
            assert json.loads(p.read_text()) == list(command)
            p.unlink()
            (out / 'heartbeat.json').write_text(json.dumps({'status': 'running', 'metric_isolation': 'users'}))
            (out / 'events.jsonl').write_text('{"kind": "baseline"}\n')
        return SimpleNamespace(returncode=0, stdout='true')

    monkeypatch.setattr(env, 'run', run)
    monkeypatch.setattr(env, 'bounded_subprocess', lambda *a, **k: SimpleNamespace(returncode=0))
    registry = SimpleNamespace(add=lambda *a: 'watcher-one', remove=lambda *a: None)
    spec = env.WatcherSpec(metric_cmd=command, ignore_names=(), ignore_git_refs=())
    assert env.start_watcher('workspace', out, spec, 'watcher-image', registry) == 'watcher-one'
    assert not (out / '.metric-command.json').exists()
