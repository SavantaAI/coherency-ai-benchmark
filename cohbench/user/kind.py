"""The `classifier` policy kind for the runner: `--policy classifier:CONFIG`.

The runner registers the kind with one call, which imports nothing heavy:

    from cohbench.user.kind import register; register()

`register()` hands `make_factory` to `cohbench.runner.policy.register_policy_kind`.
`make_factory(path)` imports the classifier, then only loads and checks the
config, so the runner may call it to check the file before anything starts.
The API key is read when the first policy is made, from the file named by
`COHBENCH_CLASSIFIER_KEY_FILE`, and one client is shared by every policy made
for the same config in the process. Calling `register()` again is harmless.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

KIND = "classifier"

_lock = threading.Lock()
_clients: dict[str, Any] = {}


def make_factory(path: str | Path) -> Any:
    from .api import Client, read_key
    from .classifier import Classifier
    from .config import load

    cfg = load(path)

    def make() -> Classifier:
        with _lock:
            if cfg.digest not in _clients:
                _clients[cfg.digest] = Client(cfg, read_key())
            client = _clients[cfg.digest]
        return Classifier(cfg, client)

    return make


def register() -> None:
    from cohbench.runner.policy import register_policy_kind

    register_policy_kind(KIND, make_factory)
