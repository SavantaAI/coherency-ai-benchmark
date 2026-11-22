"""The simulated user's classifier and its calibration tools. See README.md.

Names are loaded on first use, so `import cohbench.user.kind` (the runner's
entry) loads nothing else from this package.
"""

from __future__ import annotations

import importlib
from typing import Any

_WHERE = {"Client": "api", "EndpointError": "api", "ServedMismatch": "api", "read_key": "api",
          "Classifier": "classifier", "ContextOverflow": "classifier", "factory": "classifier",
          "Config": "config", "ConfigError": "config", "load": "config"}

__all__ = sorted(_WHERE)


def __getattr__(name: str) -> Any:
    if name in _WHERE:
        return getattr(importlib.import_module(f".{_WHERE[name]}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
