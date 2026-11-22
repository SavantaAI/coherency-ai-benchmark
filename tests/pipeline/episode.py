"""Record the simulated harness at the production trajectory input boundary."""

import json
from dataclasses import asdict
from datetime import timedelta

from cohbench.runner.port import ExportRejected, ExportResult
from cohbench.trajectory import Hints, RunnerEvent, Segment, normalise, t3, validate
from cohbench.trajectory import bundle
from cohbench.trajectory.adapters import PiProfile
from tests.runner.fake import iso_us
from tests.tasks.minipi import MiniPi, ScriptedEpisode, run_metric


class RecordingPi(MiniPi):
    def step(self, steer=None):
        start = len(self.log)
        result = super().step(steer)
        for call in self.log[start:]:
            self.recorder(call)
        return result


class ExportingEpisode(ScriptedEpisode):
    def __init__(self, *args, probes, **kwargs):
        self.raw = {}
        self.probe_answers = probes
        super().__init__(*args, **kwargs)
        self.pi.recorder = self.record_call

    def _wrap(self, watcher):
        super()._wrap(watcher)
        if self.metric_cmd is not None:
            # FakeWatcher seeds a generic metric. Replace it at initialization
            # with the task's actual measurement before any step executes.
            (watcher.root / "metrics.jsonl").write_text("")
            watcher.metric(
                run_metric(*self.metric_cmd, watcher.tree), reason="baseline"
            )

    def records(self):
        return self.raw.setdefault(
            self.segment,
            {
                "thread": {
                    "id": f"thread-{self.segment}",
                    "providerInstanceId": "pi",
                    "lineage": {"parentThreadId": None},
                },
                "runs": [],
                "items": [],
            },
        )

    def item(self, kind, at, **fields):
        rows = self.records()["items"]
        rows.append(
            {
                "id": f"raw-{self.segment}-{len(rows)}",
                "threadId": f"thread-{self.segment}",
                "runId": self.cur_run,
                "ordinal": len(rows),
                "status": "completed",
                "type": kind,
                "startedAt": at,
                "completedAt": at,
                **fields,
            }
        )

    def record_sent(self, sent, text, intent):
        records = self.records()
        if not any(row["id"] == sent.run_id for row in records["runs"]):
            records["runs"].append(
                {
                    "id": sent.run_id,
                    "threadId": sent.thread_id,
                    "ordinal": len(records["runs"]),
                    "status": "running",
                    "requestedAt": sent.utc,
                    "startedAt": sent.utc,
                    "completedAt": None,
                    "userMessageId": sent.message_id,
                }
            )
        self.item(
            "user_message",
            sent.utc,
            messageId=sent.message_id,
            text=text,
            createdBy="user",
            inputIntent=intent,
        )
        return sent

    def _sent(self, via, text, **kwargs):
        return self.record_sent(super()._sent(via, text, **kwargs), text, "turn_start")

    def correct(self, text, **kwargs):
        return self.record_sent(super().correct(text, **kwargs), text, "steer")

    def record_call(self, call):
        at = iso_us(self.c.wall + timedelta(seconds=self.next_t - self.c.clock))
        if call["tool"] == "bash":
            self.item(
                "command_execution",
                at,
                title="bash",
                input=call["input"]["command"],
                output=call["result"],
                exitCode=1 if call["is_error"] else 0,
            )
        else:
            self.item(
                "dynamic_tool",
                at,
                title=call["tool"],
                toolName=call["tool"],
                input=call["input"],
                output=call["result"],
            )

    def wait(self, timeout_s):
        turn = super().wait(timeout_s)
        for output in turn.outputs:
            self.item(
                "assistant_message",
                output.delivered_utc,
                id=output.item_id,
                runId=output.run_id,
                text=output.text,
                streaming=False,
            )
        if turn.state != "time_limit":
            row = next(r for r in self.records()["runs"] if r["id"] == turn.run_id)
            row.update(
                status="completed" if turn.state == "finished" else "interrupted",
                completedAt=turn.at or iso_us(self.c.wall),
            )
        return turn

    def probe(self, question):
        self.probe_reply = self.probe_answers[question]
        return super().probe(question)

    def export(self, dest, handoff):
        self.handoff = handoff
        try:
            events = [
                RunnerEvent(
                    row["type"], row["utc"], row.get("ref"),
                    row.get("fields", {}), row.get("end_utc"),
                )
                for row in handoff.runner_events
                if row.get("continuation") in (None, "launched")
            ]
            hints = Hints(
                user_kinds=handoff.user_kinds,
                answers=handoff.answers,
                runner=events,
                excluded=handoff.excluded,
                episode_start_utc=self.start_utc,
                handbacks_recorded=True,
            )
            segments = [
                Segment(t3.from_export({"threads": [self.raw[n]]}), f"thread-{n}")
                for n in sorted(self.raw)
            ]
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "raw.json").write_text(json.dumps(self.raw, sort_keys=True))
            (dest / "hints.json").write_text(json.dumps(asdict(hints), sort_keys=True))
            result = normalise(segments, PiProfile(), hints)
            check = validate(result.trajectory)
            if not check.ok:
                raise ExportRejected("trajectory", check.errors)
            made = bundle.make(
                result,
                [bundle.WatcherInput.from_dir(w.root) for w in self.watchers],
                handoff.identifiers,
                handoff.observations,
                episode_record=handoff.episode_record,
                probe_targets=handoff.probe_targets,
                end_mark=handoff.end_mark,
            )
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "trajectory.jsonl").write_text(result.trajectory.dumps())
            bundle.write(made, dest / "bundle")
            check = bundle.validate(bundle.read(dest / "bundle"))
            if not check["ok"]:
                raise ExportRejected("bundle", [str(check["errors"])])
            return ExportResult(
                {"scoring_bundle": ["bundle"], "trajectory": ["trajectory.jsonl"]},
                {"bundle": check},
            )
        except ExportRejected:
            raise
        except Exception as exc:
            raise ExportRejected("pipeline", [f"{type(exc).__name__}: {exc}"]) from exc
