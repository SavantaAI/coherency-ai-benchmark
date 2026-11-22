"""The unresolved D-042 property, without filtering timelines."""
import pytest

from cohbench.scoring import steering
from .timeline import generate, render


@pytest.mark.xfail(strict=True, reason="B1 rules disagree for seed 7: a same-content write changes the causal span")
def test_seeded_grain_property_200_timelines(tmp_path):
    failures = []
    for seed in range(200):
        timeline = generate(seed)
        results = [steering.score(render(timeline, merge).load(tmp_path / f"{seed}-{merge}")) for merge in (False, True)]
        if any(results[0][part] != results[1][part] for part in ("g", "c", "l", "score")):
            failures.append((seed, timeline))
    assert not failures, failures
