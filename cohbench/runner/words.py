"""Words that would tell the agent it is being measured.

Names and text are checked against the one shared list,
`cohbench.env.MEASUREMENT_WORDS` (D-019), through `telling_words` and
`telling_names`. Message text is also checked against a few phrases of the
runner's own (`MESSAGE_EXTRA`), which are not applied to file names: a task
may well have an `evaluate.py` or a `grader/` directory of its own.

What is checked, and where:

- workspace names and symlink targets: the shared list and the canary
  (`task.py`, at load). The metric directory never reaches the agent, so its
  names are not checked against the list (only against D-026's instruction
  file names);
- every text the runner can send (the opening prompt, the coached sentence,
  event and reopen messages, the reply templates, card texts that can become
  replies, the secret refusal, probe questions): the shared list, the extra
  phrases and the task's canary, at load and again in the guard before each
  send;
- workspace file contents: the canary and a shared-list word are both errors
  (D-031).

The post-run awareness question is not checked against the lists, since it
asks about exactly this; it is checked for the canary.
"""

from __future__ import annotations

import re

from cohbench.env import telling_names, telling_words

MESSAGE_EXTRA = re.compile(
    r"\bcoheren(?:ce|t)\b|\bgrader\b|\brubric\b|\bscoring\b|"
    r"\bbeing (?:tested|measured|watched|graded|evaluated|observed|scored)\b",
    re.IGNORECASE,
)


def message_words(text: str, canary: str | None = None) -> list[str]:
    """The telling words in a text that may be sent to the agent, lower-cased,
    with `canary` reported as `<canary>`."""
    out = telling_words(text) + [m.group(0).lower() for m in MESSAGE_EXTRA.finditer(text)]
    if canary and canary in text:
        out.append("<canary>")
    return out


def name_words(names: list[str] | tuple[str, ...]) -> list[str]:
    """The names (paths) holding a word from the shared list."""
    return telling_names(list(names))


__all__ = ["MESSAGE_EXTRA", "message_words", "name_words", "telling_words"]
