"""The script format: parsing, validation, templates and conditions.

A script is JSON. `load_script` checks every field and collects every problem
it finds, each with the JSON path of the offending value, then raises
`ScriptError` listing them all. A script that loads has no unknown keys, no
bad types, no NaN or infinite numbers, regular expressions that compile and
have no nested unbounded quantifier, children that exist, and no template
that names a variable nothing defines. Validation is total: any input,
however malformed, gives a `ScriptError`, never another exception.

The README has the format with a worked example; `schema.json` beside this
file is the same format as a JSON Schema for editors. This module is the
authority when the two disagree.
"""

from __future__ import annotations

import copy
import decimal
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

import unicodedata

from .wire import ES_WHITESPACE as ES_SPACE, es_blank, es_strip

FORMAT_VERSION = 1
DEFAULT_FALLBACK = "Sorry, I can't continue with this right now."
MAX_DELAY_SECONDS = 3600.0
MAX_REPEAT = 1000
# Regular expressions run on at most this much text (tool output, user text).
REGEX_TEXT_LIMIT = 1024 * 1024

VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BUILTIN_VARS = frozenset({"last_output", "last_exit", "user_message"})

DELEGATE_TOOL = "mcp__t3-code__delegate_task"
TOOL_STEP_NAMES = {"bash": "bash", "read": "read", "write": "write", "edit": "edit"}
TOOL_STEPS = frozenset({"bash", "read", "write", "edit", "tool", "delegate"})
OTHER_STEPS = frozenset({"parallel", "branch", "repeat", "say", "reply", "set"})
STEP_TYPES = TOOL_STEPS | OTHER_STEPS
ENDS = ("statement", "question")
ON_STEER = ("finish", "abandon")
CONDITION_KEYS = ("exit", "is_error", "contains", "regex", "var_set", "var", "not", "all", "any")
EXPECT_KEYS = ("contains", "regex", "var_set", "var", "not", "all", "any")
VAR_TEXT_OPS = ("equals", "contains", "regex")
VAR_NUM_OPS = ("lt", "le", "gt", "ge", "eq", "ne")

_COMMON_TOOL_KEYS = {"do", "say", "delay", "capture", "id"}
_STEP_KEYS = {
    "bash": {"command", "timeout"},
    "read": {"path", "offset", "limit"},
    "write": {"path", "content"},
    "edit": {"path", "edits"},
    "tool": {"name", "input"},
    "delegate": {"child", "task", "input", "tool"},
}
_TOOL_REQUIRED = {
    "bash": {"command"}, "read": {"path"}, "write": {"path", "content"},
    "edit": {"path", "edits"}, "tool": {"name"}, "delegate": {"child", "task"},
}


