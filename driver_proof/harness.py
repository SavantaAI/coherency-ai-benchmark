"""Per-harness settings: provider instance, model slug, options, native files.

User prompts are not here. Every operation sends both harnesses the same text
(ops.py). The one harness-specific piece of setup is the Pi dialog extension
below, which is a diagnostic and not part of the reference harness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Harness:
    name: str
    provider_instance: str
    model: str
    # Model options as T3 model-selection options. Chosen for low spend.
    options: dict[str, str] = field(default_factory=dict)
    version_cmd: tuple[str, ...] = ()
    # Where the harness keeps its own session files inside the container.
    native_session_dir: str = ""
    # T3's provider status cache, holding the version T3 detected.
    status_cache: str = ""
    # Name of the harness's own blocking question tool, if it ships one.
    native_question_tool: str | None = None
    # Where a diagnostic Pi extension is installed (Pi only).
    extension_dir: str | None = None

    def default_model_files(self) -> dict[str, str]:
        """Harness settings that make the harness's own default model the run's
        model, so a child the agent starts with model "default" (T3's
        delegate_task) or the harness's native subagent tool uses it too."""
        if self.name == "pi":
            provider, model = self.model.split("/", 1)
            body = {"defaultProvider": provider, "defaultModel": model, "defaultThinkingLevel": self.options.get("thinking", "off")}
            return {"/home/node/.pi/agent/settings.json": json.dumps(body, indent=2) + "\n"}
        body = {"model": self.model, "env": {"CLAUDE_CODE_SUBAGENT_MODEL": self.model}}
        return {"/home/node/.claude/settings.json": json.dumps(body, indent=2) + "\n"}

    def option_list(self) -> list[dict[str, str]]:
        return [{"id": key, "value": value} for key, value in self.options.items()]

    def model_selection(self) -> dict:
        return {"instanceId": self.provider_instance, "model": self.model, "options": self.option_list()}


HARNESSES = {
    "pi": Harness(
        name="pi",
        provider_instance="pi",
        model="anthropic/claude-haiku-5-5",
        options={"thinking": "off"},
        version_cmd=("pi", "--version"),
        native_session_dir="/home/node/.pi/agent/sessions",
        status_cache="/home/node/.t3/caches/pi.json",
        native_question_tool=None,
        extension_dir="/home/node/.pi/agent/extensions",
    ),
    "claude": Harness(
        name="claude",
        provider_instance="claudeAgent",
        model="claude-haiku-5-5",
        options={"effort": "low"},
        version_cmd=("claude", "--version"),
        native_session_dir="/home/node/.claude/projects",
        status_cache="/home/node/.t3/caches/claudeAgent.json",
        native_question_tool="AskUserQuestion",
    ),
}

# DIAGNOSTIC ONLY, NOT PART OF THE REFERENCE HARNESS. Stock Pi 1.6.0 has no
# tool that asks the user a question. To exercise T3's pending-request path on
# Pi anyway, operation 2 loads this extension in a second thread. It registers
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
