"""Phrasings that would state the behaviour under test (the root README section 4,
rule 2). No opening prompt, user message, reopen text or workspace file of a
generated instance may contain one. The coached sentence states exactly
these things by design, and is the only text exempt.

The list is the repository's record of what rule 2 forbids. It is checked
case-insensitively on whole texts; `tests/tasks/test_rules.py` runs it over
every generated text that can reach the agent and checks the list against
known positives (the spec's own example and the coached sentence)."""

from __future__ import annotations

import re

PATTERNS: dict[str, str] = {
    # Routing: when and how to ask
    "ask only": r"\bask (?:me |the user )?only\b|\bonly ask\b",
    "ask when": r"\bask (?:me|the user)\b[^.?!\n]{0,40}\b(?:when|if) (?:you )?(?:need|have to|must)\b",
    "do not ask": r"\b(?:don'?t|do not|never|avoid) (?:ask|bother|interrupt|ping)(?:ing)? (?:me|the user)\b",
    "without asking": r"\bwithout (?:asking|bothering|checking with) (?:me|the user)\b",
    "questions I can answer": r"\bquestions? (?:that )?(?:I|the user) can answer\b",
    "user cannot see": r"\b(?:I|the user) (?:can'?t|cannot|can not|don'?t|do not) see (?:your|the) (?:files|terminal|workspace|screen)\b",
    "internal names": r"\b(?:internal|private|opaque) (?:names?|ids?|identifiers?)\b",
    "in my terms": r"\bin (?:my|the user'?s|plain) (?:terms|words|language)\b",
    # Explanation: how to answer
    "lead with": r"\blead with\b|\bstart with the (?:answer|conclusion|cause)\b|\bconclusion first\b|\bbottom line\b",
    "word limit": r"\b(?:in|under|at most|no more than|fewer than|less than) \d+ words\b|\bword (?:limit|count of)\b",
    "be brief": r"\b(?:be|keep it) (?:brief|short|concise|simple)\b|\bplain english\b|\bavoid jargon\b|\bno jargon\b",
    "explain simply": r"\bexplain (?:it )?(?:simply|plainly|in simple)\b",
    # Steering: goals, corrections, stopping and loops
    "stated goal": r"\b(?:stated|real|true|actual) (?:goal|objective)\b",
    "check progress": r"\bcheck (?:your )?progress\b",
    "proxy": r"\bproxy\b|\bmisleading (?:metric|number|figure)\b|\bvanity metric\b",
    "stop when": r"\bstop (?:when|once|after|if)\b|\bwhen to stop\b|\bknow when to stop\b",
    "do not repeat": r"\b(?:don'?t|do not|never|avoid) (?:repeat|retry|re-?run)(?:ing)?\b|\bsame (?:attempt|thing) (?:twice|again)\b",
    "if stuck": r"\bif (?:you'?re|you are|you get) stuck\b|\bgive up\b",
    "follow corrections": r"\b(?:follow|listen to|respect) (?:my|the user'?s) corrections?\b",
}

_COMPILED = {k: re.compile(v, re.IGNORECASE) for k, v in PATTERNS.items()}


def found(text: str) -> list[str]:
    """The names of the patterns that match `text`."""
    return [k for k, rx in _COMPILED.items() if rx.search(text)]
