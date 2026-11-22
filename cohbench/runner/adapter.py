"""The adapter: `EpisodePort` on the real driver, `cohbench.driver.Episode`.

    from cohbench.runner.adapter import open_episode
    run_episode(task, cfg, open_episode, ("scripted", rules_file), out)

What it maps, and what it absorbs:

- Shapes. The driver's `Sent`, `TurnResult` (with `outputs` as dicts),
  `Question`, `SecretRequest`, `ProbeResult`, mark acknowledgements, usage
  summary and excluded intervals become the port's dataclasses. The driver
  objects for a pending question or secret are kept by item id, since
  `answer` and `refuse_secret` take them back.
- Errors. `TurnNotRunning` stays `TurnNotRunning`; any other `EpisodeError`
  is an `EpisodeFailure`; `ExportFailed(stage, reasons)` is
  `ExportRejected(stage, reasons)`. A non-fatal `DriverFailure` (a read
  failed before anything was sent) is retried once; a fatal one is an
  `EpisodeFailure`. `correct()` while the main run has ended and a subagent
  still works (the driver's plain `EpisodeError`, nothing to steer) is
  `TurnNotRunning`: the runner holds the correction for the hand-back.
- Usage. The gateway summary's four token fields are summed; usage is
  complete when every call reported it or the driver settled it at a
  confirmed interrupt (`calls_not_complete == 0`).
- Probes on the scripted upstream need the copy's reply. It comes from the
  scripted run file (below), by question, with a default.
- Runner events. The driver is opened with `driver_events` set to the
  user kinds of its own sends, the controlled reset and the probes (dated at
  the start of their excluded intervals). The runner owns every other event:
  correction deliveries (with the D-025 mark the driver took at the observed
  delivery), verdicts, waits, planted events, reopens, budget cutoffs,
  `unjudged` and the episode end. The adapter checks them with the
  trajectory unit's `check_runner_inputs` and forwards each once at export:
  waits through `record_user_wait`, the rest through `add_runner_event`. It
  drops the runner's own resets and probes after checking that the driver's
  agree in number.
- Deliveries. `correction_delivery` returns the delivery time, the
  driver's mark and the watcher's word on it (`mark_settled`,
  `mark_quiescent`); `correct()` passes on `Sent.delivery` when the driver
  already knows it.
- Turns (D-029). `Turn.started_by_t3` lists the runs in `TurnResult.runs`
  that T3 started itself.
- `note_user_wait` is a no-op: the wait is forwarded at export with its cause.

The scripted run file. `--upstream scripted:<file>` names either a plain
script for the scripted server, or a wrapper that also gives the probe
copies' replies:

    {"format": "cohbench-scripted-run", "version": 1,
     "script": {...the scripted server's script...},
     "probes": {"<probe question>": "<the copy's reply>"},
     "probe_default": "I don't know."}
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from .port import (Delivery, Description, EpisodeFailure, EpisodeSpec, ExportHandoff, ExportRejected, ExportResult, Interval, MarkAck,
                   Option, Output, Pending, ProbeOutcome, Prompt, Sent, Turn, TurnNotRunning, Usage)

RUN_FORMAT = "cohbench-scripted-run"
DRIVER_OWNED = ("controlled_reset", "probe")


def _last(first: Any, last: Any) -> int | None:
    """The last batch of a straddling run: the watcher's `straddled_last`, else (an
    older watcher) the one straddled batch."""
    if isinstance(last, int) and not isinstance(last, bool):
        return last
    return first if isinstance(first, int) and not isinstance(first, bool) else None
DRIVER_EVENTS = ("user_kinds", "controlled_reset", "probe")
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")
_IMAGES: dict[str, Any] = {}


def load_scripted_run(path: Path) -> tuple[dict[str, Any], dict[str, str], str]:
    """(script, probe replies by question, default probe reply)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(doc, dict) and doc.get("format") == RUN_FORMAT:
        if doc.get("version") != 1 or not isinstance(doc.get("script"), dict):
            raise ValueError(f"{path}: a {RUN_FORMAT} file has version 1 and a script")
        probes = doc.get("probes", {})
        if not isinstance(probes, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in probes.items()):
            raise ValueError(f"{path}: probes maps question text to reply text")
        return doc["script"], dict(probes), str(doc.get("probe_default", "I don't know."))
    if isinstance(doc, dict) and "handlers" in doc:
        return doc, {}, "I don't know."
    raise ValueError(f"{path}: neither a scripted server script nor a {RUN_FORMAT} file")


