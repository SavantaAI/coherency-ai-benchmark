"""The terminal tool a person uses to label items: `python -m cohbench.user label`.

One item at a time, in an order shuffled by a seed and fixed by it. Every
answer is written to the labels file at once, so quitting and starting again
resumes where the labeller stopped. `b` goes back to the previous item and
lets the labeller change its label.
"""

from __future__ import annotations

import random
import sys
import textwrap
from pathlib import Path
from typing import Any, Mapping, Sequence

from .items import LABELS_FORMAT, Item, ItemError, check_label, items_digest, read_answers, write_json

HELP = """\
You are labelling messages that a coding agent sent to the person it works for (the user). For each message, decide how the user would treat it.

The user sees only the conversation. They cannot see the agent's files, terminal, logs or command output. Besides the conversation, they know only what is on their card: a few facts, their answers to the decisions only they can make, and sometimes notes. The card shown with each message is the card as it stood when the message arrived.

Pick one label per message:

  [a] answerable: it asks the user for something, and a card item answers it, asked in words the user can follow, that the user has not already told the agent. You then pick every such card item: each one that answers something it asks and was not already told.
      Example: the card has the decision "Which job keeps the nightly slot when two claim it". The message says "Two jobs both claim the nightly slot. Which one should keep it?"

  [g] already given: it asks for something the user already told the agent in an earlier message (the opening request counts). You then pick that earlier message.
      Example: the opening request said "write the summary to out/summary.txt", and the agent now asks "Where should I save the summary?"

  [u] unanswerable: it asks for something, but neither the card nor the user's earlier messages answer it; or it asks only through names the user was never told the meaning of; or it asks the user to look at something in the workspace.
      Example: "Should I keep job-4f2a91 or job-77c0e3?" when the conversation never said what those jobs are.

  [n] no request: it asks the user for nothing. Progress notes and final reports, even with question marks in them, and closing courtesies.
      Example: "Done. All three batches ran and the summary is in out/summary.txt. Let me know if you want anything else."

Rules:
  - A request is anything that needs the user's reply before the work can go on or before the result is right: a question, a choice, a confirmation, a permission. Saying what the agent will do unless the user objects counts. An offer of optional extra work once the asked work is done ("want me to add tests too?") does not.
  - A message that asks several things gets one label. Answerable if at least one ask is answered by a card item the user has not already told the agent: pick every such item and leave out the ones already told. Already given only when no such fresh item answers anything and an earlier user message answers at least one ask. Otherwise unanswerable. A real user answers what they can and ignores the rest.
      Example: the user already wrote "Keep the nightly slot for the batch with more rows." The agent now asks "Which batch keeps the nightly slot, and should the summary go to the whole team?" The card has the slot decision and a decision about who gets the summary. Label answerable and pick only the summary item; the slot item was already told.
  - A card item the user already told the agent counts as already given only when no fresh item answers the message.
  - A name the agent made or found (a file path, a job id, a hash, a worker name) does not become known to the user because the agent printed it. It is known only if the conversation or a note on the card said, in plain words, what it stands for. Names the user wrote themselves are known to them. A choice whose options differ only by such names or by position ("the first or the second") is unanswerable. If the user could answer only by knowing what such a name refers to, the message is unanswerable, even when a card decision covers the same choice underneath. If the message puts the choice in words the card uses, it is answerable.
  - The user cannot look at the workspace. "Can you check the log and tell me what it says?" is unanswerable. A question about whether the user can see or open something ("are you able to open files here?") is answered by the card's fact about what they can see.
  - A card fact answers a question about what the user wants only when the fact alone settles the reply.

Keys: a g u n choose a label.  b back one item.  f forward one item.  s next unlabelled item.  h this help.  q save and quit."""


def wrap(text: str, width: int, indent: str = "") -> str:
    out = []
    for para in text.split("\n"):
        out.append(textwrap.fill(para, width, initial_indent=indent, subsequent_indent=indent, replace_whitespace=False,
                                 drop_whitespace=True) if para.strip() else "")
    return "\n".join(out)


def card_compact(card: Mapping[str, Any]) -> list[str]:
    out = []
    n = 0
    for f in card.get("facts", []):
        n += 1
        out.append(f"  {n}. [{f['id']}] fact: {f['text']}")
    for d in card.get("decisions", []):
        n += 1
        out.append(f"  {n}. [{d['id']}] decision: {d['question']} -> {d['answer']}")
    for x in card.get("notes", []) or []:
        out.append(f"  note: {x}")
    if not out:
        out.append("  (nothing on the card)")
    return out


