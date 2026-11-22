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
    def _run(self, name, inp):
        paths = {s["path"]: self.home / s["path"][2:] for s in self.collect_specs}
        before = {key: path.read_bytes() if path.exists() else b"" for key, path in paths.items()}
        result = super()._run(name, inp)
        added = {}
        for key, path in paths.items():
            after = path.read_bytes() if path.exists() else b""
            assert after.startswith(before[key]), "the tool log must be append only"
            added[key] = [json.loads(line) for line in after[len(before[key]):].splitlines()]
        self.call_logs.append(added)
        return result

    def step(self, steer=None):
        start = len(self.log)
        self.call_logs = []
        result = super().step(steer)
        for call, logs in zip(self.log[start:], self.call_logs, strict=True):
            self.recorder(call, logs)
        return result


class ExportingEpisode(ScriptedEpisode):
    def __init__(self, *args, probes, **kwargs):
        self.raw = {}
        self.segment_logs = {}
        self.log_mapping = []
        self.probe_answers = probes
        super().__init__(*args, **kwargs)
        self.pi.recorder = self.record_call
        self.pi.collect_specs = self.collector.specs
        assert all(s["path"].startswith("~/") for s in self.collector.specs)

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

    def record_call(self, call, logs):
        end = self.c.wall + timedelta(seconds=self.next_t - self.c.clock)
        start = end - timedelta(seconds=1)
        at = iso_us(end)
        for path, lines in logs.items():
            if not lines:
                continue
            mapped = self.segment_logs.setdefault(self.segment, {}).setdefault(path, [])
            base = round(start.timestamp() * 1000)
            assert len(lines) < 500, "too many runs for the simulated call interval"
            for i, line in enumerate(lines):
                assert line["start_ms"] <= line["end_ms"]
                if i:
                    assert lines[i - 1]["end_ms"] <= line["start_ms"]
                # Fixed ordered slots remove real execution speed from the fake
                # clock. The original log remains in MiniPi.home for inspection.
                translated = {**line, "start_ms": base + i * 1000 // len(lines),
                              "end_ms": base + (i + 1) * 1000 // len(lines) - 1}
                mapped.append(translated)
                self.log_mapping.append({"segment": self.segment, "path": path,
                                         "call_start": iso_us(start), "call_end": at,
                                         "original": line, "mapped": translated})
        if call["tool"] == "bash":
            self.item(
                "command_execution",
                at,
                title="bash",
                input=call["input"]["command"],
                output=call["result"],
                exitCode=1 if call["is_error"] else 0,
                startedAt=iso_us(start),
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

    def collect(self):
        rows = self.segment_logs.get(self.segment, {})
        self.collected_files = {
            s["path"]: ("".join(json.dumps(line, sort_keys=True) + "\n" for line in rows[s["path"]]).encode()
                        if s["path"] in rows else None)
            for s in self.collector.specs
        }
        return super().collect()

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
            collected = self.collect()
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
            (dest / "log-mapping.json").write_text(json.dumps(self.log_mapping, sort_keys=True))
            collected.write(dest / "collected" / "blobs")
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
                collected=collected,
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