class ScriptError(ValueError):
    """Every problem found in a script, each prefixed with its JSON path."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("invalid script:\n" + "\n".join("  " + e for e in self.errors))


class RenderError(Exception):
    """A template named a variable that has no value at run time."""

    def __init__(self, name: str, where: str):
        self.name = name
        self.where = where
        super().__init__(f"variable {name!r} has no value ({where})")


class ConditionError(Exception):
    """A numeric comparison met a value that is not a number."""


@dataclass
class Handler:
    path: str
    label: str | None
    steps: list[str]          # step paths
    final: str | None
    final_delay: float
    end: str
    on_steer: str
    expect: dict | None
    capture: list[dict]       # captures from the user message


@dataclass
class Script:
    raw: dict
    sha256: str
    description: str | None
    handlers: list[Handler]
    children: dict[str, list[Handler]]
    fallback: str
    vars: dict[str, str]
    steps: dict[str, dict] = field(default_factory=dict)
    defined: frozenset = frozenset()   # every variable the script can set
    source: str = ""                   # the JSON text, numbers as written
    delegate_names: frozenset = frozenset()   # the delegation tool and every alias it was given
    on_child_completion: dict = field(default_factory=dict)   # {"text", "capture"} for T3's completion prompt

    def summary(self) -> dict:
        return {
            "sha256": self.sha256,
            "description": self.description,
            "handlers": len(self.handlers),
            "children": {k: len(v) for k, v in self.children.items()},
        }


# ---------------------------------------------------------------- templates


def template_vars(text: str) -> list[str]:
    return VAR_RE.findall(text)


def render(text: str, values: dict[str, str | None], where: str) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if values.get(name) is None:
            raise RenderError(name, where)
        return str(values[name])

    return VAR_RE.sub(sub, text)


def render_value(value: Any, values: dict[str, str | None], where: str) -> Any:
    """Render every string inside a JSON value (dict keys are left alone)."""
    if isinstance(value, str):
        return render(value, values, where)
    if isinstance(value, list):
        return [render_value(v, values, where) for v in value]
    if isinstance(value, dict):
        return {k: render_value(v, values, where) for k, v in value.items()}
    return value


def search(pattern: str, text: str) -> re.Match | None:
    return re.search(pattern, text[:REGEX_TEXT_LIMIT], re.MULTILINE)


def capture_value(cap: dict, text: str) -> str | None:
    """The captured text, or None when the pattern or its group did not match."""
    m = search(cap["regex"], text)
    if m is None:
        return None
    group = cap.get("group", 1 if m.re.groups else 0)
    return m.group(group)


# --------------------------------------------------------------- conditions


NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def _number(text: str | None, what: str) -> decimal.Decimal:
    """A variable's text as an exact decimal: integers compare as integers,
    decimals exactly (no float rounding). Text the decimal library cannot
    hold (an exponent beyond its range) is a ConditionError."""
    t = str(text).strip() if text is not None else ""
    if not NUMBER_RE.fullmatch(t):
        raise ConditionError(f"{what} is {text!r}, not a number")
    try:
        return decimal.Decimal(t)
    except decimal.DecimalException:
        raise ConditionError(f"{what} is {text[:60]!r}, a number out of range") from None


def _operand(v: Any) -> decimal.Decimal:
    """A literal operand. The validator stores literals as exact decimals
    read from the script's text; a float can only arrive from a script given
    as a Python object, and compares by its shortest decimal form."""
    if isinstance(v, decimal.Decimal):
        return v
    return decimal.Decimal(v) if isinstance(v, int) else decimal.Decimal(repr(v))


# What T3 Code (the pinned release's source) does with a delegate_task call
# before the child sees it. The input schema (packages/contracts/src/
# orchestratorMcp.ts, OrchestratorMcpDelegateTaskInput) decodes `task` as a
# TrimmedNonEmptyString (baseSchemas.ts: JavaScript trim(), non-blank) of at
# most 120,000 characters, and `role` as one of these literals. taskPrompt
# (apps/server/src/mcp/delegatedTaskModes.ts) adds the role line; the child's
# first message is then withMessageSenderNote (packages/contracts/src/
# orchestrationV2.ts) of that prompt.
T3_ROLES = ("implementation", "research", "review", "design", "test", "general")
T3_TASK_MAX = 120_000   # JavaScript string length: UTF-16 code units


def t3_task(task: str) -> str:
    """The task as T3 decodes it: trimmed with JavaScript's trim()."""
    return es_strip(task)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def launch_prompt(task: str, role: Any) -> str:
    """T3's `taskPrompt` of the decoded task: the trimmed task alone for no
    role or `general`, otherwise a role line, a blank line and the task. The
    validator and the engine both use this one function."""
    task = t3_task(task)
    if role is None or role == "general":
        return task
    return f"Act as the {role} sub-agent for this task.\n\n{task}"


def t3_leading_command(text: str) -> bool:
    """T3's LEADING_COMMAND, /^\\s*(?:\\/|\\p{Sc}[a-zA-Z0-9:_-]*[a-zA-Z])/u: a
    prompt that starts with a slash command or a skill mention such as
    `$deploy` gets no sender note, so the provider can parse it."""
    rest = text.lstrip(ES_SPACE)
    if rest.startswith("/"):
        return True
    if not rest or unicodedata.category(rest[0]) != "Sc":
        return False
    run = re.match(r"[a-zA-Z0-9:_-]*", rest[1:]).group(0)
    return any(c.isascii() and c.isalpha() for c in run)


def evaluate(cond: dict, result: dict | None, values: dict[str, str | None], where: str) -> bool:
    """Evaluate a validated condition.

    `result` is the previous tool result {"text", "is_error", "exit"}, or None
    when no tool has returned yet. A condition on a result that does not exist
    is false (and `not` of it true). Raises RenderError for a template with no
    value and ConditionError for a numeric comparison on a non-number."""
    key, arg = next(iter(cond.items()))
    if key == "not":
        return not evaluate(arg, result, values, where)
    if key == "all":
        return all(evaluate(c, result, values, where) for c in arg)
    if key == "any":
        return any(evaluate(c, result, values, where) for c in arg)
    if key == "var_set":
        return values.get(arg) is not None
    if key == "var":
        name = arg["name"]
        value = values.get(name)
        op = next(k for k in arg if k != "name")
        operand = arg[op]
        if op in VAR_TEXT_OPS:
            if value is None:
                return False
            if op == "equals":
                return value == render(operand, values, where)
            if op == "contains":
                return render(operand, values, where) in value
            return search(operand, value) is not None
        if value is None:
            raise ConditionError(f"{where}: variable {name!r} has no value to compare")
        left = _number(value, f"{where}: variable {name!r}")
        if isinstance(operand, dict):
            other = operand["var"]
            if values.get(other) is None:
                raise ConditionError(f"{where}: variable {other!r} has no value to compare")
            right = _number(values[other], f"{where}: variable {other!r}")
        else:
            right = _operand(operand)
        try:
            return {"lt": left < right, "le": left <= right, "gt": left > right,
                    "ge": left >= right, "eq": left == right, "ne": left != right}[op]
        except decimal.DecimalException as e:
            raise ConditionError(f"{where}: the numbers cannot be compared: {type(e).__name__}") from None
    if result is None:
        return False
    if key == "exit":
        return result.get("exit") == arg
    if key == "is_error":
        return bool(result.get("is_error")) == arg
    if key == "contains":
        return render(arg, values, where) in result.get("text", "")
    if key == "regex":
        return search(arg, result.get("text", "")) is not None
    raise AssertionError(f"unvalidated condition {key!r}")


