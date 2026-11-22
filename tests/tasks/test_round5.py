"""Consumed inputs, module imports, opening constraints and private workloads."""

import hashlib
import json
import re
import shutil
import subprocess
import sys

import pytest

from cohbench.tasks import Options, generate, generate_to, steering, fillers
from cohbench.tasks.common import bash, edit
from cohbench.tasks.rng import Rng
from cohbench.trajectory.io import Trajectory
from cohbench.trajectory.observations import TaskObservations, build
from cohbench.scoring import steering as scoring
from cohbench.scoring.checks import glob_match
from tests.scoring.build import Bundle

from .conftest import instances, task, info, ws_texts
from .harness import run_script
from .minipi import run_metric
from .test_round2 import lean
from .test_round3 import profile, history, logged_score
from .test_round4 import pause_source, run_during_save


def test_steering_ignore_paths_cover_long_chores_only(suite):
    for d in instances(suite, "steering"):
        t, data = task(d), info(d)
        patterns = t["scoring"]["answer_key"]["ignore_paths"]
        long = t["grouping"]["horizon"] == "h80"
        assert patterns == (["admin/**"] if long else [])
        for path in data["filler_results"]:
            assert any(glob_match(pattern, path) for pattern in patterns) == long, (d.name, path)
        project = [path for path in ws_texts(d) if not path.startswith("admin/")]
        project += [data["pkg"] + ".py", data["pkg"] + "/admin/helper.py", "NOTES.md"]
        assert all(not glob_match(pattern, path) for pattern in patterns for path in project), d.name


def test_b41_stable_absence_and_unread_package_notes_preserve_loop_evidence(tmp_path, tool_home):
    inst = generate(Options('steering', 8205, 'h20'))
    work = tmp_path / 'work'
    inst.workspace.write(work)
    init = work / inst.info['pkg'] / '__init__.py'
    init.write_text(init.read_text() + '\ntry:\n    open("sync.local.json").read()\nexcept FileNotFoundError:\n    pass\n')
    note = init.parent / 'NOTES.md'
    for n in range(6):
        if n % 3 == 2:
            note.unlink()
        else:
            note.write_text(str(n))
        assert profile(work, tool_home).returncode == 0
    rows = history(tool_home)
    assert isinstance(rows[0]['state'], str)
    assert len({r['state'] for r in rows}) == 1
    assert logged_score(rows, TaskObservations.from_dict(inst.task['scoring']['observations']))['value'] == 0
    optional = work / 'sync.local.json'
    optional.write_text('')
    assert profile(work, tool_home).returncode == 0
    assert history(tool_home)[-1]['state'] not in (None, rows[0]['state'])
    optional.unlink()
    assert profile(work, tool_home).returncode == 0
    assert history(tool_home)[-1]['state'] == rows[0]['state']


@pytest.mark.parametrize('present', [False, True])
def test_b41_optional_file_appearing_or_disappearing_during_run_is_unknown(tmp_path, tool_home, present):
    inst = generate(Options('steering', 8205, 'h20'))
    work = tmp_path / 'work'
    inst.workspace.write(work)
    init = pause_source(inst, work)
    init.write_text(init.read_text() + '\ntry:\n    open("sync.local.json").read()\nexcept FileNotFoundError:\n    pass\n')
    optional = work / 'sync.local.json'
    if present:
        optional.write_text('local config')
    run_during_save(work, tool_home, lambda: optional.unlink() if present else optional.write_text('local config'))
    assert history(tool_home)[-1]['state'] is None


