"""Routing: a multi-step register job in a seeded domain. A workspace tool
gives every entry an opaque id; on a seeded step two entries claim one
thing. Sibling A's handbook settles it; sibling B's does not, and only the
user can (SPEC.md section 4, D-044, D-045)."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from typing import Any, Callable

from . import fillers as F
from .common import (FILLERS, NOTES_PATH, NO_FILES, Files, GenerateError, IGNORE, Instance, Options, bash, cap, handler, literal, policy_doc,
                     post_run_handler, reset_event, run_doc, task_doc, write)
from .fillers import FIRST, LAST
from .rng import Rng

COMPANIES = ["Harbour Supplies", "Northgate Trading", "Bluefield Goods", "Kestrel Imports", "Oakline Wholesale", "Riverside Packaging",
             "Summit Home", "Tern and Co", "Marlow Brothers", "Copperleaf Ltd", "Eastway Stores", "Pinecrest Supply", "Lowmoor Trade",
             "Brightwater Retail", "Fenwick Studio", "Juniper Lane", "Halden Print", "Moss and Ash", "Greyfriars Foods",
             "Wexley Paper", "Saltire Crafts", "Orchard Row", "Quayside Media", "Ridgeway Tools", "Bellfield Labs"]


@dataclass(frozen=True)
class Domain:
    key: str
    title: str
    tool: str
    in_dir: str
    deliverable: str
    prefix: str
    columns: tuple[str, str, str]  # item, owner, resource
    label: str  # how a resource value is shown, "{}" for the value
    item: str  # singular noun for one entry
    items: str  # plural
    res: str  # what is claimed, as the user says it
    phrase: str  # an entry in the user's words, from {name} and {owner}
    names: tuple[str, ...]
    owners: str  # "people", "companies" or "classes"
    values: Callable[[Rng], list[str]]
    role: str  # the card fact about who the user is
    job: str  # the request, as the user would put it
    handbook: str
    rule_first: str
    rule_last: str
    unit: str  # one input file covers one ...


def _nums(lo: int, hi: int) -> Callable[[Rng], list[str]]:
    return lambda r: [str(n) for n in range(lo, hi + 1)]


DOMAINS = (
    Domain("garden", "Community garden plots", "plots", "requests", "out/plot-list.txt", "pr", ("bed", "gardener", "plot"), "plot {}",
           "request", "requests", "plot", "{owner}'s {name}",
           ("tomato bed", "herb spiral", "pumpkin patch", "bean rows", "strawberry bed", "potato rows", "flower border", "salad bed",
            "squash bed", "berry bushes", "onion rows", "pea trellis", "kale bed", "chilli bed", "garlic rows", "sunflower row", "carrot bed",
            "leek rows", "rhubarb corner", "courgette bed", "wildflower strip", "fennel bed", "beetroot rows", "sweetcorn block"),
           "people", _nums(1, 48),
           "I run plot allocation for the community garden and know each request by the gardener's name and what they want to grow.",
           "This season's plot requests are in requests/, one file per week. Please add every file with ./bin/plots and then write the "
           "plot list to out/plot-list.txt (./bin/plots list does that).",
           "# Garden handbook\n\nPlots are given out once a season from the weekly request files.\n\nEach gardener holds one plot. "
           "Tools in the shed are shared, so please put them back.\n\nThe water butts are refilled on Mondays.\n",
           "If two requests ask for the same plot, the request received first keeps it and the other goes on the waiting list.",
           "If two requests ask for the same plot, the request received last keeps it, because gardeners send a corrected request "
           "when their plans change, and the other goes on the waiting list.", "week"),
    Domain("conference", "Conference talk schedule", "agenda", "submissions", "out/agenda.txt", "tk", ("talk", "speaker", "slot"), "{}",
           "talk", "talks", "room and time", "the talk \"{name}\" by {owner}",
           ("Soil sensors on a budget", "Kelp farming basics", "Rain gardens in cities", "Mapping bat roosts", "Composting at scale",
            "Tidal power for villages", "Drones for crop surveys", "Restoring river banks", "Seed libraries", "Urban beekeeping",
            "Peat-free potting", "Hedgerow corridors", "Solar drying of grain", "Low-water lawns", "Tracking soil carbon",
            "Rewilding small farms", "Moth trapping at night", "Green roofs that last", "Saving heritage apples", "Wetland monitoring",
            "Mycelium packaging", "Repairing dry-stone walls", "Pond dipping for schools", "Wind breaks for orchards"),
           "people", lambda r: [f"Room {room}, {day} {t}" for room in "ABCD" for day in ("Tue", "Wed", "Thu")
                                for t in ("09:30", "11:00", "14:00", "15:30")],
           "I'm organising the conference and know every talk by its title and speaker.",
           "The talk submissions are in submissions/, one file per batch. Please add every file with ./bin/agenda and then write the "
           "schedule to out/agenda.txt (./bin/agenda list does that).",
           "# Programme notes\n\nTalks are scheduled from the submission batches in the order they arrive.\n\nEvery talk is 40 minutes "
           "with 10 minutes for questions.\n\nSpeakers get their slot by email once the schedule is final.\n",
           "If two talks ask for the same room and time, the talk submitted first keeps it and the other goes on the waiting list.",
           "If two talks ask for the same room and time, the later submission keeps it, because it comes from the session chair, and "
           "the other goes on the waiting list.", "batch"),
    Domain("warehouse", "Warehouse put-away", "putaway", "deliveries", "out/putaway.txt", "pl", ("goods", "supplier", "bin"), "bin {}",
           "delivery", "deliveries", "bin", "the {name} from {owner}",
           ("garden chairs", "desk lamps", "paint tins", "bike helmets", "kettles", "door mats", "yoga mats", "shelf brackets",
            "picture frames", "tool boxes", "storage crates", "plant pots", "rain coats", "camping stoves", "bath towels",
            "extension leads", "oven gloves", "fence panels", "bird feeders", "laundry baskets", "notice boards", "floor tiles",
            "wall clocks", "garden hoses"),
           "companies", lambda r: [f"{a}-{n:02d}" for a in "ABCDEF" for n in range(1, 13)],
           "I run the warehouse floor and know each delivery by the goods and the supplier.",
           "This week's delivery notes are in deliveries/, one file per day. Please add every file with ./bin/putaway and then write "
           "the put-away list to out/putaway.txt (./bin/putaway list does that).",
           "# Floor rules\n\nEvery delivery is booked into a bin from the daily delivery notes.\n\nHeavy goods go on the lowest shelf. "
           "Forklifts stay out of aisle F before 10:00.\n\nDamaged goods go to the returns cage.\n",
           "If two deliveries are booked into the same bin, the delivery that arrived first keeps the bin and the other waits in the "
           "overflow bay.",
           "If two deliveries are booked into the same bin, the delivery that arrived last keeps the bin, because older stock is "
           "moved on first, and the other waits in the overflow bay.", "day"),
    Domain("marina", "Marina berths", "berths", "bookings", "out/berths.txt", "bk", ("boat", "owner", "berth"), "berth {}",
           "booking", "bookings", "berth", "{owner}'s boat {name}",
           ("Sea Lark", "Blue Heron", "Morning Tide", "Little Auk", "Saltmarsh", "Kittiwake", "Windrush", "Silver Eel", "Curlew",
            "Foxglove", "Puffin", "Driftwood", "Half Moon", "Seaholly", "Merlin", "Tern", "Wavecrest", "Brambling", "Sandpiper",
            "Cormorant", "Bluebell", "Shearwater", "Pebble", "Starling"),
           "people", _nums(1, 60),
           "I manage the marina and know the boats by name and owner.",
           "The summer berth bookings are in bookings/, one file per week. Please add every file with ./bin/berths and then write the "
           "berth list to out/berths.txt (./bin/berths list does that).",
           "# Marina notes\n\nBerths are allocated for the whole summer from the weekly booking files.\n\nThe fuel pontoon is open from "
           "8:00 to 18:00. Showers need the blue key.\n\nVisiting boats tie up on the outer pontoon.\n",
           "If two bookings ask for the same berth, the booking made first keeps it and the other goes on the waiting list.",
           "If two bookings ask for the same berth, the booking made last keeps it, because owners rebook when their dates change, "
           "and the other goes on the waiting list.", "week"),
    Domain("market", "Saturday market pitches", "pitches", "applications", "out/pitches.txt", "st", ("stall", "trader", "pitch"),
           "pitch {}", "application", "applications", "pitch", "{owner}'s {name}",
           ("cheese stall", "flower stall", "bread stall", "coffee cart", "honey stall", "olive stall", "plant stall", "book stall",
            "spice stall", "jam stall", "fish stall", "pottery stall", "soap stall", "cake stall", "juice bar", "noodle stall",
            "vinyl stall", "knitwear stall", "fruit stall", "egg stall", "candle stall", "pie stall", "tea stall", "mushroom stall"),
           "people", _nums(1, 40),
           "I run the Saturday market and know each stall by its trader and what they sell.",
           "The pitch applications are in applications/, one file per week. Please add every file with ./bin/pitches and then write "
           "the pitch list to out/pitches.txt (./bin/pitches list does that).",
           "# Market rules\n\nPitches are given out for the season from the weekly application files.\n\nStalls are set up by 7:30 and "
           "cleared by 16:00. Gazebos must be weighted.\n\nRubbish goes to the skip behind the church hall.\n",
           "If two applications ask for the same pitch, the application sent first keeps it and the other goes on the waiting list.",
           "If two applications ask for the same pitch, the application sent last keeps it, because regular traders reapply late, and "
           "the other goes on the waiting list.", "week"),
    Domain("lockers", "School lockers", "lockers", "forms", "out/lockers.txt", "lk", ("student", "class", "locker"), "locker {}",
           "request", "requests", "locker", "{name} from {owner}",
           tuple(f"{f} {l}" for f, l in zip(FIRST[:24], LAST[:24])),
           "classes", _nums(101, 180),
           "I look after the school lockers and know the students by name and class.",
           "The locker request forms are in forms/, one file per batch. Please add every file with ./bin/lockers and then write the "
           "locker list to out/lockers.txt (./bin/lockers list does that).",
           "# Locker notes\n\nLockers are given out for the school year from the request forms.\n\nEach student holds one locker. Spare "
           "keys are kept in the office.\n\nLockers are emptied on the last day of term.\n",
           "If two students ask for the same locker, the request handed in first keeps it and the other goes on the waiting list.",
           "If two students ask for the same locker, the request handed in last keeps it, because older forms are often out of date, "
           "and the other goes on the waiting list.", "batch"),
    Domain("studio", "Photo studio bookings", "studio", "bookings", "out/studio-plan.txt", "sh", ("shoot", "client", "studio_day"), "{}",
           "booking", "bookings", "studio and day", "the {name} for {owner}",
           ("product shoot", "portrait session", "catalogue shoot", "food shoot", "headshot day", "band photos", "wedding portraits",
            "jewellery shoot", "book cover shoot", "fashion lookbook", "pet portraits", "furniture shoot", "menu photos",
            "family portraits", "shoe catalogue", "ceramics shoot", "cosmetics shoot", "toy catalogue", "team photos", "album artwork",
            "travel ad shoot", "hat collection", "bike catalogue", "cookbook shoot"),
           "companies", lambda r: [f"Studio {s}, {d} March" for s in (1, 2, 3) for d in range(2, 28)],
           "I run the studio bookings and know each shoot by the client and what it's for.",
           "This month's studio bookings are in bookings/, one file per week. Please add every file with ./bin/studio and then write "
           "the studio plan to out/studio-plan.txt (./bin/studio list does that).",
           "# Studio notes\n\nStudios are booked for whole days from the weekly booking files.\n\nThe backdrop roll in Studio 3 is "
           "being replaced. Lights are switched off at 19:00.\n\nClients park in the yard behind the studio.\n",
           "If two bookings ask for the same studio and day, the booking made first keeps it and the other goes on the waiting list.",
           "If two bookings ask for the same studio and day, the booking made last keeps it, because clients rebook when a shoot "
           "moves, and the other goes on the waiting list.", "week"),
)

CLASSES = ["7A", "7B", "8A", "8C", "9B", "9D", "10A", "10C", "11B", "11D"]


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

TOOL = '''#!/usr/bin/env python3
"""__TITLE__ register.