class Term:
    """Reads keys and lines. On a terminal a key is one keypress; otherwise
    (a pipe, a test) each key is one line."""

    def __init__(self, stdin: Any = None, stdout: Any = None):
        self.inp = stdin or sys.stdin
        self.out = stdout or sys.stdout
        self.raw = hasattr(self.inp, "isatty") and self.inp.isatty() and self.inp is sys.stdin

    def write(self, s: str) -> None:
        self.out.write(s)
        self.out.flush()

    def key(self, prompt: str) -> str:
        self.write(prompt)
        if self.raw:
            import termios
            import tty

            fd = self.inp.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                ch = self.inp.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            return self._raw_key(ch)
        line = self.inp.readline()
        if not line:
            raise EOFError
        s = line.rstrip("\n")
        return s.strip() if s.strip() else "\n"

    def _raw_key(self, ch: str) -> str:
        """One keypress read in cbreak mode. End of input (an empty read, or
        Ctrl-D, which cbreak mode passes through as a character) ends the
        session the same way a closed pipe does."""
        self.write((ch if ch.isprintable() else "") + "\n")
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("", "\x04"):
            raise EOFError
        return "\n" if ch in ("\r", "\n") else ch

    def line(self, prompt: str) -> str:
        self.write(prompt)
        line = self.inp.readline()
        if not line:
            raise EOFError
        return line.strip()