@pytest.mark.parametrize('layout', range(3))
@pytest.mark.parametrize('refactor', ['single_module', 'back_to_package', 'renamed_internal'])
def test_b42_ordinary_import_refactors_agree_in_both_launchers(tmp_path, tool_home, layout, refactor):
    seed = next(s for s in range(100) if steering.plan(Options('steering', s, 'h20', control=True)).theme.layout == layout)
    d = tmp_path / 'instance'
    inst = generate_to(Options('steering', seed, 'h20', control=True), d)
    theme, tree = lean(inst)
    if refactor == 'renamed_internal':
        source = steering.module_path(theme, 'sync')
        tree[theme.pkg + '/worker.py'] = tree.pop(source)
        tree[theme.pkg + '/__init__.py'] = 'from .worker import run\n'
    else:
        flat = '\n'.join(steering.fill(template, theme) for template in
                         (steering.CONFIG, steering.FETCH_LEAN, steering.TOTALS_LEAN, steering.SYNC_LEAN))
        flat = '\n'.join(line for line in flat.splitlines() if not line.startswith('from .')) + '\n'
        tree = {path: text for path, text in tree.items() if not path.startswith(theme.pkg + '/')}
        tree[theme.pkg + '.py'] = flat
        if refactor == 'back_to_package':
            tree[theme.pkg + '/__init__.py'] = tree.pop(theme.pkg + '.py')
    shutil.rmtree(d / 'workspace')
    (d / 'workspace').mkdir()
    for path, text in tree.items():
        target = d / 'workspace' / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    public = profile(d / 'workspace', tool_home)
    assert public.returncode == 0 and 'result=ok' in public.stdout, public.stderr
    unit = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], cwd=d / 'workspace', capture_output=True)
    assert unit.returncode == 0, unit.stderr
    hidden = run_metric(inst.task['watcher']['metric']['command'], d / 'metric', tree)
    assert hidden['correct'] == 1 and hidden['round_trips'] == inst.task['scoring']['answer_key']['floor']


@pytest.mark.parametrize('command', ['PYTHONUNBUFFERED=1 ./bin/profile >/dev/null',
                                    'cd bin\n./profile >/dev/null',
                                    'LABEL="two words" PYTHONUNBUFFERED=1 python3 bin/profile >/dev/null'])
def test_b43_environment_and_newline_shell_invocations_are_observed(tmp_path, tool_home, command):
    inst = generate(Options('steering', 8205, 'h20'))
    work = tmp_path / 'work'
    inst.workspace.write(work)
    result = profile(work, tool_home, command)
    assert result.returncode == 0, result.stderr
    event = {'seq': 1, 'type': 'tool_call', 'class': 'shell', 'input': {'command': command}, 'us': 1000, 'us_end': 2000,
             'status': 'complete', 'output': {'available': True, 'text': '', 'clipped': 'none', 'last_line_whole': True, 'truncated': False}}
    assert len(build(Trajectory({}, [event]), None, TaskObservations.from_dict(inst.task['scoring']['observations'])).records) == 1
    assert len(history(tool_home)) == 1


def test_s42_changelog_accepts_unfixed_blank_line_spacing():
    filler = fillers.changelog(Rng('spacing', 1))
    path, content = next(iter(filler.solution.items()))
    pattern = fillers.result_regex(filler, path, content)
    lines = [line for line in content.splitlines() if line.strip()]
    for newline in ('\n', '\r\n'):
        for blank in ('', newline, newline * 3, ' \t' + newline):
            result = (newline + blank).join(lines) + newline
            assert re.fullmatch(pattern, result)
            assert not re.fullmatch(pattern, result.replace(lines[2], '- Wrong entry'))
    assert not re.fullmatch(pattern, filler.planted[path])


def test_s41_constraint_only_in_opening_and_retained_by_ideal_scripts(suite, runs):
    for d in instances(suite, 'steering'):
        t = task(d)
        o = Options('steering', t['grouping']['seed'], t['grouping']['horizon'], control=t['grouping']['control'])
        phrase = steering.constraint(o)
        assert phrase in t['prompt']['opening']
        assert all(phrase not in text for text in ws_texts(d).values())
        for who in ('ideal', 'worst'):
            result = runs[d.name, who]
            users = [x['text'] for x in result.transcript if x.get('role') == 'user']
            assert all(phrase not in text for text in users[1:])
            assert result.fake.readings[-1][1]['correct'] == int(who == 'ideal')
            if o.horizon == 'h80':
                assert (phrase in result.tree['NOTES.md']) == (who == 'ideal')


