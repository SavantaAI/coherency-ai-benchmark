"""Harness profiles. Only the run manifest names the profile that ran."""

from .base import Cleaned, NativeSession, Profile, ToolArgs
from .claude import ClaudeCodeProfile
from .pi import PiProfile

PROFILES: dict[str, type[Profile]] = {"pi": PiProfile, "claude-code": ClaudeCodeProfile}


def profile(name: str) -> Profile:
    try:
        return PROFILES[name]()
    except KeyError:
        raise ValueError(f"no profile {name!r}; known: {sorted(PROFILES)}") from None


__all__ = ["Cleaned", "ClaudeCodeProfile", "NativeSession", "PROFILES", "PiProfile", "Profile", "ToolArgs", "profile"]
