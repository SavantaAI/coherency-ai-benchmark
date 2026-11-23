"""The unit's own code revision: a short hash of the classifier and shared endpoint sources, the way
the runner records its sources. Every verdict record and every verdicts file
carries it, so a calibration result names exactly what produced it."""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path


@functools.cache
def code_revision() -> str:
    h = hashlib.sha256()
    root = Path(__file__).parent.parent
    sources = list((root / "user").glob("*.py"))
    sources += [root / name for name in ("endpoint.py", "errors.py", "ledger.py")]
    for f in sorted(sources):
        h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:12]
