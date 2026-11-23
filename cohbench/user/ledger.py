"""Compatibility entry point for shared endpoint call accounting."""

from cohbench.ledger import Ledger, main, positive, line_bytes

from cohbench import ledger as _shared


def __getattr__(name):
    return getattr(_shared, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_shared)))


__all__ = [name for name in dir(_shared) if not name.startswith("_")]


if __name__ == "__main__":
    raise SystemExit(main())