def _images(harness: str) -> Any:
    if harness not in _IMAGES:
        from cohbench.env import provision

        _IMAGES[harness] = provision(harness)[0]
    return _IMAGES[harness]


def open_episode(spec: EpisodeSpec, *, images: Any = None, episode_cls: Any = None) -> "DriverEpisode":
    """The factory `run_episode` takes. Builds missing images once per process."""
    return DriverEpisode(spec, images=images if images is not None else _images(spec.harness), episode_cls=episode_cls)


class DriverEpisode:
    def __init__(self, spec: EpisodeSpec, *, images: Any, episode_cls: Any = None):
        from cohbench.driver import Episode
        from cohbench.env import Upstream, WatcherSpec

        kind, path = spec.upstream
        self.probe_replies: dict[str, str] = {}
        self.probe_default = "I don't know."
        if kind == "scripted":
            script, self.probe_replies, self.probe_default = load_scripted_run(path)
            upstream = Upstream.scripted(script)
        elif kind == "real":
            upstream = Upstream.real(path)  # the environment loads the key into the gateway only; never read here
        else:
            raise ValueError(f"upstream {kind!r}")
        base = WatcherSpec()
        watcher = WatcherSpec(ignore=tuple(dict.fromkeys((*base.ignore, *spec.watcher_ignore))), metric_cmd=spec.metric_command,
                              task_dir=spec.metric_dir, metric_timeout_s=spec.metric_timeout_s)
        self.ep = (episode_cls or Episode)(
            spec.harness, model=spec.model, upstream=upstream, images=images, workspace=spec.workspace_dir, manifest=spec.workspace_manifest,
            out_dir=spec.out_dir, mode=spec.mode, coached_sentence=spec.coached_sentence, watcher=watcher, driver_events=DRIVER_EVENTS, collect=spec.collect)
        self._sent: dict[str, Any] = {}  # message id -> the driver's Sent, for correction_delivery
        self._pending: dict[str, Any] = {}  # item id -> the driver's Question or SecretRequest
        self.started = False
        self.absorbed: list[str] = []  # what the adapter had to work around, for the export report

    # ----- conversions ---------------------------------------------------------------------

    def _err(self, exc: Exception) -> Exception:
        from cohbench.driver import EpisodeError
        from cohbench.driver import TurnNotRunning as DriverTurnNotRunning

        if isinstance(exc, DriverTurnNotRunning):
            return TurnNotRunning(str(exc))
        if isinstance(exc, EpisodeError):
            done = list(getattr(exc, "done", None) or [])
            if getattr(exc, "handoff_done", False) and "handoff" not in done:
                done.append("handoff")
            return EpisodeFailure(str(exc), fatal=getattr(exc, "fatal", True) is not False, unsent=bool(getattr(exc, "unsent", False)),
                                  done=done, not_sent=list(getattr(exc, "not_sent", None) or []))
        from cohbench.env.docker import DockerError

        if isinstance(exc, DockerError):  # the environment failed under the driver: a driver failure, not a runner abort
            return EpisodeFailure(f"environment: {exc}")
        return exc

    def _call(self, fn: Any, *a: Any, **kw: Any) -> Any:
        try:
            try:
                return fn(*a, **kw)
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "fatal", True) is False and not getattr(exc, "unsent", False):  # a read failed before anything was sent: once more
                    self.absorbed.append(f"retried {getattr(fn, '__name__', 'a call')} after a non-fatal driver failure: {exc}"[:300])
                    return fn(*a, **kw)
                raise
        except Exception as exc:  # noqa: BLE001
            mapped = self._err(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    def _port_sent(self, s: Any) -> Sent:
        if s.message_id:
            self._sent[s.message_id] = s
        sub = getattr(s, "submitted", None)
        return Sent(utc=s.sent_utc, clock_s=s.episode_s, run_id=s.run_id, thread_id=s.thread_id, message_id=s.message_id,
                    request_id=s.request_id, mechanism=s.mechanism, delivered_utc=s.delivered_utc, delivery_mark=getattr(s, "delivery_mark", None),
                    submitted=dict(sub) if isinstance(sub, dict) else None)

    @staticmethod
    def _delivery(r: Any, ds: Any) -> Delivery | None:
        if r is None:
            return None
        if hasattr(r, "delivered_utc") and hasattr(r, "mark_label"):  # the driver's Delivery
            if not r.delivered_utc:
                return None
            taken = getattr(r, "mark_taken", r.mark_label is not None)
            return Delivery(r.delivered_utc, r.mark_label if taken else None, getattr(r, "mark_settled", None) if taken else None,
                            getattr(r, "mark_quiescent", None) if taken else None, straddled=getattr(r, "mark_straddled", None) if taken else None,
                            straddled_last=_last(getattr(r, "mark_straddled", None), getattr(r, "mark_straddled_last", None)) if taken else None)
        if isinstance(r, dict):
            utc, mark = r.get("delivered_utc") or r.get("utc"), r.get("mark")
        elif isinstance(r, (tuple, list)):
            utc, mark = r[0], (r[1] if len(r) > 1 else None)
        else:
            utc, mark = str(r), getattr(ds, "delivery_mark", None)
        return Delivery(utc, mark) if utc else None

    @staticmethod
    def _secret_pending(item_id: str, thread_id: str | None, label: Any, reason: Any, fields: Any) -> Pending:
        fs = [f for f in fields or [] if isinstance(f, dict)]
        return Pending("secret", item_id, thread_id, None, label=label, reason=reason,
                       fields=tuple(str(f.get("label") or f.get("name") or "") for f in fs),
                       placeholders=tuple(f.get("placeholder") if isinstance(f.get("placeholder"), str) else None for f in fs))

    def _request_pending(self, o: dict[str, Any]) -> Pending | None:
        """A request output that is not the marked one, from the T3 item the
        driver attaches to it (`raw`, or the request object's `raw`)."""
        raw = o.get("raw")
        obj = o.get("request") or o.get("question") or o.get("secret")
        if raw is None and obj is not None:
            raw = getattr(obj, "raw", None)
        if not isinstance(raw, dict):
            return None
        if o["kind"] == "secret":
            return self._secret_pending(o["item_id"], o.get("thread_id"), raw.get("label") or raw.get("title"), raw.get("reason"), raw.get("fields"))

        class _Q:  # what _prompts reads
            def __init__(self, item: dict[str, Any]) -> None:
                self.raw, self.question_ids, self.text, self.options = item, [], item.get("title") or "", []
        return Pending("question", o["item_id"], o.get("thread_id"), raw.get("requestId"), self._prompts(_Q(raw)))

    @staticmethod
    def _prompts(question: Any) -> tuple[Prompt, ...]:
        out = []
        for q in question.raw.get("questions") or []:
            opts = tuple(Option(str(o.get("label") or o.get("value")), None, o.get("description")) for o in q.get("options") or [])
            out.append(Prompt(str(q.get("id")), q.get("question") or q.get("header") or "", q.get("header"), opts, bool(q.get("multiSelect"))))
        if not out and question.question_ids:
            out = [Prompt(question.question_ids[0], question.text, None, tuple(Option(o) for o in question.options))]
        return tuple(out)

    # ----- the port ------------------------------------------------------------------------

    def start(self, text: str) -> Sent:
        s = self._call(self.ep.start, text)
        self.started = True
        return self._port_sent(s)

    def wait(self, timeout_s: float) -> Turn:
        r = self._call(self.ep.wait, timeout_s=timeout_s, poll_s=min(1.0, max(timeout_s / 2, 0.2)))
        pending = None
        if r.question is not None:
            q = r.question
            self._pending[q.item_id] = q
            pending = Pending("question", q.item_id, q.thread_id, q.request_id, self._prompts(q))
        elif r.secret is not None:
            s = r.secret
            self._pending[s.item_id] = s
            pending = self._secret_pending(s.item_id, s.thread_id, s.label, s.reason, s.fields)
        # Every request is shown as the trajectory unit renders it, the same text the scorer view
        # holds, whatever its position: the marked one from its object, another from its T3 item.
        shown = {pending.item_id: pending.text()} if pending is not None else {}
        for o in r.outputs:
            if o.get("kind") in ("question", "secret") and o.get("item_id") not in shown:
                other = self._request_pending(o)
                if other is not None:
                    shown[o["item_id"]] = other.text()
                else:
                    self.absorbed.append(f"request {o.get('item_id')} shown as the driver's flat text: its output carries no T3 item to render")
        outputs = tuple(Output(o["kind"], shown.get(o.get("item_id"), o.get("text") or ""), o.get("item_id"), o.get("run_id"), o.get("delivered_utc"))
                        for o in r.outputs)
        closing = pending.item_id if pending is not None else (r.final_item_id if r.state == "finished" else None)
        by_t3 = tuple(str(x.get("run_id")) for x in getattr(r, "runs", None) or [] if x.get("started_by") == "t3" and x.get("run_id"))
        # D-037: the hand-back's own instant, as the driver's turn-end mark names it (the pending
        # request's start, or the closing run's end); else the request's start. Not `ended_utc`,
        # which is when the driver built its report.
        at = getattr(r, "handback_utc", None)  # the driver's hand-back instant
        if not at:
            at = (r.mark or {}).get("at") if isinstance(getattr(r, "mark", None), dict) else None
        if not at and pending is not None:
            obj = self._pending.get(pending.item_id)
            at = (getattr(obj, "raw", None) or {}).get("startedAt")
        at = at if r.state in ("finished", "question", "secret") else None
        detail = None
        if r.state in ("failed", "refused"):
            detail = "; ".join(r.errors[:3]) or json.dumps(r.refusal, default=str)[:300] if (r.errors or r.refusal) else None
        return Turn(r.state, r.run_id, outputs, pending, detail, closing, by_t3, at or None)

    @staticmethod
    def _limit(fn: Any, timeout_s: float | None) -> dict[str, Any]:
        """The time left before the deadline, as the driver call's own limit (D-037)."""
        if timeout_s is None or "timeout_s" not in inspect.signature(fn).parameters:
            return {}
        return {"timeout_s": max(timeout_s, 0.001)}

    def reply(self, text: str, *, timeout_s: float | None = None) -> Sent:
        return self._port_sent(self._call(self.ep.reply, text, **self._limit(self.ep.reply, timeout_s)))

    def answer(self, pending: Pending, value: dict[str, str], *, timeout_s: float | None = None) -> Sent:
        q = self._pending.get(pending.item_id)
        if q is None:
            raise EpisodeFailure(f"no pending question {pending.item_id} from this episode")
        return self._port_sent(self._call(self.ep.answer, q, dict(value), **self._limit(self.ep.answer, timeout_s)))

    def refuse_secret(self, pending: Pending, text: str, *, timeout_s: float | None = None) -> Sent:
        s = self._pending.get(pending.item_id)
        if s is None:
            raise EpisodeFailure(f"no pending secret request {pending.item_id} from this episode")
        return self._port_sent(self._call(self.ep.refuse_secret, s, text, **self._limit(self.ep.refuse_secret, timeout_s)))

    def correct(self, text: str, *, timeout_s: float | None = None) -> Sent:
        from cohbench.driver import episode as dep

        main_ended = getattr(dep, "MainRunEnded", None)
        try:
            ds = self._call(self.ep.correct, text, **self._limit(self.ep.correct, timeout_s))
        except EpisodeFailure as exc:
            cause = exc.__cause__
            if main_ended is not None and isinstance(cause, main_ended):
                # D-029: the main run ended while a subagent works; nothing to steer. The runner
                # holds the correction for T3's continuation run or the hand-back.
                self.absorbed.append(f"correction held: {cause}"[:300])
                raise TurnNotRunning(f"the main run has ended while a subagent works: {cause}") from cause
            if not exc.fatal:
                # A child thread could not be read; nothing was sent: held exactly as above.
                self.absorbed.append(f"correction held: {exc}"[:300])
                raise TurnNotRunning(f"the driver could not read the tree before steering, nothing sent: {exc}"
                                     f" (done {exc.done}, not sent {exc.not_sent})") from exc
            raise
        sent = self._port_sent(ds)
        known = getattr(ds, "delivery", None)
        if known is None and ds.delivered_utc:  # delivered at once, the Delivery not on the Sent: ask for it
            known = self._call(self.ep.correction_delivery, ds)
        d = self._delivery(known, ds)
        if d is not None:
            sent = Sent(**{**sent.__dict__, "delivered_utc": d.utc, "delivery_mark": d.mark, "delivery_settled": d.settled, "delivery_quiescent": d.quiescent,
                           "delivery_straddled": d.straddled, "delivery_straddled_last": d.straddled_last})
        return sent


    def correction_delivery(self, sent: Sent) -> Delivery | None:
        ds = self._sent.get(sent.message_id or "")
        if ds is None:
            return None
        d = self._delivery(self._call(self.ep.correction_delivery, ds), ds)
        if d is not None and getattr(ds, "mechanism", None):
            d = Delivery(d.utc, d.mark, d.settled, d.quiescent, ds.mechanism, d.straddled, d.straddled_last)  # the driver fills the observed route in on its Sent
        return d

    def interrupt(self) -> None:
        # The driver stops the whole tree and returns once it reads stopped; on a stopped tree it sends nothing.
        self._call(self.ep.interrupt)

    def reset(self, text: str, *, timeout_s: float | None = None, dispatch_timeout_s: float | None = None) -> Sent:
        """`timeout_s`: the idle gate and the handoff (infrastructure; the driver's
        default when None). `dispatch_timeout_s`: the continuation's launch, from
        when the handoff ended: the episode time left."""
        kw = self._limit(self.ep.reset, timeout_s)
        if dispatch_timeout_s is not None and "dispatch_timeout_s" in inspect.signature(self.ep.reset).parameters:
            kw["dispatch_timeout_s"] = max(dispatch_timeout_s, 0.001)
        return self._port_sent(self._call(self.ep.reset, text, **kw))

    def probe(self, question: str) -> ProbeOutcome:
        # Every copy-side problem is an outcome in the driver, never an exception;
        # an exception is about the main episode (the main container not resumed,
        # say), so it is a driver failure, not a diagnostic (B4).
        r = self._call(self.ep.probe, question, scripted_reply=self.probe_replies.get(question, self.probe_default))
        cleanup = r.detail.get("cleanup") if isinstance(r.detail, dict) else None
        incomplete = dict(cleanup) if isinstance(cleanup, dict) and cleanup.get("complete") is False else None
        if incomplete is None and getattr(r, "cleanup_complete", None) is False:
            incomplete = {"complete": False}
        return ProbeOutcome(r.outcome, r.answer, r.reason, incomplete)

    def mark(self, label: str, at: str | None = None) -> MarkAck:
        """D-037: a mark names an instant; the watcher never closes a batch for it."""
        kw = {"at": at} if at is not None and "at" in inspect.signature(self.ep.mark).parameters else {}
        if at is not None and not kw:
            self.absorbed.append(f"mark {label}: the driver's mark takes no instant; marked at the call")
        ack = self._call(self.ep.mark, label, **kw)
        if not ack.get("ok", True):
            raise EpisodeFailure(f"the watcher did not acknowledge mark {label}: {ack.get('error')}", fatal=False)
        return MarkAck(label, ack.get("batch"), ack.get("settled"), ack.get("quiescent"), ack.get("accepted"), ack.get("straddled"), ack.get("covered_utc"),
                       _last(ack.get("straddled"), ack.get("straddled_last")))

    def clock(self) -> float:
        return self.ep.elapsed() if self.started else 0.0

    def origin_utc(self) -> str | None:
        return getattr(getattr(self.ep, "clock", None), "start_utc", None) if self.started else None

    def excluded(self) -> list[Interval]:
        return [Interval(e["start_utc"], e["end_utc"], e["reason"]) for e in self.ep.excluded()]

    def note_user_wait(self, ref: dict[str, str] | None, start_utc: str, end_utc: str) -> None:
        return None  # forwarded at export, with its charge and cause

    def usage(self) -> Usage:
        u = self._call(self.ep.usage) if self.started else {}  # a gateway failure is a driver failure, as every other call
        if not u:
            return Usage(0 if not self.started else None, not self.started, {})
        values = [u.get(f) for f in TOKEN_FIELDS]
        tokens = sum(v for v in values if isinstance(v, int)) if any(isinstance(v, int) for v in values) else (0 if u.get("calls") == 0 else None)
        complete = u.get("calls_not_complete", 0) == 0 and u.get("usage_status") in ("complete", None) or u.get("calls") == 0
        return Usage(tokens, bool(complete), dict(u))

    def watcher_dirs(self) -> list[Path]:
        return list(self.ep.watcher_dirs())

    def describe(self) -> Description:
        d = self.ep.describe()
        return Description(d["harness"], d.get("harness_version"), d.get("t3_version"), d.get("driver_version"), d["model"])

    def collect(self):
        return self._call(self.ep.collect)

    def export(self, dest: Path, handoff: ExportHandoff) -> ExportResult:
        from cohbench.driver import ExportFailed, TaskInfo

        from cohbench.trajectory import Hints, RunnerEvent
        from cohbench.trajectory.normalise import check_runner_inputs

        ours = [e for e in handoff.runner_events if e["type"] in DRIVER_OWNED]
        theirs = [e for e in self.ep.runner_events if e["type"] in DRIVER_OWNED]
        # Handoffs with handoffs: each side records one `controlled_reset` per completed
        # handoff, whether its continuation was launched or never sent; the continuation
        # states are compared where both sides name one.
        notes = [f"{kind}: the runner has {a}, the driver {b}" for kind in DRIVER_OWNED
                 if (a := sum(1 for e in ours if e["type"] == kind)) != (b := sum(1 for e in theirs if e["type"] == kind))]
        if not notes:
            mine = [e.get("continuation") for e in ours if e["type"] == "controlled_reset"]
            drv = [e.get("continuation") for e in theirs if e["type"] == "controlled_reset"]
            notes += [f"controlled_reset {n + 1}: the runner's continuation is {a}, the driver's {b}" for n, (a, b) in enumerate(zip(mine, drv))
                      if a in ("launched", "unsent") and b in ("launched", "unsent") and a != b]
        if notes:
            raise ExportRejected("handoff", [f"the runner and the driver disagree on events the driver records: {n}" for n in notes])
        forward = [e for e in handoff.runner_events if e["type"] not in DRIVER_OWNED]
        targets = {pid: dict(t) for pid, t in handoff.probe_targets.items()}  # already in the trajectory unit's shape (record.bundle_targets)
        hints = Hints(user_kinds=dict(handoff.user_kinds), answers=dict(handoff.answers), excluded=list(handoff.excluded),
                      runner=[RunnerEvent(e["type"], e["utc"], e["ref"], dict(e["fields"]), e.get("end_utc")) for e in forward])
        problems = check_runner_inputs(hints, targets)
        if problems:
            raise ExportRejected("handoff", problems)
        for e in forward:
            f = dict(e["fields"])
            if e["type"] == "wait":
                takes = inspect.signature(self.ep.record_user_wait).parameters
                extra = {k: v for k, v in f.items() if k not in ("charged_ms", "ended", "cause")}
                kw = {k: v for k, v in extra.items() if k in takes}
                if "cause" in f or "cause" in takes and "causes" not in takes:
                    kw["cause"] = f.get("cause") or (f.get("causes") or ["verdict_reply"])[0]
                ev = self.ep.record_user_wait(e["ref"], e["utc"], e["end_utc"], charged_ms=f.get("charged_ms"), ended=f.get("ended", "reply"), **kw)
                if isinstance(ev, dict) and isinstance(ev.get("fields"), dict):
                    # Until the driver's record_user_wait takes these fields: the event it returns gets the runner's own.
                    ev["fields"].update({k: v for k, v in extra.items() if k not in takes})
                    if "cause" not in f:
                        ev["fields"].pop("cause", None)
            else:
                self.ep.add_runner_event(e["type"], e["utc"], ref=e["ref"], end_utc=e.get("end_utc"), **f)
        task = TaskInfo(identifiers=handoff.identifiers, observations=handoff.observations, episode=handoff.episode_record)
        kw: dict[str, Any] = {}
        if "end_mark" in inspect.signature(self.ep.export).parameters:
            kw["end_mark"] = handoff.end_mark  # the runner's end mark bounds the last watcher log
        if handoff.end_utc and "end_utc" in inspect.signature(self.ep.export).parameters:
            kw["end_utc"] = handoff.end_utc  # D-035: the end instant, an elapsed cutoff's deadline
        try:
            out = self.ep.export(Path(dest), task=task, end_reason=handoff.end_reason, user_kinds=handoff.user_kinds, answers=handoff.answers,
                                 marks=handoff.marks, probe_targets=targets, **kw)
        except ExportFailed as exc:
            raise ExportRejected(exc.stage, list(exc.reasons), getattr(exc, "report", None)) from exc
        except Exception as exc:  # noqa: BLE001
            mapped = self._err(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        report = dict(out.get("report") or {})
        report["adapter_absorbed"] = list(self.absorbed)
        report["dest"] = out.get("dest")
        return ExportResult({k: list(v) for k, v in out.get("files", {}).items()}, report)

    def close(self) -> None:
        self.ep.close()
        left = getattr(self.ep, "left_over", None)
        if left:
            self.ep.close()  # the driver retries whatever is still listed
            left = getattr(self.ep, "left_over", None)
            if left:
                raise EpisodeFailure(f"close left Docker objects behind: {left}"[:500])
