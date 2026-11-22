"""A scripted stand-in for the Anthropic Messages API (decision D-015).

The real harness talks to this server through the gateway and gets scripted
tool calls and replies. See README.md in this directory.
"""

from .engine import Episode
from .script import Script, ScriptError, load_script, load_script_file
from .server import App

__all__ = ["App", "Episode", "Script", "ScriptError", "load_script", "load_script_file"]
