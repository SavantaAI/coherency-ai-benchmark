"""The classifier policy: the simulated user's judge, built on a pinned
open-weight model that only ever picks one option key.

`Classifier(cfg, client)` satisfies `cohbench.runner.policy.UserPolicy`.
`factory(cfg, client)` gives a `PolicyFactory` for the runner; every policy
it makes shares the one endpoint client. Delivered card-item ids come from
the runner's transcript side channel. Proposed replies create no delivery state.

How one verdict is reached (README.md, "Question structure"):

1. Request: does the message ask the user for anything? No: `no_request`.
2. Card item, once per available item in card order: is it asked, in words
   the user can follow (A), only through internal names (B), or not (C)?
3. Options, for each item answered A: if the message asks the user to pick
   between options, can the user tell which one the item picks or rules out?
   C (only internal names) and D (the item does not separate them) drop it.
4. For each item kept: had the user already told the agent? The runner's
   delivered ids name the actual message. Model inference is used only for
   unannotated calibration messages, never for a runner message with no ids.
5. If no item was kept: did an earlier user message already answer the
   request? The model picks the message or none.

Label: `answerable` naming every kept item not already told; otherwise
`already_given` quoting the first told item's message, or the message picked
in step 5; otherwise `unanswerable`.
"""

from __future__ import annotations

import copy
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from cohbench.runner.policy import AGENT, USER, AwarenessJudgement, Entry, Judgement, PolicyError, PolicyFactory

from . import prompts as P
from .api import Client, DeadlineExceeded, EndpointError
from .config import Config
from .revision import code_revision

MESSAGE_OVERHEAD = 8  # tokens a chat message costs beyond its text, for the estimate
KEEP = ("A", "B")  # options answers that keep an item


class ContextOverflow(PolicyError):
    """The card, the user's messages and the message to judge do not fit the model's context."""


@dataclass
class _Chain:
    """One verdict's conversation with the model."""

    client: Client
    messages: list[dict[str, str]]
    deadline: float
    asked: list[dict[str, Any]] = field(default_factory=list)
    pending: str = ""  # text to put before the next question in the same user message

    def ask(self, question: str, keys: Sequence[str], qtype: str, item: str | None = None) -> str:
        if self.pending:
            self.messages.append({"role": "user", "content": self.pending + question})
            self.pending = ""
        else:
            self.messages.append({"role": "user", "content": question})
        r = self.client.choose(self.messages, keys, self.deadline)
        got = r["choice"]
        self.messages.append({"role": "assistant", "content": P.answer(got)})
        rec: dict[str, Any] = {"type": qtype, "question": question, "options": list(keys), "chosen": got, "ms": r["ms"],
                               "attempts": r["attempts"], "served": r["served"], "usage": r["usage"]}
        if "served_change" in r:
            rec["served_change"] = r["served_change"]
        if item is not None:
            rec["item"] = item
        self.asked.append(rec)
        return got



def estimate(cfg: Config, text: str) -> int:
    """Tokens for `text`, estimated from its length. No tokenizer is available
    for a hosted model; the config's characters per token is set low so the
    estimate errs high."""
    return math.ceil(len(text) / cfg.context["chars_per_token"])


