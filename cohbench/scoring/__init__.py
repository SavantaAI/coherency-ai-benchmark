"""Scorers and the results report (SPEC.md sections 2, 5, 6 and 7; D-045).

Every per-episode scorer reads one scoring bundle and nothing else (D-018).
The report joins configurations to scored episodes through a results index
the scorers never see. See README.md.
"""

from .load import Episode, ScoreError, load
from .score import score_episode

__all__ = ["Episode", "ScoreError", "load", "score_episode"]
