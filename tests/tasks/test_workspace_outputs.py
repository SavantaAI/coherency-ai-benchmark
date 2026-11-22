"""The workspace code prints what `workspace_outputs.json` records, under
the test interpreter. The same script, run by hand with a 3.11 interpreter,
is the check for the agent's container (README, "Python versions")."""

from __future__ import annotations

import json
import sys

from .check_python import RECORDED, outputs


def test_workspace_code_prints_the_recorded_outputs():
    got = outputs(sys.executable)
    want = json.loads(RECORDED.read_text())
    assert len(want) == 64
    assert got == want, sorted(k for k in want if got.get(k) != want[k])
