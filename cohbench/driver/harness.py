"""Per-harness descriptions: everything the driver needs to know about one
harness, in one place. Nothing else in the driver names a harness.

Pi 1.6.0 is exercised by the tests (tests/driver). Claude Code 2.1.330 is
kept from the driver proof and is unexercised here: every Claude Code turn
through T3 was refused by the model API in the proof (D-002), so its
question tool, steering capability and instruction file are taken from the
proof's notes and T3's source, not from a run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Harness:
    name: str
    # The trajectory unit's adapter profile (cohbench/trajectory/adapters).
    profile: str
    provider_instance: str
    # T3's model slug per provider (cohbench/env/site.py Provider.name): Pi
    # names the provider, Claude Code does not. Only Anthropic is exercised.
    model_formats: dict[str, str] = field(default_factory=dict)
    options: dict[str, str] = field(default_factory=dict)
    version_cmd: tuple[str, ...] = ()
    # Where the harness keeps its own files inside the container.
    home_dirs: tuple[str, ...] = ()
    session_dir: str = ""
    # T3's provider status cache, holding the version T3 detected.
    status_cache: str = ""
    # The project instruction file the harness reads from the workspace root.
    instruction_file: str = ""
    # How a blocking question reaches T3: the harness's own tool, if it has one.
    question_tool: str | None = None
    # T3's message tool as this harness names it (exact names).
    send_to_user_tools: tuple[str, ...] = ("mcp__t3-code__send_to_user", "mcp__t3_code__send_to_user")
    # The reference harness (SPEC section 2) gets the model under test as its
    # own default model, a recorded run setting; a native harness runs at its
    # defaults and nothing is written.
    reference: bool = False
    exercised: bool = False
    notes: tuple[str, ...] = ()

    def model_slug(self, model: str, provider: str = "anthropic") -> str:
        try:
            return self.model_formats[provider].format(model=model)
        except KeyError:
            raise ValueError(f"{self.name} has no model slug form for provider {provider!r}") from None

    def model_selection(self, model: str, options: dict[str, str] | None = None, provider: str = "anthropic") -> dict:
        opts = dict(self.options if options is None else options)
        return {"instanceId": self.provider_instance, "model": self.model_slug(model, provider), "options": [{"id": k, "value": v} for k, v in opts.items()]}

    def default_model_files(self, model: str, options: dict[str, str] | None = None, provider: str = "anthropic") -> dict[str, str]:
        """The reference harness's own default model set to the episode's, so a
        child started with model "default" runs on it too (driver proof).
        Empty for a native harness."""
        if not self.reference:
            return {}
        opts = dict(self.options if options is None else options)
        if self.name == "pi":
            pi_provider, bare = self.model_slug(model, provider).split("/", 1)
            body = {"defaultProvider": pi_provider, "defaultModel": bare, "defaultThinkingLevel": opts.get("thinking", "off")}
            return {"/home/node/.pi/agent/settings.json": json.dumps(body, indent=2) + "\n"}
        raise ValueError(f"no default-model file for {self.name}")


HARNESSES: dict[str, Harness] = {
    "pi": Harness(
        name="pi",
        profile="pi",
        provider_instance="pi",
        model_formats={"anthropic": "anthropic/{model}"},
        options={"thinking": "off"},
        version_cmd=("pi", "--version"),
        home_dirs=("/home/node/.pi",),
        session_dir="/home/node/.pi/agent/sessions",
        status_cache="/home/node/.t3/caches/pi.json",
        # Pi 1.6.0 loads AGENTS.md (or CLAUDE.md) from the working directory
        # and its parents without project trust (docs/configuration.md,
        # "Context files"). Shown in tests/driver by the scripted server's
        # stored system prompts.
        instruction_file="AGENTS.md",
        question_tool=None,
        reference=True,
        exercised=True,
        notes=("Stock Pi has no question tool; a question is plain text that ends the turn.",),
    ),
    "claude": Harness(
        name="claude",
        profile="claude-code",
        provider_instance="claudeAgent",
        model_formats={"anthropic": "{model}"},
        # Nothing set: a native harness runs at its own defaults (SPEC
        # section 2); a caller's `options` is an explicit override.
        options={},
        version_cmd=("claude", "--version"),
        home_dirs=("/home/node/.claude",),
        session_dir="/home/node/.claude/projects",
        status_cache="/home/node/.t3/caches/claudeAgent.json",
        instruction_file="CLAUDE.md",
        question_tool="AskUserQuestion",
        reference=False,
        exercised=False,
        notes=(
            "Unexercised: every Claude Code turn through T3 was refused by the model API in the driver proof.",
            "Instruction file, question tool and steering are taken from the proof's notes and T3's adapter, not from a run.",
            "T3 can compact an idle Claude thread on its own (IdleCompactionService, beta); not handled here.",
        ),
    ),
}


# T3 Code's own git ref namespace in the workspace repository (its checkpoint
# refs). The watcher never records it (D-026).
DRIVER_REF_NAMESPACES = ("refs/t3/",)


def instruction_files() -> tuple[str, ...]:
    """Every harness's instruction file name, whichever harness runs (D-026)."""
    return tuple(sorted({h.instruction_file for h in HARNESSES.values() if h.instruction_file}))


def harness(name: str) -> Harness:
    try:
        return HARNESSES[name]
    except KeyError:
        raise ValueError(f"no harness {name!r}; known: {sorted(HARNESSES)}") from None


# DIAGNOSTIC ONLY, NOT PART OF THE REFERENCE HARNESS. Stock Pi 1.6.0 has no
# tool that asks the user a question. To exercise T3's pending-request path on
# Pi anyway, the driver proof loaded this extension, and tests/driver load it to exercise `answer`. It registers
# one tool that opens Pi's blocking `select` (with options) or `input` dialog;
# T3 maps both to a pending `user_input` request. The file name, tool name and
# text are neutral on purpose: the agent can read them.
PI_DIALOG_EXTENSION_NAME = "ask.ts"
PI_DIALOG_EXTENSION = """\
import { Type } from "typebox";

export default function (pi: any) {
  pi.registerTool({
    name: "ask_user",
    label: "Ask the user",
    description:
      "Ask the user a question and wait for the reply. Pass options when the reply is a choice between known values.",
    parameters: Type.Object({
      question: Type.String({ description: "The question to show the user" }),
      options: Type.Optional(Type.Array(Type.String(), { description: "Choices to offer" })),
    }),
    async execute(_id: string, params: any, _signal: any, _onUpdate: any, ctx: any) {
      const answer =
        params.options && params.options.length > 0
          ? await ctx.ui.select(params.question, params.options)
          : await ctx.ui.input(params.question, "Your answer");
      const text = answer === undefined ? "The user did not answer." : String(answer);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
}
"""