def evaluate_expect(cond: dict, text: str, values: dict[str, str | None], where: str) -> bool:
    """A handler's `expect`, with `contains` and `regex` applied to the user message."""
    return evaluate(cond, {"text": text, "is_error": False, "exit": None}, values, where)


# ------------------------------------------------------------ regex safety


def regex_problem(pattern: str) -> str | None:
    """Why a pattern is refused, or None. Refuses what does not compile, and
    a repeat that can match more than once around another repeat that can,
    when either is unbounded (`(a+)+`, `(\\w*\\s?)*`): the shape that makes
    Python's backtracking engine take exponential time on a near miss.
    Possessive repeats and atomic groups are allowed around anything.
    Overlapping alternatives inside a repeat (`(a|ab)*`) are not detected."""
    try:
        re.compile(pattern, re.MULTILINE)
    except (re.error, OverflowError, RecursionError) as e:
        return f"regular expression does not compile: {e}"
    try:
        from re import _constants as C, _parser as P
        tree = P.parse(pattern)
    except Exception as e:  # noqa: BLE001  the analysis must not let a pattern through unseen
        return f"regular expression could not be analysed: {e}"
    repeats = (C.MAX_REPEAT, C.MIN_REPEAT)

    def subpatterns(av: Any):
        if isinstance(av, P.SubPattern):
            yield av
        elif isinstance(av, (list, tuple)):
            for x in av:
                yield from subpatterns(x)

    def walk(sp, outer_unbounded: bool | None) -> str | None:
        for op, av in sp:
            if op in repeats:
                lo, hi, inner = av
                many = hi > 1
                unbounded = hi == C.MAXREPEAT
                if many and outer_unbounded is not None and (unbounded or outer_unbounded):
                    return "nested repeats with an unbounded count can take exponential time"
                found = walk(inner, (unbounded or bool(outer_unbounded)) if many else outer_unbounded)
            elif op == getattr(C, "POSSESSIVE_REPEAT", None):
                # No backtracking into it, so it is no repeating context; its
                # inside is still checked on its own.
                found = walk(av[2], None)
            elif op == getattr(C, "ATOMIC_GROUP", None):
                found = walk(av, None)
            else:
                found = None
                for child in subpatterns(av):
                    found = walk(child, outer_unbounded)
                    if found:
                        break
            if found:
                return found
        return None

    try:
        return walk(tree, None)
    except RecursionError:
        return "regular expression is nested too deeply"


# --------------------------------------------------------------- validation


