"""The episode clock and the episode end: one place for both.

The episode clock (decision D-020) leaves out intervals in which the host did
work the agent did not cause: the container handoff at a controlled reset,
probe copies, exports. Its origin is the delivery of the opening prompt to
the agent. The full trajectory keeps wall times at full precision
(microseconds from the origin, `us`) beside their milliseconds (`t`), and
lists the intervals in its header. Every ordering and membership decision is
made on wall microseconds; conversion to the episode clock (the wall time
minus the excluded intervals before it) and to milliseconds happens once,
when a bundle record is written. A charged user wait is a real wait and is
never excluded.

The episode end is the runner's `episode_end`. What survives it, by record
type, is `END_CONTRACT`; the view, the change log and the observations apply
it through the functions here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

EXCLUDED_REASONS = ("handoff", "probe", "export")

# The cutoff contract, by record type (docs/trajectory-format.md, "The episode end").
END_CONTRACT = {
    "view: user message": "kept when sent at or before the end",
    "view: output to the user": "kept when delivered at or before the end",
    "view: turn": "a turn still active at the end (open, or ending later) gets a turn_end with end 'interrupted' at the end time",
    "view: wait": "a wait still open at the end, or ending later, ends at the end: ended 'episode_end' (or 'budget_cutoff' when the end is one), charged up to the end on the episode clock, an uncharged one included",
    "view: runner event": "kept when at or before the end; nothing follows episode_end",
    "observations: attempt": "kept when started at or before the end; one that finished after it has result status 'unfinished' and no fingerprint or end time",
    "change log: state": "the final state is the batch of the runner's end mark (the only mark that selects state), which must agree with the end time, or else the last batch closed at or before the end",
    "change log: metric reading": "at most one per retained batch: the earliest successful reading of that batch's exact tree, whatever asked for it and whenever it finished; a failed reading is never kept; it carries no time",
    "change log: anything else": "dropped after the end",
}


class ClockError(ValueError):
    pass


def to_ms(us: int) -> int:
    """Microseconds to whole milliseconds, rounding down (toward the past),
    the one rounding used. The driver and the runner stamp times as ISO
    strings cut to the millisecond, and a time so stamped is the floor of
    its microseconds, so the bundle's milliseconds agree with theirs."""
    return us // 1000


@dataclass(frozen=True)
class Interval:
    us: int  # wall microseconds from the episode's origin (the opening prompt's delivery)
    us_end: int
    reason: str


def _interval(i: Interval | dict[str, Any]) -> Interval:
    if isinstance(i, Interval):
        return i
    if "us" in i:
        return Interval(i["us"], i["us_end"], i["reason"])
    return Interval(i["t"] * 1000 if isinstance(i["t"], int) else i["t"], i["t_end"] * 1000 if isinstance(i["t_end"], int) else i["t_end"], i["reason"])


class Clock:
    """Every decision (order, membership, which side of a boundary) is made on
    wall microseconds; `__call__` converts once, to episode milliseconds, when
    a record is written."""

    def __init__(self, intervals: Iterable[Interval | dict[str, Any]] = ()) -> None:
        ivs = [_interval(i) for i in intervals]
        for i in ivs:
            if i.reason not in EXCLUDED_REASONS or not (isinstance(i.us, int) and isinstance(i.us_end, int)) or i.us_end <= i.us:
                raise ClockError(f"excluded interval {i}: a reason in {EXCLUDED_REASONS} and a positive length")
        ivs.sort(key=lambda i: i.us)
        for a, b in zip(ivs, ivs[1:]):
            if b.us < a.us_end:
                raise ClockError(f"excluded intervals {a} and {b} overlap")
        self.intervals = ivs

    def inside(self, us: int) -> Interval | None:
        """The interval `us` falls strictly inside; its start and end are outside."""
        return next((i for i in self.intervals if i.us < us < i.us_end), None)

    def episode_us(self, us: int, what: str = "event") -> int:
        i = self.inside(us)
        if i is not None:
            raise ClockError(f"{what} at {us} us falls inside the excluded {i.reason} interval {i.us}..{i.us_end}")
        return us - sum(i.us_end - i.us for i in self.intervals if i.us_end <= us)

    def __call__(self, us: int, what: str = "event") -> int:
        """Wall microseconds to episode milliseconds, never before the origin."""
        return max(0, to_ms(self.episode_us(us, what)))

    def wall_us(self, ms: int) -> int:
        """Episode milliseconds back to wall microseconds (the start of an
        interval for a time at it), for a record read back from a file."""
        u, gone = ms * 1000, 0
        for i in self.intervals:
            if i.us - gone < u:
                gone += i.us_end - i.us
        return u + gone

    def as_header(self) -> list[dict[str, Any]]:
        return [{"us": i.us, "us_end": i.us_end, "reason": i.reason} for i in self.intervals]


def episode_end(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((e for e in events if e["type"] == "episode_end"), None)
