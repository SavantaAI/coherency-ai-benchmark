"""Task generators for the three families (phase 4). See README.md."""

from __future__ import annotations

from pathlib import Path

from .common import FAMILIES, HORIZONS, GenerateError, Instance, Options, write_instance


def generate(o: Options) -> Instance:
    """One instance in memory, from its options alone."""
    if o.family not in FAMILIES:
        raise GenerateError(f"family: one of {list(FAMILIES)}")
    if o.horizon not in HORIZONS:
        raise GenerateError(f"horizon: one of {list(HORIZONS)}")
    if not isinstance(o.seed, int) or o.seed < 0:
        raise GenerateError("seed: a non-negative integer")
    if o.sibling is not None and o.family != "routing":
        raise GenerateError("only Routing has siblings")
    if o.family == "routing":
        from . import routing
        return routing.generate(o)
    if o.family == "explanation":
        from . import explanation
        return explanation.generate(o)
    from . import steering
    return steering.generate(o)


def generate_to(o: Options, out: Path) -> Instance:
    inst = generate(o)
    write_instance(inst, Path(out))
    return inst


__all__ = ["FAMILIES", "HORIZONS", "GenerateError", "Instance", "Options", "generate", "generate_to", "write_instance"]