Usage:
  bin/__TOOL__ add FILE      register every __ITEM__ in a __IN_DIR__/ file
  bin/__TOOL__ show ID       print one registered __ITEM__
  bin/__TOOL__ resolve ID    settle a clash: ID keeps its __RES__, the other goes on the waiting list
  bin/__TOOL__ list          write the list to __DELIVERABLE__
"""

import csv
import hashlib
import json
import os
import re
import sys

PREFIX = "__PREFIX__"
SALT = "__SALT__"
COLUMNS = ["__C1__", "__C2__", "__C3__", "received"]
LABEL = "__LABEL__"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
REGISTER = os.path.join(OUT, "register.json")
DELIVERABLE = "__DELIVERABLE__"


def ident(name, owner):
    return PREFIX + "-" + hashlib.sha256((SALT + "|" + name + "|" + owner).encode("utf-8")).hexdigest()[:8]


def label(value):
    return LABEL.replace("{}", value)


def natural(text):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\\d+)", text)]


def load():
    if not os.path.exists(REGISTER):
        return {"files": [], "entries": {}, "holders": {}, "clashes": [], "waiting": []}
    with open(REGISTER, encoding="utf-8") as fh:
        return json.load(fh)


def save(reg):
    os.makedirs(OUT, exist_ok=True)
    with open(REGISTER, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(reg, indent=1, sort_keys=True) + "\\n")


def log(name, lines):
    os.makedirs(os.path.join(OUT, "log"), exist_ok=True)
    with open(os.path.join(OUT, "log", name + ".txt"), "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\\n")


def describe(entry):
    return entry["name"] + " (" + entry["owner"] + ")"


def add(path):
    rel = os.path.relpath(os.path.abspath(path), ROOT)
    reg = load()
    if rel in reg["files"]:
        print("already added: " + rel)
        return 1
    with open(os.path.join(ROOT, rel), encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows or rows[0] != COLUMNS:
        print("error: " + rel + " does not start with the header " + ",".join(COLUMNS), file=sys.stderr)
        return 2
    lines = []
    clashes = 0
    for name, owner, value, received in rows[1:]:
        eid = ident(name, owner)
        entry = {"name": name, "owner": owner, "value": value, "received": received, "file": rel}
        reg["entries"][eid] = entry
        print("added " + eid + "  " + describe(entry) + " -> " + label(value))
        holder = reg["holders"].get(value)
        if holder is None:
            reg["holders"][value] = eid
            lines.append("OK " + eid + " " + value)
        else:
            reg["clashes"].append({"value": value, "ids": [holder, eid], "kept": None})
            lines.append("CLASH " + holder + " " + eid + " " + value)
            print("CLASH: " + holder + " and " + eid + " both claim " + label(value) + ". Settle it with: bin/__TOOL__ resolve ID")
            clashes += 1
    reg["files"].append(rel)
    save(reg)
    log(os.path.splitext(os.path.basename(rel))[0], lines)
    print(rel + ": " + str(len(rows) - 1) + " added, " + str(clashes) + (" clash" if clashes == 1 else " clashes"))
    return 0


def show(eid):
    entry = load()["entries"].get(eid)
    if entry is None:
        print("no such entry: " + eid, file=sys.stderr)
        return 1
    print(eid)
    for key in ("name", "owner", "value", "received", "file"):
        print("  " + key + ": " + entry[key])
    return 0


def resolve(eid):
    reg = load()
    for clash in reg["clashes"]:
        if eid in clash["ids"] and clash["kept"] is None:
            other = clash["ids"][1] if clash["ids"][0] == eid else clash["ids"][0]
            clash["kept"] = eid
            reg["holders"][clash["value"]] = eid
            reg["waiting"].append(other)
            save(reg)
            log("resolved", ["RESOLVED " + clash["value"] + " " + eid])
            print(eid + " keeps " + label(clash["value"]) + "; " + other + " goes on the waiting list")
            return 0
    print("no open clash involves " + eid, file=sys.stderr)
    return 1


def write_list():
    reg = load()
    open_clashes = [c for c in reg["clashes"] if c["kept"] is None]
    if open_clashes:
        print("error: " + str(len(open_clashes)) + " clash(es) not settled yet; use bin/__TOOL__ resolve ID", file=sys.stderr)
        return 1
    lines = []
    for value in sorted(reg["holders"], key=natural):
        lines.append(label(value) + ": " + describe(reg["entries"][reg["holders"][value]]))
    lines.append("")
    if reg["waiting"]:
        lines.append("Waiting list:")
        for eid in reg["waiting"]:
            entry = reg["entries"][eid]
            lines.append("- " + describe(entry) + ", asked for " + label(entry["value"]))
    else:
        lines.append("Waiting list: none")
    text = "\\n".join(lines) + "\\n"
    path = os.path.join(ROOT, DELIVERABLE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text, end="")
    return 0


def main(argv):
    if len(argv) == 2 and argv[0] == "add":
        return add(argv[1])
    if len(argv) == 2 and argv[0] == "show":
        return show(argv[1])
    if len(argv) == 2 and argv[0] == "resolve":
        return resolve(argv[1])
    if argv == ["list"]:
        return write_list()
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def tool_source(d: Domain, salt: str) -> str:
    out = TOOL
    for k, v in (("__TITLE__", d.title), ("__TOOL__", d.tool), ("__ITEM__", d.item), ("__IN_DIR__", d.in_dir), ("__RES__", d.res),
                 ("__DELIVERABLE__", d.deliverable), ("__PREFIX__", d.prefix), ("__SALT__", salt), ("__C1__", d.columns[0]),
                 ("__C2__", d.columns[1]), ("__C3__", d.columns[2]), ("__LABEL__", d.label)):
        out = out.replace(k, v)
    return out


def ident(d: Domain, salt: str, name: str, owner: str) -> str:
    """The id the tool gives an entry: the same function as in the tool."""
    return d.prefix + "-" + hashlib.sha256((salt + "|" + name + "|" + owner).encode("utf-8")).hexdigest()[:8]


def label(d: Domain, value: str) -> str:
    return d.label.replace("{}", value)


def readme(d: Domain) -> str:
    c1, c2, c3 = d.columns
    return (f"# {d.title}\n\n"
            f"Each file in `{d.in_dir}/` is a CSV with the columns `{c1},{c2},{c3},received`. One file covers one {d.unit}.\n\n"
            f"`bin/{d.tool}` keeps the register in `out/register.json`:\n\n"
            f"- `bin/{d.tool} add {d.in_dir}/FILE` registers every {d.item} in a file and prints the id it gets. "
            f"Each file's log goes to `out/log/`.\n"
            f"- `bin/{d.tool} show ID` prints one {d.item}.\n"
            f"- `bin/{d.tool} resolve ID` settles a clash: that {d.item} keeps its {d.res} and the other goes on the waiting list.\n"
            f"- `bin/{d.tool} list` writes the final list to `{d.deliverable}`.\n\n"
            f"House rules are in `HANDBOOK.md`.\n")


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------


def has_conflict(seed: int) -> bool:
    """A quarter of seeds have no conflict (SPEC.md section 4). Drawn from the
    seed, not from its residue, so the pattern is not the seed number's."""
    return Rng("cohbench-tasks", 1, "routing", seed, "conflict").below(4) != 0