def _is_number(v: Any) -> bool:
    """A JSON number. Integers are never converted to float, so 10**400 is a
    number that range checks then refuse, not an OverflowError."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    return isinstance(v, float) and math.isfinite(v)


class _V:
    def __init__(self, exact: Any = None, root: str = "$") -> None:
        self.exact = exact   # the script parsed with exact decimals, when it came as text
        self.root = root
        self.in_child = False   # validating a child's handlers: they may not delegate
        self.tool_names: list[tuple[str, str]] = []   # (name, path) of every `tool` step
        self.delegate_names: set[str] = {DELEGATE_TOOL}  # the delegation tool and its aliases
        self.errors: list[str] = []
        self.steps: dict[str, dict] = {}
        self.defined: set[str] = set(BUILTIN_VARS)
        self.used: list[tuple[str, str]] = []
        self.delegated: list[tuple[str, str]] = []

    def err(self, path: str, msg: str) -> None:
        self.errors.append(f"{path}: {msg}")

    def obj(self, v: Any, path: str, required: set[str], optional: set[str]) -> dict | None:
        if not isinstance(v, dict):
            self.err(path, f"expected an object, got {_tname(v)}")
            return None
        for k in sorted(required - v.keys()):
            self.err(path, f"missing required key {k!r}")
        for k in sorted(v.keys() - required - optional):
            self.err(f"{path}.{k}", "unknown key")
        return v

    def string(self, v: Any, path: str, *, nonempty: bool = False, template: bool = True) -> str | None:
        if not isinstance(v, str):
            self.err(path, f"expected a string, got {_tname(v)}")
            return None
        if nonempty and es_blank(v):
            self.err(path, "must not be empty")
        if template:
            for name in template_vars(v):
                self.used.append((name, path))
        return v

    def number(self, v: Any, path: str, *, nullable: bool = False, lo: float | None = None,
               hi: float | None = None, integer: bool = False) -> float | None:
        if v is None and nullable:
            return None
        if not _is_number(v) or (integer and not isinstance(v, int)):
            kind = "an integer" if integer else "a finite number"
            self.err(path, f"expected {kind}{' or null' if nullable else ''}, got {_tname(v)}")
            return None
        if lo is not None and v < lo:
            self.err(path, f"must be at least {lo}")
            return None
        if hi is not None and v > hi:
            self.err(path, f"must be at most {hi}")
            return None
        return v

    def exact_number(self, path: str, value: Any) -> decimal.Decimal | None:
        """The literal at `path` as written in the script's text, or None
        when the decimal library cannot hold it (a literal such as
        1e-99999999999999999999, which a float would quietly turn into 0)."""
        if self.exact is not None:
            node = self.exact
            for key, index in re.findall(r"\.([^.\[]+)|\[(\d+)\]", path[len(self.root):]):
                node = node[int(index)] if index else node[key]
            if node is None:
                return None
            if isinstance(node, (int, decimal.Decimal)) and not isinstance(node, bool):
                return decimal.Decimal(node)
        return _operand(value)

    def name(self, v: Any, path: str, *, defining: bool) -> str | None:
        if not isinstance(v, str) or not NAME_RE.match(v):
            self.err(path, "expected a variable name")
            return None
        if defining:
            if v in BUILTIN_VARS:
                self.err(path, f"{v!r} is a built-in variable")
                return None
            self.defined.add(v)
        else:
            self.used.append((v, path))
        return v

    def regex(self, v: Any, path: str) -> re.Pattern | None:
        if not isinstance(v, str):
            self.err(path, f"expected a regular expression string, got {_tname(v)}")
            return None
        problem = regex_problem(v)
        if problem:
            self.err(path, problem)
            return None
        return re.compile(v, re.MULTILINE)

    def template_value(self, v: Any, path: str) -> None:
        if isinstance(v, str):
            for name in template_vars(v):
                self.used.append((name, path))
        elif isinstance(v, list):
            for i, x in enumerate(v):
                self.template_value(x, f"{path}[{i}]")
        elif isinstance(v, dict):
            for k, x in v.items():
                self.template_value(x, f"{path}.{k}")
        elif isinstance(v, float) and not math.isfinite(v):
            self.err(path, "NaN and infinite numbers are not allowed")
        elif not (v is None or isinstance(v, (bool, int, float))):
            self.err(path, f"not a JSON value: {type(v).__name__}")

    # conditions -------------------------------------------------------

    def condition(self, v: Any, path: str, keys: tuple[str, ...] = CONDITION_KEYS) -> dict | None:
        if not isinstance(v, dict) or len(v) != 1:
            self.err(path, f"a condition is an object with exactly one of {', '.join(keys)}")
            return None
        key, arg = next(iter(v.items()))
        if key not in keys:
            self.err(f"{path}.{key}", f"unknown condition; use one of {', '.join(keys)}")
            return None
        p = f"{path}.{key}"
        if key == "exit":
            self.number(arg, p, integer=True)
        elif key == "is_error":
            if not isinstance(arg, bool):
                self.err(p, f"expected true or false, got {_tname(arg)}")
        elif key == "contains":
            self.string(arg, p)
        elif key == "regex":
            self.regex(arg, p)
        elif key == "var_set":
            self.name(arg, p, defining=False)
        elif key == "var":
            ops = VAR_TEXT_OPS + VAR_NUM_OPS
            if self.obj(arg, p, {"name"}, set(ops)) is None:
                return v
            self.name(arg.get("name"), f"{p}.name", defining=False)
            given = [k for k in arg if k in ops]
            if len(given) != 1:
                self.err(p, f"needs exactly one of {', '.join(ops)}")
                return v
            op = given[0]
            operand = arg[op]
            if op in ("equals", "contains"):
                self.string(operand, f"{p}.{op}")
            elif op == "regex":
                self.regex(operand, f"{p}.{op}")
            elif isinstance(operand, dict):
                if self.obj(operand, f"{p}.{op}", {"var"}, set()) is not None:
                    self.name(operand.get("var"), f"{p}.{op}.var", defining=False)
            elif (self.exact is not None and isinstance(operand, (int, float))
                  and not isinstance(operand, bool)):
                # From the script's text: judge the literal by its exact parse,
                # not by the float the plain parse made of it (1e400 is a
                # decimal the comparison can use, though no float holds it).
                exact = self.exact_number(f"{p}.{op}", operand)
                if exact is None or not exact.is_finite():
                    self.err(f"{p}.{op}", "the number is outside the range a decimal can hold")
                else:
                    arg[op] = exact
            elif self.number(operand, f"{p}.{op}") is not None:
                arg[op] = self.exact_number(f"{p}.{op}", operand)
        elif key == "not":
            self.condition(arg, p, keys)
        elif key in ("all", "any"):
            if not isinstance(arg, list) or not arg:
                self.err(p, "expected a non-empty list of conditions")
            else:
                for i, c in enumerate(arg):
                    self.condition(c, f"{p}[{i}]", keys)
        return v

    def captures(self, caps: Any, path: str) -> list[dict]:
        if not isinstance(caps, list):
            self.err(path, "expected a list of captures")
            return []
        out = []
        for i, c in enumerate(caps):
            cp = f"{path}[{i}]"
            if self.obj(c, cp, {"var", "regex"}, {"group", "required"}) is None:
                continue
            self.name(c.get("var"), f"{cp}.var", defining=True)
            rx = self.regex(c.get("regex"), f"{cp}.regex")
            if "group" in c:
                g = c["group"]
                if isinstance(g, bool) or not isinstance(g, (int, str)):
                    self.err(f"{cp}.group", "expected a group number or name")
                elif rx is not None:
                    if isinstance(g, int) and not 0 <= g <= rx.groups:
                        self.err(f"{cp}.group", f"the pattern has {rx.groups} group(s)")
                    if isinstance(g, str) and g not in rx.groupindex:
                        self.err(f"{cp}.group", f"the pattern has no group named {g!r}")
            if "required" in c and not isinstance(c["required"], bool):
                self.err(f"{cp}.required", "expected true or false")
            out.append(c)
        return out

    # steps ------------------------------------------------------------

    def step_list(self, v: Any, path: str, *, nonempty: bool = False) -> list[str]:
        if not isinstance(v, list):
            self.err(path, f"expected a list of steps, got {_tname(v)}")
            return []
        if nonempty and not v:
            self.err(path, "expected at least one step")
        out = []
        for i, s in enumerate(v):
            p = f"{path}[{i}]"
            if self.step(s, p, allow_flow=True):
                out.append(p)
        return out

    def step(self, s: Any, path: str, *, allow_flow: bool) -> bool:
        if not isinstance(s, dict):
            self.err(path, f"expected a step object, got {_tname(s)}")
            return False
        kind = s.get("do")
        if not isinstance(kind, str) or kind not in STEP_TYPES:
            self.err(f"{path}.do", f"expected one of {', '.join(sorted(STEP_TYPES))}, got {_short(kind)}")
            return False
        if not allow_flow and kind not in TOOL_STEPS:
            self.err(f"{path}.do", f"a parallel call must be a tool step ({', '.join(sorted(TOOL_STEPS))}), got {kind!r}")
            return False
        before = len(self.errors)
        if kind in TOOL_STEPS:
            self.tool_step(s, path, kind, parallel_member=not allow_flow)
        elif kind == "parallel":
            self.obj(s, path, {"do", "calls"}, {"say", "delay", "id"})
            self._say_delay(s, path)
            calls = s.get("calls")
            if not isinstance(calls, list) or not calls:
                self.err(f"{path}.calls", "expected a non-empty list of tool steps")
            else:
                launches: dict[tuple, tuple[str, str]] = {}
                for i, c in enumerate(calls):
                    self.step(c, f"{path}.calls[{i}]", allow_flow=False)
                    if isinstance(c, dict) and c.get("do") == "delegate":
                        inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                        task, role = c.get("task"), inp.get("role")
                        if not isinstance(task, str) or not (role is None or isinstance(role, str)):
                            continue
                        key = launch_prompt(task, role)
                        child = c.get("child")
                        if key in launches and launches[key][0] != child:
                            self.err(f"{path}.calls[{i}]",
                                     f"its launch message equals that of {launches[key][1]}, which goes to a different "
                                     "child; T3 starts both children with the same message, so which is which cannot "
                                     "be decided")
                        launches.setdefault(key, (child, f"{path}.calls[{i}]"))
        elif kind == "branch":
            self.obj(s, path, {"do", "cases"}, {"else", "id"})
            cases = s.get("cases")
            if not isinstance(cases, list) or not cases:
                self.err(f"{path}.cases", "expected a non-empty list of cases")
            else:
                for i, c in enumerate(cases):
                    cp = f"{path}.cases[{i}]"
                    if self.obj(c, cp, {"when", "steps"}, set()) is not None:
                        self.condition(c.get("when"), f"{cp}.when")
                        self.step_list(c.get("steps"), f"{cp}.steps")
            if "else" in s:
                self.step_list(s["else"], f"{path}.else")
        elif kind == "repeat":
            self.obj(s, path, {"do", "max", "steps"}, {"until", "id"})
            self.number(s.get("max"), f"{path}.max", integer=True, lo=1, hi=MAX_REPEAT)
            self.step_list(s.get("steps"), f"{path}.steps", nonempty=True)
            if "until" in s:
                self.condition(s["until"], f"{path}.until")
        elif kind == "say":
            self.obj(s, path, {"do", "text"}, {"delay", "id"})
            self.string(s.get("text"), f"{path}.text", nonempty=True)
            self._say_delay(s, path)
        elif kind == "reply":
            self.obj(s, path, {"do", "text"}, {"end", "delay", "id"})
            self.string(s.get("text"), f"{path}.text", nonempty=True)
            if "end" in s and (not isinstance(s["end"], str) or s["end"] not in ENDS):
                self.err(f"{path}.end", f"expected one of {', '.join(ENDS)}")
            self._say_delay(s, path)
        elif kind == "set":
            self.obj(s, path, {"do", "var", "value"}, {"id"})
            self.name(s.get("var"), f"{path}.var", defining=True)
            self.string(s.get("value"), f"{path}.value")
        if "id" in s and not isinstance(s["id"], str):
            self.err(f"{path}.id", "expected a string label")
        if len(self.errors) == before:
            self.steps[path] = s
        return True

    def _say_delay(self, s: dict, path: str) -> None:
        if "say" in s:
            self.string(s["say"], f"{path}.say", nonempty=True)
        if "delay" in s:
            self.number(s["delay"], f"{path}.delay", lo=0, hi=MAX_DELAY_SECONDS)

    def tool_step(self, s: dict, path: str, kind: str, *, parallel_member: bool) -> None:
        allowed = set(_STEP_KEYS[kind]) | _COMMON_TOOL_KEYS
        if parallel_member:
            allowed -= {"say", "delay"}
        required = {"do"} | _TOOL_REQUIRED[kind]
        if self.obj(s, path, required, allowed - required) is None:
            return
        self._say_delay(s, path)
        if kind == "bash":
            self.string(s.get("command"), f"{path}.command", nonempty=True)
            if "timeout" in s:
                self.number(s["timeout"], f"{path}.timeout", nullable=True, lo=0)
        elif kind == "read":
            self.string(s.get("path"), f"{path}.path", nonempty=True)
            for k in ("offset", "limit"):
                if k in s:
                    self.number(s[k], f"{path}.{k}", nullable=True, lo=0, integer=True)
        elif kind == "write":
            self.string(s.get("path"), f"{path}.path", nonempty=True)
            self.string(s.get("content"), f"{path}.content")
        elif kind == "edit":
            self.string(s.get("path"), f"{path}.path", nonempty=True)
            edits = s.get("edits")
            if not isinstance(edits, list) or not edits:
                self.err(f"{path}.edits", "expected a non-empty list of {oldText, newText}")
            else:
                for i, e in enumerate(edits):
                    ep = f"{path}.edits[{i}]"
                    if self.obj(e, ep, {"oldText", "newText"}, set()) is not None:
                        self.string(e.get("oldText"), f"{ep}.oldText", nonempty=True)
                        self.string(e.get("newText"), f"{ep}.newText")
        elif kind == "tool":
            if self.string(s.get("name"), f"{path}.name", nonempty=True, template=False) is not None:
                self.tool_names.append((s["name"], f"{path}.name"))
            if "input" in s:
                if not isinstance(s["input"], dict):
                    self.err(f"{path}.input", f"expected an object, got {_tname(s['input'])}")
                else:
                    self.template_value(s["input"], f"{path}.input")
        elif kind == "delegate":
            if self.in_child:
                self.err(path, "a child's handlers may not delegate: delegation is one level deep")
            child = s.get("child")
            if not isinstance(child, str):
                self.err(f"{path}.child", "expected a child name")
            else:
                self.delegated.append((child, f"{path}.child"))
            self.string(s.get("task"), f"{path}.task", nonempty=True)
            if "tool" in s:
                if self.string(s["tool"], f"{path}.tool", nonempty=True, template=False) is not None:
                    self.delegate_names.add(s["tool"])
            if "input" in s:
                inp = s["input"]
                if not isinstance(inp, dict):
                    self.err(f"{path}.input", f"expected an object, got {_tname(inp)}")
                elif "task" in inp:
                    self.err(f"{path}.input.task", "the task goes in the step's own `task`")
                else:
                    if "role" in inp and (not isinstance(inp["role"], str) or inp["role"] not in T3_ROLES):
                        self.err(f"{path}.input.role", f"expected one of T3's roles: {', '.join(T3_ROLES)}")
                    if "mode" in inp and inp["mode"] != "wait":
                        self.err(f"{path}.input.mode", "scripted delegation is blocking: only mode \"wait\" is "
                                 "accepted (an async child's completion would reach the parent as a prompt T3 writes)")
                    self.template_value(inp, f"{path}.input")
            task = s.get("task")
            if isinstance(task, str) and not template_vars(task) and not es_blank(task):
                if utf16_len(t3_task(task)) > T3_TASK_MAX:
                    self.err(f"{path}.task", f"T3 refuses a task longer than {T3_TASK_MAX} characters")
        if "capture" in s:
            self.captures(s["capture"], f"{path}.capture")

    # handlers ---------------------------------------------------------

    def handler_list(self, v: Any, path: str) -> list[Handler]:
        if not isinstance(v, list) or not v:
            self.err(path, "expected a non-empty list of handlers")
            return []
        out = []
        for i, h in enumerate(v):
            hp = f"{path}[{i}]"
            if self.obj(h, hp, {"steps"},
                        {"id", "final", "final_delay", "end", "on_steer", "expect", "capture"}) is None:
                continue
            caps = self.captures(h["capture"], f"{hp}.capture") if "capture" in h else []
            steps = self.step_list(h.get("steps"), f"{hp}.steps")
            final = None
            if "final" in h:
                final = self.string(h["final"], f"{hp}.final", nonempty=True)
            elif not always_replies(h.get("steps")):
                self.err(hp, "needs `final`, unless every path through its steps ends in a `reply`")
            delay = 0.0
            if "final_delay" in h:
                delay = self.number(h["final_delay"], f"{hp}.final_delay", lo=0, hi=MAX_DELAY_SECONDS) or 0.0
            end = h.get("end", "statement")
            if not isinstance(end, str) or end not in ENDS:
                self.err(f"{hp}.end", f"expected one of {', '.join(ENDS)}")
                end = "statement"
            on_steer = h.get("on_steer", "finish")
            if not isinstance(on_steer, str) or on_steer not in ON_STEER:
                self.err(f"{hp}.on_steer", f"expected one of {', '.join(ON_STEER)}")
                on_steer = "finish"
            expect = self.condition(h["expect"], f"{hp}.expect", EXPECT_KEYS) if "expect" in h else None
            label = None
            if "id" in h:
                if not isinstance(h["id"], str):
                    self.err(f"{hp}.id", "expected a string label")
                else:
                    label = h["id"]
            out.append(Handler(hp, label, steps, final, float(delay), end, on_steer, expect, caps))
        return out


def always_replies(steps: Any) -> bool:
    """True when every way through a step list reaches a `reply`, so the
    handler can never run out of steps. Steps run in order and a `reply`
    ends the handler, so a list always replies when any one of its steps
    does. A step always replies when it is a `reply`; a `branch` with an
    `else` whose every case and whose `else` always reply; or a `repeat`
    whose steps always reply (its first round then ends the handler)."""
    if not isinstance(steps, list):
        return False
    return any(_step_always_replies(s) for s in steps)


def _step_always_replies(s: Any) -> bool:
    if not isinstance(s, dict):
        return False
    kind = s.get("do")
    if kind == "reply":
        return True
    if kind == "branch":
        cases = s.get("cases")
        return ("else" in s and isinstance(cases, list) and bool(cases)
                and all(isinstance(c, dict) and always_replies(c.get("steps")) for c in cases)
                and always_replies(s["else"]))
    if kind == "repeat":
        return always_replies(s.get("steps"))
    return False


def _short(v: Any) -> str:
    return repr(v) if isinstance(v, str) else _tname(v)


def _tname(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, float) and not math.isfinite(v):
        return "a non-finite number"
    return {bool: "a boolean", int: "a number", float: "a number", str: "a string",
            list: "a list", dict: "an object"}.get(type(v), type(v).__name__)


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not allowed")


DEFAULT_CHILD_COMPLETION = "Noted."


def load_script(raw: Any, *, root: str = "$", known_vars: frozenset | set = frozenset(),
                reserved_tools: frozenset | set = frozenset()) -> Script:
    """Validate a parsed script, a JSON string or UTF-8 bytes, and return it
    ready to run. Raises ScriptError and nothing else. `root` names the
    script in error messages and step paths; a script loaded beside another
    (an import's replacement script) gets a different root so that their
    step paths cannot collide. `known_vars` are variables that exist without
    this script setting them (the episode it is loaded into); `reserved_tools`
    are delegation tool aliases of a script this one is loaded beside, which
    it may not call through `do: tool` either."""
    try:
        return _load(raw, root, frozenset(known_vars), frozenset(reserved_tools))
    except ScriptError:
        raise
    except RecursionError:
        raise ScriptError([f"{root}: nested too deeply"]) from None


def _exact_float(text: str) -> Any:
    try:
        return decimal.Decimal(text)
    except decimal.DecimalException:
        return None  # the float parse gives inf here, which the validator reports


def _json_problem(v: Any, path: str) -> str | None:
    """The first value that JSON cannot hold, for a script given as a Python object."""
    if v is None or isinstance(v, (str, bool, int, float)):
        return None
    if isinstance(v, list):
        for i, x in enumerate(v):
            p = _json_problem(x, f"{path}[{i}]")
            if p:
                return p
        return None
    if isinstance(v, dict):
        for k, x in v.items():
            if not isinstance(k, str):
                return f"{path}: key {k!r} is not a string"
            p = _json_problem(x, f"{path}.{k}")
            if p:
                return p
        return None
    return f"{path}: not a JSON value ({type(v).__name__})"


def _load(raw: Any, root: str, known_vars: frozenset, reserved_tools: frozenset = frozenset()) -> Script:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ScriptError([f"{root}: not UTF-8 text: {e.reason} at byte {e.start}"]) from None
    source, exact = None, None
    if isinstance(raw, str):
        source = raw
        try:
            raw = json.loads(source, parse_constant=_reject_constant)
            exact = json.loads(source, parse_constant=_reject_constant, parse_float=_exact_float)
        except (json.JSONDecodeError, ValueError) as e:
            raise ScriptError([f"{root}: not valid JSON: {e}"]) from None
    else:
        problem = _json_problem(raw, root)
        if problem:
            raise ScriptError([problem])
    try:
        json.dumps(raw, ensure_ascii=False).encode("utf-8")   # NaN and inf allowed here: this checks text only
    except UnicodeEncodeError:
        raise ScriptError([f"{root}: holds a string that is not valid Unicode"]) from None
    except (ValueError, TypeError):
        pass  # NaN, infinities and non-JSON values get a path-specific error below
    work = copy.deepcopy(raw)   # steps come from this copy; literals in it become exact decimals
    v = _V(exact, root)
    v.defined |= known_vars
    v.delegate_names |= reserved_tools
    top = v.obj(work, root, {"version", "handlers"},
                {"description", "children", "fallback", "vars", "on_child_completion"})
    if top is None:
        raise ScriptError(v.errors)
    if top.get("version") != FORMAT_VERSION or isinstance(top.get("version"), bool):
        v.err(f"{root}.version", f"expected {FORMAT_VERSION}")
    description = None
    if "description" in top:
        if not isinstance(top["description"], str):
            v.err(f"{root}.description", "expected a string")
        else:
            description = top["description"]
    initial: dict[str, str] = {}
    if "vars" in top:
        if not isinstance(top["vars"], dict):
            v.err(f"{root}.vars", "expected an object of name: string")
        else:
            for k, val in top["vars"].items():
                if v.name(k, f"{root}.vars.{k}", defining=True) is None:
                    continue
                if not isinstance(val, str):
                    v.err(f"{root}.vars.{k}", "expected a string")
                else:
                    initial[k] = val
    fallback = top.get("fallback", DEFAULT_FALLBACK)
    if not isinstance(fallback, str) or es_blank(fallback) or template_vars(fallback):
        v.err(f"{root}.fallback", "expected a non-empty string without variables")
        fallback = DEFAULT_FALLBACK
    completion = {"text": DEFAULT_CHILD_COMPLETION, "capture": []}
    if "on_child_completion" in top:
        cp = f"{root}.on_child_completion"
        oc = v.obj(top["on_child_completion"], cp, {"text"}, {"capture"})
        if oc is not None:
            if v.string(oc.get("text"), f"{cp}.text", nonempty=True) is not None:
                completion["text"] = oc["text"]
            if "capture" in oc:
                caps = v.captures(oc["capture"], f"{cp}.capture")
                for i, c in enumerate(caps):
                    if c.get("required"):
                        v.err(f"{cp}.capture[{i}].required", "a capture on T3's completion prompt cannot be required")
                completion["capture"] = caps
    handlers = v.handler_list(top.get("handlers"), f"{root}.handlers")
    children: dict[str, list[Handler]] = {}
    if "children" in top:
        if not isinstance(top["children"], dict):
            v.err(f"{root}.children", "expected an object of name: {handlers}")
        else:
            for name, c in top["children"].items():
                cp = f"{root}.children.{name}"
                if not NAME_RE.match(name):
                    v.err(cp, "child names are identifiers")
                    continue
                if v.obj(c, cp, {"handlers"}, set()) is not None:
                    v.in_child = True
                    children[name] = v.handler_list(c.get("handlers"), f"{cp}.handlers")
                    v.in_child = False
    for name, p in v.tool_names:
        if name in v.delegate_names:
            v.err(p, f"{name!r} is the delegation tool; call it with `do: delegate`, which records the "
                     "delegation (and is refused in a child's handlers)")
    for child, p in v.delegated:
        if child not in children:
            v.err(p, f"no child named {child!r} in {root}.children")
    for name, p in v.used:
        if name not in v.defined:
            v.err(p, f"variable {name!r} is never set: capture it, `set` it, or give it in {root}.vars")
    if v.errors:
        raise ScriptError(v.errors)
    if source is None:
        source = json.dumps(raw, ensure_ascii=False)
    return Script(
        raw=copy.deepcopy(raw),
        # Over the exact text, so two scripts that differ only in how a
        # number is written have different hashes.
        sha256=hashlib.sha256(source.encode()).hexdigest(),
        description=description,
        handlers=handlers,
        children=children,
        fallback=fallback,
        vars=initial,
        steps=v.steps,
        defined=frozenset(v.defined),
        delegate_names=frozenset(v.delegate_names),
        on_child_completion=completion,
        source=source,
    )


def load_script_file(path: str) -> Script:
    with open(path, "rb") as f:
        data = f.read()
    return load_script(data)