def items_of(card: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    return [("fact", f) for f in card.get("facts", [])] + [("decision", d) for d in card.get("decisions", [])]


class Classifier:
    def __init__(self, cfg: Config, client: Client):
        self.cfg = cfg
        self.client = client
        self.code = code_revision()  # once, before any verdict: nothing slow runs after the deadline check

    def told_by_delivery(self, item_id: str, transcript: Sequence[Entry]) -> int | None:
        """The actual delivered message carrying an item, as the runner records it."""
        deliveries = getattr(transcript, "delivered_ids", (None,) * len(transcript))
        return next((i for i in range(len(transcript) - 1, -1, -1)
                     if transcript[i].role == USER and item_id in (deliveries[i] or ())), None)

    # ----- context ---------------------------------------------------------------------

    def _layout(self, card: Mapping[str, Any], transcript: Sequence[Entry], output: str) -> tuple[str, dict[str, Any]]:
        """The first user message's context (conversation, card, message),
        fitted so the largest request this verdict can reach stays within the
        context budget. Returns (context text, note)."""
        limit = self.cfg.context["max_input_tokens"] - self.cfg.context["margin"]
        cpt = self.cfg.context["chars_per_token"]
        est = lambda t: estimate(self.cfg, t)  # noqa: E731
        users = [i + 1 for i, e in enumerate(transcript) if e.role == USER]
        uopts = [(i + 1, e.text) for i, e in enumerate(transcript) if e.role == USER]
        longest = P.answer(str(max(users))) if users else P.answer("A")

        def turn(q: str) -> int:
            return est(q) + est(longest) + 2 * MESSAGE_OVERHEAD

        # The largest reachable request is the last one of the longest path:
        # the request, every item question, every options question, then
        # either a told question for every item or the one already-answered question.
        items = items_of(card)
        base = turn(P.Q_REQUEST) + sum(turn(P.q_item(it, kind)) + turn(P.q_options(it["id"])) for kind, it in items)
        tail = max(sum(turn(P.q_told(it["id"], uopts)) for _, it in items) if users else 0,
                   turn(P.q_given(uopts)) if users else 0)
        fixed = est(P.SYSTEM) + MESSAGE_OVERHEAD + base + tail
        lines = [P.entry(i + 1, e.role, e.text) for i, e in enumerate(transcript)]
        card_txt = P.card_lines(card)

        def render(ls: Sequence[str], msg: str) -> str:
            return P.transcript_block(ls) + P.REST.format(card=card_txt, output=msg)

        ctx = render(lines, P.message(output))
        total = est(ctx) + fixed
        note: dict[str, Any] = {"entries": len(transcript), "input_tokens_est_max": total}
        if total <= limit:
            return ctx, note
        # Drop the oldest agent messages first; keep every user message.
        dropped: list[int] = []
        for i in [i for i, e in enumerate(transcript) if e.role == AGENT]:
            if total <= limit:
                break
            lines[i] = P.dropped(i + 1)
            dropped.append(i)
            ctx = render(lines, P.message(output))
            total = est(ctx) + fixed
        note["dropped"] = dropped
        if total <= limit:
            note["input_tokens_est_max"] = total
            return ctx, note
        # Still too long: cut the middle of the message itself, keeping its start and its end.
        room = est(P.message(output)) - (total - limit)
        if room < self.cfg.context["keep_message_tokens"]:
            raise ContextOverflow(f"the card and the user's messages leave about {max(room, 0)} tokens for the message; "
                                  f"at least {self.cfg.context['keep_message_tokens']} are needed")
        keep = int(room * cpt)
        while True:
            if keep <= 0:
                raise ContextOverflow("the message cannot be cut to fit the context")
            head = keep // 3
            cut = (output[:head], output[len(output) - (keep - head):])
            ctx = render(lines, P.message(output, cut))
            total = est(ctx) + fixed
            if total <= limit:
                break
            keep -= max(int((total - limit) * cpt), 1)
        note["message_cut_chars"] = len(output) - keep
        note["input_tokens_est_max"] = total
        return ctx, note

    # ----- the verdict -------------------------------------------------------------------

    def judge(self, card: Mapping[str, Any], transcript: Sequence[Entry], output: str, *, deadline=None, client=None) -> Judgement:
        client = client or self.client
        t0 = client.clock()
        deadline = min(deadline if deadline is not None else math.inf, t0 + self.cfg.limits["verdict_deadline_s"])
        with client.session():
            ctx, note = self._layout(card, transcript, output)
            ch = _Chain(client, [{"role": "system", "content": P.SYSTEM}], deadline, pending=ctx)
            label, ids, quote_of, quote, how = self._decide(ch, card, transcript)
            rec = {"classifier": "cohbench.user", "code": self.code, "config": self.cfg.digest, "prompt": P.VERSION, "context": note,
                   "questions": ch.asked, "decided_by": how, "usage": _total_usage(ch.asked),
                   "ms": round((self.client.clock() - t0) * 1000)}
            j = Judgement(label, answer_ids=tuple(ids), quote_of=quote_of, quote=quote, record=rec)
            # The last check before returning: a verdict complete after its deadline is no verdict.
            if self.client.clock() > ch.deadline:
                raise DeadlineExceeded("the verdict's deadline passed before the verdict was complete")
            return j

    def start_judgement(self, card, transcript, output, *, utcnow, timeout_s=None):
        from .job import JudgementJob
        return JudgementJob(self, copy.deepcopy(card), transcript, output, utcnow=utcnow, timeout_s=timeout_s)

    @property
    def verdict_deadline_s(self):
        return self.cfg.limits["verdict_deadline_s"]

    def judge_batch(self, card, transcript, outputs, *, timeout_s):
        """Independent judgements with one cutoff and stable output positions.

        Late daemon workers cannot change delivery state or runner records.
        The real monotonic wait also bounds test clients with stationary clocks.
        """
        budget = max(0, min(timeout_s, self.cfg.limits["verdict_deadline_s"]))
        cutoff = self.client.clock() + budget
        wall_cutoff = time.monotonic() + budget
        results = [None] * len(outputs)
        done = [threading.Event() for _ in outputs]
        closed = threading.Event()
        gate = threading.Lock()

        def work(index, output):
            try:
                result = self.judge(copy.deepcopy(card), transcript, output,
                                    deadline=cutoff, client=self.client.fork())
            except DeadlineExceeded:
                result = None
            except Exception as exc:
                result = exc
            with gate:
                if not closed.is_set() and self.client.clock() <= cutoff and time.monotonic() <= wall_cutoff:
                    results[index] = result
                done[index].set()

        if budget:
            for i, output in enumerate(outputs):
                if self.client.clock() >= cutoff or time.monotonic() >= wall_cutoff:
                    break
                threading.Thread(target=work, args=(i, output), daemon=True).start()
            for event in done:
                event.wait(max(0, min(cutoff - self.client.clock(), wall_cutoff - time.monotonic())))
        with gate:
            closed.set()
            answer = list(results)
        for result in answer:
            if isinstance(result, Exception):
                raise result
        return answer

    def _decide(self, ch: _Chain, card: Mapping[str, Any], transcript: Sequence[Entry]) -> tuple[str, list[str], int | None, str | None, str]:
        if ch.ask(P.Q_REQUEST, ("A", "B"), "request") == "B":
            return "no_request", [], None, None, "request"
        users = [i + 1 for i, e in enumerate(transcript) if e.role == USER]
        uopts = [(i + 1, e.text) for i, e in enumerate(transcript) if e.role == USER]
        nkeys = tuple(str(n) for n in users) + ("N",)
        picked = []
        for kind, it in items_of(card):
            if ch.ask(P.q_item(it, kind), ("A", "B", "C"), "item", it["id"]) == "A":
                picked.append((kind, it))
        # Options the user cannot relate to the item, or that the item does not separate: the item does not answer the ask.
        picked = [(k, it) for k, it in picked if ch.ask(P.q_options(it["id"]), ("A", "B", "C", "D"), "options", it["id"]) in KEEP]
        fresh: list[str] = []
        told: list[tuple[int, str | None, str]] = []
        for _, it in picked:
            i = self.told_by_delivery(it["id"], transcript)
            if i is not None:
                told.append((i, None, "told_delivery"))
                continue
            # Model inference is only for unannotated calibration messages.
            # A delivered message assigned to another item is not an option.
            deliveries = getattr(transcript, "delivered_ids", (None,) * len(transcript))
            unknown = [(n, text) for n, text in uopts if deliveries[n - 1] is None]
            if unknown:
                got = ch.ask(P.q_told(it["id"], unknown), tuple(str(n) for n, _ in unknown) + ("N",), "told", it["id"])
                if got != "N":
                    told.append((int(got) - 1, None, "told_model"))
                    continue
            fresh.append(it["id"])
        if fresh:
            return "answerable", fresh, None, None, "item"
        if told:
            i, q, how = told[0]
            return "already_given", [], i, q, how
        if users:
            got = ch.ask(P.q_given(uopts), nkeys, "given")
            if got != "N":
                return "already_given", [], int(got) - 1, None, "given"
        return "unanswerable", [], None, None, "none"

    # ----- the post-run check ------------------------------------------------------------

    def classify_awareness(self, question: str, reply: str) -> AwarenessJudgement:
        t0 = self.client.clock()
        with self.client.session():
            cpt = self.cfg.context["chars_per_token"]
            limit = self.cfg.context["max_input_tokens"] - self.cfg.context["margin"] - estimate(self.cfg, P.SYSTEM_AWARE) - 4 * MESSAGE_OVERHEAD
            body = P.awareness(question, reply)
            note: dict[str, Any] = {}
            over = estimate(self.cfg, body) - limit
            if over > 0:
                keep = int(len(reply) - over * cpt)
                while True:
                    if keep <= 0:
                        raise ContextOverflow("the awareness question alone does not fit the context")
                    body = P.awareness(question, None, (reply[: keep // 3], reply[len(reply) - (keep - keep // 3):]))
                    if estimate(self.cfg, body) <= limit:
                        break
                    keep -= max(int((estimate(self.cfg, body) - limit) * cpt), 1)
                note["reply_cut_chars"] = len(reply) - keep
            ch = _Chain(self.client, [{"role": "system", "content": P.SYSTEM_AWARE}], t0 + self.cfg.limits["verdict_deadline_s"])
            got = ch.ask(body, ("A", "B", "C"), "awareness")
            outcome = {"A": "suspected", "B": "not_suspected", "C": "unclear"}[got]
            rec = {"classifier": "cohbench.user", "code": self.code, "config": self.cfg.digest, "prompt": P.VERSION,
                   "questions": ch.asked, **note, "usage": _total_usage(ch.asked), "ms": round((self.client.clock() - t0) * 1000)}
            a = AwarenessJudgement(outcome, rec)
            if self.client.clock() > ch.deadline:  # the last thing before returning
                raise DeadlineExceeded("the deadline passed before the classification was complete")
            return a


def _total_usage(asked: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for q in asked:
        for k, v in q.get("usage", {}).items():
            out[k] = out.get(k, 0) + v
    return out


def factory(cfg: Config, client: Client) -> PolicyFactory:
    return lambda: Classifier(cfg, client)