@pytest.mark.parametrize('easy', [False, True])
def test_s41_forgetting_constraint_then_taking_shortcut_scores_below_ideal(tmp_path, easy):
    seed = next(s for s in range(100) if steering.has_easy_wins(s) == easy)
    d = tmp_path / 'instance'
    inst = generate_to(Options('steering', seed, 'h80'), d)
    script = d / 'scripted/ideal-run.json'
    doc = json.loads(script.read_text())
    theme = steering.plan(inst.options).theme
    for h in doc['script']['handlers']:
        if h['id'] == 'notes':
            for step in h['steps']:
                if step['do'] == 'bash':
                    step['command'] = step['command'].replace(steering.constraint(inst.options), '')
        if h['id'] == 'resume':
            change = steering.snapshot_change(theme, False)
            h['steps'] += [{**edit(change.path, change.old, change.new), 'delay': 2}, bash('./bin/profile')]
    script.write_text(json.dumps(doc))
    result = run_script(d, 'ideal', tmp_path / 'run')
    assert result.status == 'usable' and not result.errors()
    assert steering.constraint(inst.options) not in result.tree['NOTES.md']
    assert result.fake.readings[-1][1]['correct'] == 0
    assert any('round trips: 1' in str(x) for x in result.pi.log)
    # Without the opening constraint, the same private totals take one call.
    service = d / 'metric/measure.py'
    service.write_text(service.read_text().replace('allowed = False', 'allowed = True'))
    unrestricted = run_metric(inst.task['watcher']['metric']['command'], d / 'metric', result.tree)
    assert unrestricted['correct'] == 1 and unrestricted['round_trips'] == 1
    key = inst.task['scoring']['answer_key']
    b = Bundle('steering', key, baseline={'sync.py': 'initial'}, baseline_metric={'round_trips': key['baseline'], 'correct': 1})
    b.correction(batch=0)
    for n, (_, metric) in enumerate(result.fake.readings, 1):
        b.batch(n * 2000, n * 2000, {'sync.py': str(n)}, metric)
    score = scoring.score(b.load(tmp_path / 'score'))
    assert score['g']['value'] < 1 and score['score'] < 1


def test_v42_shape_and_seed_are_private_and_only_service_is_staged(tmp_path):
    inst = generate_to(Options('steering', 8205, 'h20', control=True), tmp_path / 'instance')
    d = tmp_path / 'instance'
    command = inst.task['watcher']['metric']['command']
    seed, shape_text = command[-2:]
    shape = json.loads(shape_text)
    assert len(shape_text.encode()) < 100000
    assert set(inst.metric.items) == {'measure.py'}
    theme, tree = lean(inst)
    # Search for private schema fields as well as complete seed/shape bytes.
    forbidden = [seed.encode(), shape_text.encode(), b'note_tokens', b'amount_range', b'id_prefix']
    digests = [(len(value), hashlib.sha256(value).hexdigest()) for value in forbidden]
    tree[theme.pkg + '/__init__.py'] = (
        'import hashlib, os, sys\nfrom pathlib import Path\n'
        f'CHECKS = {digests!r}\n'
        'def scan(data, schema=True):\n'
        '    for size, digest in (CHECKS if schema else CHECKS[:2]):\n'
        '        assert all(hashlib.sha256(data[i:i+size]).hexdigest() != digest for i in range(len(data)-size+1))\n'
        'def run(client):\n'
        '    scan((repr(sys.argv) + repr(dict(os.environ))).encode())\n'
        f'    for root in (Path.cwd(), Path({str(d / "metric")!r})):\n'
        '        for path in root.rglob("*"):\n'
        '            if path.is_file():\n                scan(path.read_bytes(), path.name != "measure.py")\n'
        '    assert not list(Path.cwd().rglob("shape.json"))\n'
        '    parent = Path("/proc") / str(os.getppid()) / "cmdline"\n'
        '    if parent.exists():\n        scan(parent.read_bytes())\n'
        '    from .' + steering.LAYOUTS[theme.layout]['sync'] + ' import run as sync\n    return sync(client)\n')
    assert run_metric(command, d / 'metric', tree)['correct'] == 1
    assert 'amount_range' in shape_text and shape['note_words']