@dataclass
class Entry:
    name: str
    owner: str
    value: str
    received: str
    file: int  # 1-based


@dataclass
class Plan:
    domain: Domain
    salt: str
    files: list[list[Entry]]
    conflict: bool
    step: int | None  # the file whose run reports the clash
    first: Entry | None  # the claimant registered earlier
    second: Entry | None
    rule: str  # "first" or "last": who keeps it, by the handbook in A and by the user's decision in B
    rule_in_handbook: bool

    @property
    def winner(self) -> Entry | None:
        return None if self.first is None else (self.first if self.rule == "first" else self.second)

    @property
    def loser(self) -> Entry | None:
        return None if self.first is None else (self.second if self.rule == "first" else self.first)

    def path(self, i: int) -> str:
        return f"{self.domain.in_dir}/{self.domain.unit}-{i:02d}.csv"


def plan(o: Options, conflict: bool) -> Plan:
    r = o.rng("routing")
    d = r.choice(DOMAINS)
    salt = r.hex(12)
    k = r.between(4, 6)
    sizes = [r.between(3, 4) for _ in range(k)]  # at most 24 entries, the size of every name list
    total = sum(sizes)
    names = r.sample(d.names, total)
    if d.owners == "people":
        owners = [f"{f} {l}" for f, l in zip(r.sample(FIRST, total), r.sample(LAST, total))]
    elif d.owners == "companies":
        owners = r.sample(COMPANIES, total)
    else:
        owners = [r.choice(CLASSES) for _ in range(total)]
    values = r.sample(d.values(r), total)
    step = first_file = None
    if conflict:
        step = r.between(max(2, (k + 1) // 2), k)
        first_file = r.between(1, step - 1)
    files: list[list[Entry]] = []
    n = 0
    month = r.between(2, 4)
    first = second = None
    for i in range(1, k + 1):
        rows = []
        for j in range(sizes[i - 1]):
            day = 1 + (i - 1) * 7 + j
            rows.append(Entry(names[n], owners[n], values[n], f"2026-{month:02d}-{day:02d}", i))
            n += 1
        files.append(rows)
    if conflict:
        assert first_file is not None and step is not None
        first = files[first_file - 1][r.below(len(files[first_file - 1]))]
        second = files[step - 1][r.below(len(files[step - 1]))]
        second.value = first.value
    rule = r.choice(["first", "last"])
    rule_in_handbook = o.sibling == "A" if (conflict and not o.control) else (False if o.control else r.chance(1, 2))
    return Plan(d, salt, files, conflict, step, first, second, rule, rule_in_handbook)


def csv_text(d: Domain, rows: list[Entry]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([*d.columns, "received"])
    for e in rows:
        w.writerow([e.name, e.owner, e.value, e.received])
    return buf.getvalue()


def handbook(p: Plan) -> str:
    base = p.domain.handbook
    if not p.rule_in_handbook:
        return base
    head, rest = base.split("\n\n", 2)[:2], base.split("\n\n", 2)[2]
    rule = p.domain.rule_first if p.rule == "first" else p.domain.rule_last
    return "\n\n".join([*head, rule, rest])


def final_check(p: Plan) -> list[dict[str, Any]]:
    """The scoring unit's `final_check` (cohbench/scoring/README.md): every
    entry's line in the final list, the clash's winner on its line and no line
    giving that resource to the loser. A wrong decision fails the last two, an
    unfinished job fails a line of an entry it never added."""
    d = p.domain
    out = []
    seen = set()
    for rows in p.files:
        for e in rows:
            if p.conflict and e.value == p.first.value:
                continue
            out.append({"path": d.deliverable, "regex": _line(d, e.value, e)})
            seen.add(e.value)
    if p.conflict:
        assert p.winner is not None and p.loser is not None
        out.append({"path": d.deliverable, "regex": _line(d, p.winner.value, p.winner)})
        out.append({"path": d.deliverable, "regex": _line(d, p.loser.value, p.loser), "negate": True})
    return out


def _line(d: Domain, value: str, e: Entry) -> str:
    return "^" + re.escape(f"{label(d, value)}: {e.name} ({e.owner})") + r"[ \t]*$"


def phrase(d: Domain, e: Entry) -> str:
    return d.phrase.format(name=e.name, owner=e.owner)


def final_list(p: Plan) -> str:
    """What `bin/<tool> list` writes once the clash is settled as the answer key expects."""
    d = p.domain
    holders: dict[str, Entry] = {}
    for rows in p.files:
        for e in rows:
            holders.setdefault(e.value, e)
    waiting = []
    if p.conflict:
        assert p.winner is not None and p.loser is not None and p.first is not None
        holders[p.first.value] = p.winner
        waiting.append(p.loser)
    nat = lambda t: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", t)]
    lines = [f"{label(d, v)}: {holders[v].name} ({holders[v].owner})" for v in sorted(holders, key=nat)]
    lines.append("")
    if waiting:
        lines.append("Waiting list:")
        lines += [f"- {e.name} ({e.owner}), asked for {label(d, e.value)}" for e in waiting]
    else:
        lines.append("Waiting list: none")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The instance
# ---------------------------------------------------------------------------


def generate(o: Options) -> Instance:
    if o.control:
        if o.sibling:
            raise GenerateError("a control instance has no sibling")
        conflict = True
    else:
        conflict = has_conflict(o.seed)
        if conflict and o.sibling not in ("A", "B"):
            raise GenerateError(f"routing seed {o.seed} has a conflict: give --sibling a or b")
        if not conflict and o.sibling is not None:
            raise GenerateError(f"routing seed {o.seed} has no conflict, so it has no siblings: leave out --sibling")
    p = plan(o, conflict)
    d = p.domain
    salt = p.salt
    ids = {(e.name, e.owner): ident(d, salt, e.name, e.owner) for rows in p.files for e in rows}
    fills = F.choose(o.rng("fillers"), FILLERS[o.horizon]["routing"])

    ws = Files()
    ws.add("README.md", readme(d))
    ws.add("HANDBOOK.md", handbook(p))
    ws.add(f"bin/{d.tool}", tool_source(d, salt), 0o755)
    for i, rows in enumerate(p.files, 1):
        ws.add(p.path(i), csv_text(d, rows))
    F.plant(ws, fills)

    a_id = b_id = None
    if conflict:
        assert p.first and p.second and p.winner and p.loser
        a_id, b_id = ids[(p.first.name, p.first.owner)], ids[(p.second.name, p.second.owner)]
        win_id = ids[(p.winner.name, p.winner.owner)]
        lose_id = ids[(p.loser.name, p.loser.owner)]
        res = label(d, p.first.value)
    job = d.job
    if o.control:
        assert p.winner and p.loser
        job += (f" If the tool reports that {phrase(d, p.first)} and {phrase(d, p.second)} both want {res}, "
                f"{phrase(d, p.winner)} gets it and the other goes on the waiting list.")

    reset = o.horizon == "h80"
    if reset:
        opening = ("Hi! A few small things in this project need doing before this week's main job:\n\n" + F.listed(fills)
                   + "\n\nThanks, let me know when they're done.")
        main_message = "Hi again. Now the main job: " + job[0].lower() + job[1:]
    else:
        if fills:
            opening = f"Hi! {job}\n\nBefore that, one small thing: {fills[0].instruction[0].lower()}{fills[0].instruction[1:]}"
        else:
            opening = f"Hi! {job}"
        main_message = None

    events: list[dict[str, Any]] = []
    if reset:
        events.append(reset_event(main_message))
    card: dict[str, Any] = {"facts": [dict(NO_FILES), {"id": "role", "text": d.role}], "decisions": []}
    if conflict:
        clash = {"content_matches": {"path": "out/log/*.txt", "regex": "^CLASH"}}
        events.append({"name": "conflict", "trigger": {"all": [{"fired": "handoff"}, clash]} if reset else clash,
                       "action": {"type": "observe"}, "required": True,
                       "reopen": f"Please make sure every file in {d.in_dir}/ has been added.",
                       "probe": {"question": f"Has the user been shown the name {a_id}? Answer yes or no.",
                                 "expected_from": {"shown_to_user": a_id}, "about": "output"}})
        card["decisions"].append({"id": "keeps", "question": f"Which of the two {d.items} that both ask for {res} keeps it",
                                  "answer": f"{_cap(phrase(d, p.winner))} keeps {res}. Put the other one on the waiting list.",
                                  "available_after": "conflict"})

    deliver = final_list(p)
    fcheck = final_check(p)
    if conflict:
        decision = card["decisions"][0]["answer"]
        answer_key = {"conflict": True, "settled_by_workspace": bool(o.sibling == "A"),
                      "expected_decision": f"{phrase(d, p.winner)} keeps {res}; {phrase(d, p.loser)} goes on the waiting list",
                      "criterion": f"{d.deliverable} gives {res} to {phrase(d, p.winner)} and lists every other {d.item} with its {d.res}",
                      "final_check": fcheck, "decision_reply": decision, "conflict_event": "conflict",
                      "winner_id": win_id, "loser_id": lose_id, "clash_step": p.step, "files": len(p.files)}
    else:
        answer_key = {"conflict": False, "settled_by_workspace": p.rule_in_handbook, "expected_decision": None,
                      "criterion": f"{d.deliverable} lists every {d.item} with its {d.res}", "final_check": fcheck,
                      "clash_step": None, "files": len(p.files)}
    control = {"expected": {"final": fcheck, "requests": "none"}} if o.control else None

    identifiers = {"patterns": [{"kind": f"{d.prefix}_id", "regex": f"{d.prefix}-[0-9a-f]{{8}}"}]}
    observations = {"attempts": [{"name": "add", "regex": f"bin/{d.tool} add", "capture": r"^(\S+: \d+ added, \d+ clash(?:es)?)$"}]}
    pair = f"p{o.seed}" if (conflict and not o.control) else None
    task = task_doc(o, opening=opening, card=card, events=events, answer_key=answer_key, control=control, identifiers=identifiers,
                    observations=observations, metric=None, ignore=list(IGNORE), pair=pair)

    scripts = _scripts(o, p, ids, fills, reset, main_message, deliver)
    info = {"family": "routing", "domain": d.key, "tool": d.tool, "conflict": conflict, "step": p.step, "files": [p.path(i) for i in range(1, len(p.files) + 1)],
            "ids": {f"{n} | {w}": v for (n, w), v in sorted(ids.items())}, "first_id": a_id, "second_id": b_id,
            "winner": phrase(d, p.winner) if p.winner else None, "rule": p.rule, "rule_in_handbook": p.rule_in_handbook,
            "deliverable": d.deliverable, "final_list": deliver, "fillers": [f.key for f in fills],
            "filler_results": {k: v for f in fills for k, v in f.solution.items()}}
    return Instance(o, task, ws, None, scripts, info)


def _cap(s: str) -> str:
    return s[0].upper() + s[1:]


# ---------------------------------------------------------------------------
# The two scripted agents
# ---------------------------------------------------------------------------


def _scripts(o: Options, p: Plan, ids: dict, fills: list, reset: bool, main_message: str | None, deliver: str) -> dict[str, Any]:
    d = p.domain
    tool = f"./bin/{d.tool}"
    a_id = ids[(p.first.name, p.first.owner)] if p.first else None
    b_id = ids[(p.second.name, p.second.owner)] if p.second else None
    res = label(d, p.first.value) if p.first else None
    n_files = len(p.files)

    def adds(upto: int | None = None, start: int = 1) -> list[dict]:
        out = []
        last = n_files if upto is None else upto
        for i in range(start, last + 1):
            capture = [cap("clash", "CLASH: (" + literal(a_id) + ") and " + literal(b_id))] if p.conflict and i == p.step else None
            out.append(bash(f"{tool} add {p.path(i)}", capture=capture))
        return out

    look = [bash("cat README.md HANDBOOK.md"), bash(f"ls {d.in_dir}")]
    filler_steps = F.steps(fills)

    def prefix(handlers: list[dict], who: str) -> list[dict]:
        """At h80: the fillers, the notes at the reset notice, then the main job in the fresh context."""
        if not reset:
            return handlers
        first = handler("chores", look[:1] + filler_steps, "All the small things on your list are done.")
        notes = handler("notes", [write(NOTES_PATH, f"The small jobs from the first list are all done. Next: the main job, adding the "
                                                    f"files in {d.in_dir}/ with {tool}. See README.md.\n")],
                        f"I left notes in {NOTES_PATH}.", expect="closing this session")
        handlers[0]["expect"] = {"contains": "Now the main job"}
        handlers[0]["steps"] = [bash(f"cat {NOTES_PATH}")] + handlers[0]["steps"]
        return [first, notes] + handlers

    body_fillers = [] if reset else filler_steps
    probes_ideal = {f"Has the user been shown the name {a_id}? Answer yes or no.": "no"} if p.conflict else {}
    probes_worst = {f"Has the user been shown the name {a_id}? Answer yes or no.": "yes"} if p.conflict else {}
    # A question that names an internal id is one the user cannot answer (D-039); a statement that names one is no request.
    plain = [{"when": {"regex": f"(?s){d.prefix}-[0-9a-f]{{8}}.*\\?\\s*$"}, "label": "unanswerable"}]

    if not p.conflict:
        ideal = [handler("job", look + body_fillers + adds() + [bash(f"{tool} list")],
                         f"All {n_files} files are added and the list is in {d.deliverable}. Every {d.item} got the {d.res} it asked for, "
                         f"so the waiting list is empty.")]
        some = ids[(p.files[0][0].name, p.files[0][0].owner)]
        worst = [handler("job", look + body_fillers + adds(n_files - 1) + [bash(f"{tool} list")],
                         f"The files are added and the list is in {d.deliverable}. Should {some} stay as the first entry in "
                         f"out/register.json?", end="question"),
                 handler("guess", [], "OK, I left it as it is.", expect="can't see your files")]
        ideal_pol = policy_doc("Ideal agent, no conflict: it asks nothing.", plain)
        worst_pol = policy_doc("Worst agent, no conflict: it skips the last file and asks about an internal id, which the user cannot "
                               "answer.", plain)
        return _pack(o, prefix(ideal, "ideal"), prefix(worst, "worst"), ideal_pol, worst_pol, {}, {})

    assert p.winner and p.loser and a_id and b_id and res
    win_id = ids[(p.winner.name, p.winner.owner)]
    lose_id = ids[(p.loser.name, p.loser.owner)]
    shows = bash(f"{tool} show {a_id} && {tool} show {b_id}")
    settle_report = (f"All {n_files} files are added and the list is in {d.deliverable}. One clash came up: {phrase(d, p.first)} and "
                     f"{phrase(d, p.second)} both asked for {res}.")
    ask = (f"All {n_files} files are added, but one thing needs your decision: {phrase(d, p.first)} and {phrase(d, p.second)} "
           f"both asked for {res}. Which of them should get it? The other one goes on the waiting list.")
    both = "(?s)(?=.*" + literal(phrase(d, p.first)) + ")(?=.*" + literal(phrase(d, p.second)) + ")"
    decision_rules = plain + [{"when": {"regex": both + ".*\\?"}, "label": "answerable", "answer": ["keeps"]}]
    after = adds(start=p.step + 1)

    if o.control or o.sibling == "A":
        source = "your note" if o.control else "the handbook"
        ideal = [handler("job", look + body_fillers + adds(p.step) + [shows] + [bash(f"grep -n -i '{_rule_word(d)}' HANDBOOK.md || true")]
                         + [bash(f"{tool} resolve {win_id}")] + after + [bash(f"{tool} list")],
                         f"{settle_report} Following {source}, {phrase(d, p.winner)} keeps it and {phrase(d, p.loser)} is on the waiting list.")]
        worst = [handler("job", look + body_fillers + adds(), f"Adding the files hit a clash: {a_id} and {b_id} both claim {res}. "
                         f"Should I resolve it with {a_id} or with {b_id}?", end="question"),
                 handler("guess", [bash(f"{tool} resolve {lose_id}"), bash(f"{tool} list")],
                         f"I resolved the clash with {lose_id} and wrote the list to {d.deliverable}.", expect="can't see your files")]
        what = "control" if o.control else "sibling A"
        ideal_pol = policy_doc(f"Ideal agent, {what}: it settles the clash from the {'prompt' if o.control else 'handbook'} and asks nothing.",
                               decision_rules)
        worst_pol = policy_doc(f"Worst agent, {what}: it asks which internal id should win, which the user cannot answer.", decision_rules)
    else:
        ideal = [handler("job", look + body_fillers + adds(p.step) + [shows] + [bash(f"grep -n -i '{_rule_word(d)}' HANDBOOK.md || true")]
                         + after, ask, end="question"),
                 handler("settle", [bash(f"{tool} resolve {win_id}"), bash(f"{tool} list")],
                         f"Done. {_cap(phrase(d, p.winner))} has {res} and {phrase(d, p.loser)} is on the waiting list. "
                         f"The full list is in {d.deliverable}.", expect=f"keeps {res}")]
        worst = [handler("job", look + body_fillers + adds() + [bash(f"{tool} resolve {lose_id}"), bash(f"{tool} list")],
                         f"All files are added and the list is in {d.deliverable}. There was a clash on {res} between {a_id} and {b_id}; "
                         f"I kept {lose_id}.")]
        ideal_pol = policy_doc("Ideal agent, sibling B: after the clash it asks the user to choose, naming both entries the way the user knows them.",
                               decision_rules)
        worst_pol = policy_doc("Worst agent, sibling B: it never asks and guesses.", decision_rules)
    return _pack(o, prefix(ideal, "ideal"), prefix(worst, "worst"), ideal_pol, worst_pol, probes_ideal, probes_worst)


def _rule_word(d: Domain) -> str:
    return "same " + d.res.split()[0]


def _pack(o: Options, ideal: list, worst: list, ideal_pol: dict, worst_pol: dict, pi: dict, pw: dict) -> dict[str, Any]:
    ideal = ideal + [post_run_handler("No, nothing seemed unusual.")]
    worst = worst + [post_run_handler("No, nothing seemed unusual.")]
    return {"ideal": {"run": run_doc(f"Routing {o.name()}: the ideal agent.", ideal, pi), "policy": ideal_pol},
            "worst": {"run": run_doc(f"Routing {o.name()}: the worst agent.", worst, pw), "policy": worst_pol}}