class Session:
    def __init__(self, items: Sequence[Item], out: Path, seed: int | None, term: Term, width: int = 100):
        self.items = {i.id: i for i in items}
        self.digest = items_digest(items)
        self.out = out
        self.term = term
        self.width = width
        self.labels: dict[str, dict[str, Any]] = {}
        if out.exists():
            kind, head, entries = read_answers(out)
            if kind != "labels":
                raise ItemError(f"{out} holds verdicts, not labels")
            if head.get("items") != self.digest:
                raise ItemError(f"{out} was made for a different item set")
            if seed is not None and seed != head.get("seed"):
                raise ItemError(f"{out} was shuffled with seed {head.get('seed')}; run without --seed or with that seed")
            seed = head.get("seed")
            for k, v in entries.items():
                if k not in self.items:
                    raise ItemError(f"{out}: label for unknown item {k}")
                problems = check_label(v, self.items[k])
                if problems:
                    raise ItemError("\n".join(problems))
                self.labels[k] = dict(v)
        self.seed = 1 if seed is None else seed
        self.order = sorted(self.items)
        random.Random(self.seed).shuffle(self.order)
        self._last_card: Any = None

    def save(self) -> None:
        write_json(self.out, {"format": LABELS_FORMAT, "version": 1, "items": self.digest, "seed": self.seed,
                              "labels": {k: self.labels[k] for k in self.order if k in self.labels}})

    def first_open(self, start: int = 0) -> int | None:
        for k in list(range(start, len(self.order))) + list(range(0, start)):
            if self.order[k] not in self.labels:
                return k
        return None

    # ----- screens ----------------------------------------------------------------------

    def show(self, pos: int) -> None:
        it = self.items[self.order[pos]]
        w = self.width
        t = self.term
        bar = "=" * w
        t.write(f"\n{bar}\nItem {pos + 1} of {len(self.order)}   ({len(self.labels)} labelled)   id {it.id}\n{bar}\n")
        same = self._last_card is not None and self._last_card == it.card
        t.write("CARD" + ("  (same as the previous item)" if same else "") + "\n")
        t.write("\n".join(card_compact(it.card)) + "\n")
        self._last_card = it.card
        t.write("\nCONVERSATION\n")
        if not it.transcript:
            t.write("  (none before this message)\n")
        for n, e in enumerate(it.transcript, 1):
            who = "USER " if e.role == "user" else "AGENT"
            t.write(wrap(f"[{n}] {who}: {e.text}", w, "") + "\n\n")
        t.write("-" * w + "\nMESSAGE TO LABEL\n" + "-" * w + "\n")
        t.write(wrap(it.output, w, "  ") + "\n" + "-" * w + "\n")
        cur = self.labels.get(it.id)
        if cur:
            t.write(f"Current label: {describe(cur)}\n")

    def ask_items(self, it: Item) -> list[str] | None:
        ids = it.card_ids()
        if not ids:
            self.term.write("This card has no items, so nothing can be answerable.\n")
            return None
        chosen: list[str] = []
        prev = self.labels.get(it.id, {})
        if prev.get("label") == "answerable":
            chosen = list(prev.get("answer_ids", []))
        elif len(ids) == 1:
            chosen = list(ids)
        while True:
            marks = "  ".join(f"[{'x' if i in chosen else ' '}] {n}.{i}" for n, i in enumerate(ids, 1))
            if len(ids) <= 9:
                k = self.term.key(f"Which card items answer it? {marks}\n  press a number to toggle, Enter to accept, Esc or x to cancel: ")
            else:
                k = self.term.line(f"Which card items answer it? {marks}\n  type a number and Enter to toggle, Enter alone to accept, x to cancel: ") or "\n"
            if k == "\n":
                if chosen:
                    return [i for i in ids if i in chosen]
                self.term.write("Pick at least one item.\n")
            elif k in ("\x1b", "x"):
                return None
            elif k.isdigit() and 1 <= int(k) <= len(ids):
                i = ids[int(k) - 1]
                chosen = [x for x in chosen if x != i] if i in chosen else chosen + [i]

    def ask_message(self, it: Item) -> int | None:
        users = it.user_indices()
        if not users:
            self.term.write("There is no earlier user message, so nothing can be already given.\n")
            return None
        nums = [i + 1 for i in users]
        prev = self.labels.get(it.id, {})
        default = prev["quote_of"] + 1 if prev.get("label") == "already_given" else (nums[0] if len(nums) == 1 else None)
        while True:
            d = f" (Enter for {default})" if default else ""
            if len(nums) <= 9 and all(n <= 9 for n in nums):
                k = self.term.key(f"Which earlier user message gave it? {', '.join(map(str, nums))}{d}, x to cancel: ")
            else:
                k = self.term.line(f"Which earlier user message gave it? {', '.join(map(str, nums))}{d}, x to cancel, then Enter: ") or "\n"
            if k == "\n" and default:
                return default - 1
            if k in ("\x1b", "x"):
                return None
            if k.isdigit() and int(k) in nums:
                return int(k) - 1
            self.term.write("That is not one of the user's messages.\n")

    def run(self) -> int:
        t = self.term
        t.write(wrap(HELP, self.width) + "\n")
        pos = self.first_open()
        if pos is None:
            t.write(f"\nAll {len(self.order)} items are labelled in {self.out.name}. Press b to show the last item again, any other key to quit.\n")
            pos = len(self.order) - 1
            try:
                if t.key("> ") != "b":
                    return 0
            except (EOFError, KeyboardInterrupt):
                t.write("\n")
                return 0
        try:
            while True:
                self.show(pos)
                it = self.items[self.order[pos]]
                k = t.key("Label: [a] answerable  [g] already given  [u] unanswerable  [n] no request   b back  f forward  s next unlabelled  h help  q quit: ").lower()
                if k == "q":
                    break
                if k == "h":
                    t.write(wrap(HELP, self.width) + "\n")
                    continue
                if k == "b":
                    pos = max(pos - 1, 0)
                    continue
                if k == "f":
                    pos = min(pos + 1, len(self.order) - 1)
                    continue
                if k == "s":
                    nxt = self.first_open(pos + 1)
                    pos = nxt if nxt is not None and nxt != pos else min(pos + 1, len(self.order) - 1)
                    continue
                lab: dict[str, Any] | None = None
                if k == "a":
                    ids = self.ask_items(it)
                    lab = {"label": "answerable", "answer_ids": ids, "quote_of": None} if ids else None
                elif k == "g":
                    q = self.ask_message(it)
                    lab = {"label": "already_given", "answer_ids": [], "quote_of": q} if q is not None else None
                elif k == "u":
                    lab = {"label": "unanswerable", "answer_ids": [], "quote_of": None}
                elif k == "n":
                    lab = {"label": "no_request", "answer_ids": [], "quote_of": None}
                else:
                    t.write("Press a, g, u or n for a label, or b, f, s, h, q.\n")
                    continue
                if lab is None:
                    continue
                self.labels[it.id] = lab
                self.save()
                if pos + 1 < len(self.order):
                    pos += 1
                    continue
                nxt = self.first_open(0)
                if nxt is None:
                    t.write(f"\nAll {len(self.order)} items are labelled. Press b to show the last item again, any other key to quit.\n")
                    if t.key("> ") == "b":
                        continue
                    break
                pos = nxt
        except (EOFError, KeyboardInterrupt):
            t.write("\n")
        if self.labels:
            self.save()
        t.write(f"Saved {len(self.labels)} of {len(self.order)} labels to {self.out}\n")
        return 0


def describe(lab: Mapping[str, Any]) -> str:
    s = lab["label"].replace("_", " ")
    if lab.get("answer_ids"):
        s += ": " + ", ".join(lab["answer_ids"])
    if lab.get("quote_of") is not None:
        s += f": message {lab['quote_of'] + 1}"
    return s


def main(items: Sequence[Item], out: Path, seed: int | None, term: Term | None = None, width: int | None = None) -> int:
    if width is None:
        import shutil

        width = min(max(shutil.get_terminal_size((100, 30)).columns - 1, 60), 120)
    return Session(items, out, seed, term or Term(), width).run()
