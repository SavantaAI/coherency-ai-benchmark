"""Classifier compatibility imports for the shared closed-choice client."""

from cohbench.endpoint import (
    Client,
    DeadlineExceeded,
    EndpointError,
    PolicyError,
    ServedMismatch,
    KEY_ENV,
    KEY_MARK,
    WITHHELD,
    read_key,
    retryable_status,
    status_category,
    _Attempt,
    _Transient,
)

from cohbench import endpoint as _shared
from .config import Config
from . import prompts as P


def __getattr__(name):
    return getattr(_shared, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_shared)))


__all__ = sorted({name for name in dir(_shared) if not name.startswith("_")} | {"Config", "P"})
