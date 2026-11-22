"""The unit's own code revision: a short hash of `cohbench/user/*.py`, the way
the runner records its sources. Every verdict record and every verdicts file
carries it, so a calibration result names exactly what produced it."""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path


@functools.cache
def code_revision() -> str:
    h = hashlib.sha256()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:12]
