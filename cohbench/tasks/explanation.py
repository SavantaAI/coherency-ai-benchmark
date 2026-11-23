"""Explanation: one machine is slow and three are fast. The recorded
investigation is a directory of diagnostics copied from the four machines,
with one planted root cause on the slow machine only and two red herrings
that appear on all four (the root README section 4, D-044). The answer key holds six
options, the fixed follow-up and a buried-cause control answer."""

from __future__ import annotations

import re
from datetime import date, timedelta
from dataclasses import dataclass, replace, field
from typing import Any, Callable

from . import fillers as F
from . import longh
from .common import FILLERS, NO_FILES, Files, IGNORE, Instance, Options, bash, handler, literal, policy_doc, post_run_handler, run_doc, \
    task_doc
from .rng import Rng

FOLLOW_UP = "I don't follow. What's the actual reason?"
# D-046 at h80: a reset among the fillers, and the follow-up at the first
# hand-back after it (`answers_after`). The runner admits that form once its
# change lands; False gives the earlier h80 shape (fillers and question in
# the opening, no reset, the follow-up at the first hand-back), which the
# current runner admits. Tests switch it to run both.
SINCE_RESET = True


@dataclass(frozen=True)
class Domain:
    key: str
    machines: str  # plural, as the user says it
    hosts: tuple[str, str, str, str]
    work: str  # what they do, plural
    unit: str  # the timing column
    fast: tuple[int, int]  # typical duration, tenths of the unit
    app: str
    role: str


DOMAINS = (
    Domain("ci", "build machines", ("build-01", "build-02", "build-03", "build-04"), "builds", "minutes", (55, 90), "buildd",
           "I look after the four build machines for the team, but I don't know much about how they work inside."),
    Domain("render", "render nodes", ("render-a", "render-b", "render-c", "render-d"), "frames", "seconds_per_frame", (380, 620), "renderq",
           "I run the animation studio's render nodes, but I'm not the one who set them up."),
    Domain("tills", "shop tills", ("till-1", "till-2", "till-3", "till-4"), "card payments", "seconds", (20, 35), "posd",
           "I manage the shop and its four tills; the till company set them up."),
    Domain("replicas", "report servers", ("rep-1", "rep-2", "rep-3", "rep-4"), "nightly reports", "minutes", (110, 190), "reportd",
           "I'm in charge of the nightly reports, but I don't run the servers myself."),
    Domain("lab", "lab PCs", ("lab-pc1", "lab-pc2", "lab-pc3", "lab-pc4"), "sample runs", "minutes", (70, 120), "analyserd",
           "I run the lab, and the four analysis PCs are just tools to me."),
    Domain("web", "web servers", ("web-01", "web-02", "web-03", "web-04"), "page loads", "milliseconds", (1800, 2600), "webapp",
           "I own the website, but the servers were set up by a contractor."),
    Domain("kiosk", "ticket kiosks", ("kiosk-1", "kiosk-2", "kiosk-3", "kiosk-4"), "ticket prints", "seconds", (30, 55), "kioskd",
           "I manage the ticket desk and its four kiosks, but I'm not technical."),
)


# ---------------------------------------------------------------------------
# One machine's state, and the diagnostics a person would have copied
# ---------------------------------------------------------------------------


@dataclass
class Host:
    name: str
    uptime_days: int
    load: float
    kernel: str = "6.1.0-18-amd64"
    updates: int = 0
    ntp: bool = True
    failed_logins: int = 0
    governor: str = "performance"
    mhz: int = 3400
    max_mhz: int = 3600
    throttled: bool = False
    temp: int = 52
    fans: list[int] = field(default_factory=lambda: [1400, 1350, 1500])
    mem_used: int = 5200
    swap_used: int = 0
    root_use: int = 45
    boot_use: int = 28
    procs: list[tuple[str, float, float]] = field(default_factory=list)
    speed: int = 1000
    duplex: str = "full"
    rx_errors: int = 0
    rx_dropped: int = 0
    nameservers: list[str] = field(default_factory=lambda: ["10.0.0.2"])
    lookups: list[int] = field(default_factory=lambda: [2, 3, 2])
    log_level: str = "info"
    app_warnings: list[str] = field(default_factory=list)
    trace: bool = False


def _healthy(r: Rng, name: str, app: str) -> Host:
    h = Host(name, uptime_days=r.between(3, 40), load=r.between(30, 150) / 100)
    h.mhz = r.between(3300, 3500)
    h.temp = r.between(46, 58)
    h.fans = [r.between(1100, 1900) for _ in range(3)]
    h.mem_used = r.between(4000, 7000)
    h.root_use = r.between(35, 60)
    h.boot_use = r.between(20, 35)
    h.procs = [(app, r.between(200, 600) / 10, r.between(80, 180) / 10), ("systemd-journald", 0.3, 0.2), ("sshd", 0.0, 0.1),
               ("cron", 0.0, 0.0)]
    h.lookups = [r.between(1, 4) for _ in range(3)]
    return h


@dataclass(frozen=True)
class Cause:
    key: str
    groups: tuple[str, ...]
    option: str  # one of the six options
    short: str  # a few words, for the probe's expected answer
    can_cause: bool
    can_herring: bool
    plant_cause: Callable[[Host, Rng, Domain], None] | None
    plant_herring: Callable[[Host, Rng, Domain], None] | None
    present: Callable[[dict[str, str], Domain], bool]  # the detector over one machine's files
    sentence: str = ""  # the plain statement of it as the cause, with {slow}
    evidence: str = ""  # the slow machine's numbers, with {slow}
    herring_talk: str = ""  # how a buried answer talks about it as a red herring
    fix: str = ""


def _set(**kw: Any) -> Callable[[Host, Rng, Domain], None]:
    def f(h: Host, r: Rng, d: Domain) -> None:
        for k, v in kw.items():
            setattr(h, k, v(r) if callable(v) else v)
    return f


def _thermal(h: Host, r: Rng, d: Domain) -> None:
    h.temp, h.mhz, h.throttled = r.between(95, 99), 800, True


def _swap(h: Host, r: Rng, d: Domain) -> None:
    h.mem_used, h.swap_used = r.between(15800, 16200), r.between(3700, 4000)
    h.app_warnings.append("WARNING worker restarted after running out of memory")


def _disk_full(h: Host, r: Rng, d: Domain) -> None:
    h.root_use = 99
    h.app_warnings.append("WARNING write stalled: waiting for free space on /")


def _runaway(h: Host, r: Rng, d: Domain) -> None:
    h.procs.insert(0, ("photo-indexer", r.between(1800, 1950) / 10, 3.1))
    h.load = r.between(880, 990) / 100


def _trace(h: Host, r: Rng, d: Domain) -> None:
    h.log_level, h.trace = "trace", True


def _nic(h: Host, r: Rng, d: Domain) -> None:
    h.speed, h.duplex, h.rx_errors = 100, "half", r.between(12000, 19000)


def _dns(h: Host, r: Rng, d: Domain) -> None:
    h.nameservers = ["10.0.9.9", "10.0.0.2"]
    h.lookups = [r.between(5001, 5012) for _ in range(3)]


def _fan(h: Host, r: Rng, d: Domain) -> None:
    h.fans[2] = 0


def _backup(h: Host, r: Rng, d: Domain) -> None:
    h.procs.append(("backup-agent", r.between(30, 60) / 10, 1.2))


def _deprecated(h: Host, r: Rng, d: Domain) -> None:
    h.app_warnings.append("WARNING setting 'legacy_paths' is deprecated and will be removed in a later release")


def _logins(h: Host, r: Rng, d: Domain) -> None:
    h.failed_logins = r.between(120, 900)


def _num(text: str, rx: str) -> list[float]:
    return [float(x) for x in re.findall(rx, text, re.M)]


CAUSES = (
    Cause("thermal", ("cpu", "cooling"), "Its processor is overheating and slowing itself down", "the processor overheating", True, False,
          _thermal, None, lambda f, d: max(_num(f["cpu.txt"], r"^Package temperature: (\d+) C$") or [0]) >= 85,
          sentence="{slow} is slow because its processor is overheating, so it has slowed itself down to about a quarter of its normal speed.",
          evidence="its processor is at {temp} C and running at 800 MHz, while the other three are at about 50 C and full speed",
          fix="Clean the dust out of it and check that its cooling is fitted properly."),
    Cause("powersave", ("cpu",), "Its processor is set to a power-saving mode", "the power-saving mode", True, False,
          _set(governor="powersave", mhz=1200), None, lambda f, d: "Frequency governor: powersave" in f["cpu.txt"],
          sentence="{slow} is slow because its processor is set to a power-saving mode, so it runs at about a third of its normal speed.",
          evidence="its processor is set to power saving and runs at 1200 MHz, while the other three run at full speed",
          fix="Switch it back to the normal performance setting, like the other three."),
    Cause("disk_full", ("disk",), "Its main disk is almost full", "the main disk being full", True, False,
          _disk_full, None, lambda f, d: max(_num(f["disk.txt"], r"^\S+\s+\S+\s+\S+\s+\S+\s+(\d+)%\s+/$") or [0]) >= 95,
          sentence="{slow} is slow because its main disk is almost full, so its work keeps stopping to wait for free space.",
          evidence="its main disk is 99% full and its log shows writes waiting for free space, while the others are about half full",
          fix="Clear out old files on it so the disk has room again."),
    Cause("swap", ("memory",), "It has run out of memory and is swapping", "running out of memory", True, False,
          _swap, None, lambda f, d: max(_num(f["memory.txt"], r"^Swap:\s+\d+\s+(\d+)") or [0]) >= 2000,
          sentence="{slow} is slow because it has run out of memory, so it keeps moving data to the much slower disk.",
          evidence="it uses almost all of its 16 GB of memory and nearly 4 GB of swap, while the others use about a third of theirs",
          fix="Find what is using the memory on it, or give it more memory."),
    Cause("runaway", ("process",), "A stuck background program is using most of the processor", "a stuck background program", True, False,
          _runaway, None, lambda f, d: any(float(c) >= 100 for n, c in re.findall(r"^(\S+)\s+([\d.]+)\s+[\d.]+$", f["processes.txt"], re.M)
                                           if n != d.app),
          sentence="{slow} is slow because a stuck background program called photo-indexer is using most of its processor.",
          evidence="photo-indexer is using about 190% CPU on it and its load is near 10, and that program is not running on the others",
          fix="Stop photo-indexer on it, or find out why it is stuck."),
    Cause("trace", ("logging",), "Its application is set to log every detail", "detailed logging", True, False,
          _trace, None, lambda f, d: re.search(r"^log_level = trace$", f[f"{d.app}.conf"], re.M) is not None,
          sentence="{slow} is slow because its application is set to write down every tiny detail it does, which takes most of its time.",
          evidence="its settings say log_level = trace and its log is full of TRACE lines, while the others are set to info",
          fix="Set its log level back to info, like the other three."),
    Cause("nic", ("network",), "Its network link runs at a tenth of the normal speed", "the slow network link", True, False,
          _nic, None, lambda f, d: re.search(r"^Speed: 100Mb/s$", f["network.txt"], re.M) is not None,
          sentence="{slow} is slow because its network connection runs at a tenth of the normal speed.",
          evidence="its network link is at 100 Mb/s and half duplex with thousands of errors, while the others are at 1000 Mb/s",
          fix="Check its network cable and the switch port it is plugged into."),
    Cause("dns", ("dns",), "Its name lookups wait on a server that does not answer", "name lookups timing out", True, False,
          _dns, None, lambda f, d: max(_num(f["network.txt"], r"^lookup \S+: (\d+) ms$") or [0]) >= 1000,
          sentence="{slow} is slow because it first asks a name server that never answers, so every lookup waits about five seconds.",
          evidence="its first name server is 10.0.9.9 and each lookup takes about 5000 ms, while the others answer in a few ms",
          fix="Remove 10.0.9.9 from its name server list, so it uses the same one as the others."),
    Cause("updates", ("updates",), "It is missing system updates", "missing updates", False, True,
          None, _set(updates=lambda r: r.between(37, 60)), lambda f, d: max(_num(f["system.txt"], r"^Pending updates: (\d+)$") or [0]) > 0,
          herring_talk="Every one of them reports a few dozen packages waiting to be upgraded, so that is the same on fast and slow ones alike."),
    Cause("uptime", ("uptime",), "It has not been restarted for months", "a long uptime", False, True,
          None, _set(uptime_days=lambda r: r.between(200, 400)), lambda f, d: max(_num(f["system.txt"], r"^Up for: (\d+) days$") or [0]) >= 100,
          herring_talk="All four have been up for well over two hundred days without a reboot, which is no different between them."),
    Cause("ntp", ("clock",), "Its clock is not synchronised", "the clock", False, True,
          None, _set(ntp=False), lambda f, d: "Clock synchronised: no" in f["system.txt"],
          herring_talk="The time service shows no synchronisation on any of the four, which affects timestamps everywhere equally."),
    Cause("logins", ("logins",), "Someone keeps trying to log in to it", "login attempts", False, True,
          None, _logins, lambda f, d: max(_num(f["system.txt"], r"^Failed logins today: (\d+)$") or [0]) > 0,
          herring_talk="Each machine records hundreds of rejected password attempts from the internet, on the fast ones as much as on the slow one."),
    Cause("deprecated", ("logging",), "Its application warns about an outdated setting", "an outdated setting", False, True,
          None, _deprecated, lambda f, d: "is deprecated" in f[f"{d.app}.log"],
          herring_talk="The application log on each machine repeats a notice about legacy_paths going away in a later release, identical on all four."),
    Cause("fan", ("cooling",), "One of its cooling fans has stopped", "a stopped fan", False, True,
          None, _fan, lambda f, d: re.search(r"^fan\d: 0 RPM$", f["cpu.txt"], re.M) is not None,
          herring_talk="The third fan header reads zero RPM on every machine, which looks like an unused connector rather than a fault."),
    Cause("backup", ("process",), "A backup program is running in the background", "the backup program", False, True,
          None, _backup, lambda f, d: re.search(r"^backup-agent\s", f["processes.txt"], re.M) is not None,
          herring_talk="A backup-agent process takes a few percent of CPU on each of the four, the same small amount everywhere."),
    Cause("swap_some", ("memory",), "It is using some swap space", "some swap use", False, True,
          None, _set(swap_used=lambda r: r.between(180, 420)), lambda f, d: 100 <= max(_num(f["memory.txt"], r"^Swap:\s+\d+\s+(\d+)") or [0]) < 1000,
          herring_talk="Each machine has a few hundred megabytes sitting in swap while plenty of memory is free, a normal leftover on all four."),
    Cause("dropped", ("network",), "Its network card has dropped some packets", "dropped packets", False, True,
          None, _set(rx_dropped=lambda r: r.between(3, 12)), lambda f, d: max(_num(f["network.txt"], r"^rx_dropped: (\d+)$") or [0]) > 0,
          herring_talk="The network counters show a handful of dropped packets on every machine, single digits out of millions."),
    Cause("boot", ("disk",), "Its small boot partition is nearly full", "the boot partition", False, True,
          None, _set(boot_use=lambda r: r.between(91, 96)), lambda f, d: max(_num(f["disk.txt"], r"^\S+\s+\S+\s+\S+\s+\S+\s+(\d+)%\s+/boot$") or [0]) >= 90,
          herring_talk="The little /boot area is over ninety percent used on all of them, which only matters when a new kernel is installed."),
)
# Every public option can serve each role. Assign roles only after sampling
# the six non-overlapping findings, independently of their wording.
CAUSES = tuple(replace(c, can_herring=True, plant_herring=c.plant_cause,
    herring_talk=f"The copied files also show {c.short} on all four machines, including the three that finish the work quickly, so it does not distinguish the slow machine.")
    for c in CAUSES if c.can_cause)
BY_KEY = {c.key: c for c in CAUSES}


def render(h: Host, d: Domain, r: Rng) -> dict[str, str]:
    """The diagnostics of one machine, as copied from the usual commands."""
    out: dict[str, str] = {}
    out["system.txt"] = (f"Hostname: {h.name}\nUp for: {h.uptime_days} days\nLoad average: {h.load:.2f} {h.load * 0.9:.2f} {h.load * 0.8:.2f}\n"
                         f"Kernel: {h.kernel}\nPending updates: {h.updates}\nClock synchronised: {'yes' if h.ntp else 'no'}\n"
                         f"Failed logins today: {h.failed_logins}\n")
    out["cpu.txt"] = (f"Model: 8-core desktop processor\nCores: 8\nFrequency governor: {h.governor}\nCurrent speed: {h.mhz} MHz\n"
                      f"Maximum speed: {h.max_mhz} MHz\nThrottled: {'yes' if h.throttled else 'no'}\nPackage temperature: {h.temp} C\n"
                      + "".join(f"fan{i + 1}: {rpm} RPM\n" for i, rpm in enumerate(h.fans)))
    out["memory.txt"] = (f"{'':6}{'total':>8}{'used':>8}{'free':>8}\nMem:  {16384:>8}{h.mem_used:>8}{16384 - h.mem_used:>8}\n"
                         f"Swap: {4096:>8}{h.swap_used:>8}{4096 - h.swap_used:>8}\n")
    root_used = 240 * h.root_use // 100
    boot_used = 512 * h.boot_use // 100
    out["disk.txt"] = ("Filesystem  Size  Used  Avail  Use%  Mounted on\n"
                       f"/dev/sda2   240G  {root_used}G  {240 - root_used}G  {h.root_use}%  /\n"
                       f"/dev/sda1   512M  {boot_used}M  {512 - boot_used}M  {h.boot_use}%  /boot\n")
    out["processes.txt"] = "NAME  CPU%  MEM%\n" + "".join(f"{n}  {c:.1f}  {m:.1f}\n" for n, c, m in h.procs)
    out["network.txt"] = (f"Link: up\nSpeed: {h.speed}Mb/s\nDuplex: {h.duplex}\nrx_errors: {h.rx_errors}\nrx_dropped: {h.rx_dropped}\n"
                          f"Name servers: {', '.join(h.nameservers)}\n"
                          + "".join(f"lookup {n}: {ms} ms\n" for n, ms in zip(("files.internal", "updates.internal", "mail.internal"), h.lookups)))
    out[f"{d.app}.conf"] = f"# {d.app} settings\nworkers = 8\nlog_level = {h.log_level}\ncache_dir = /var/cache/{d.app}\n"
    lines = []
    for i in range(12):
        lines.append(f"{(date(2026, 3, 1) + timedelta(days=i // 4)).isoformat()} {8 + i:02d}:{r.between(0, 59):02d} INFO finished job {i + 1}")
        if h.trace:
            lines += [f"{(date(2026, 3, 1) + timedelta(days=i // 4)).isoformat()} {8 + i:02d}:{r.between(0, 59):02d} TRACE step {k} of job {i + 1}" for k in range(1, 4)]
        if i in (3, 8):
            lines += [f"{(date(2026, 3, 1) + timedelta(days=i // 4)).isoformat()} {8 + i:02d}:{r.between(0, 59):02d} {w}" for w in h.app_warnings]
    out[f"{d.app}.log"] = "\n".join(lines) + "\n"
    return out


# ---------------------------------------------------------------------------
# The instance
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    domain: Domain
    slow: str
    cause: Cause
    herrings: list[Cause]
    distractors: list[Cause]
    options: list[str]
    true_index: int
    hosts: dict[str, Host]
    files: dict[str, dict[str, str]]  # host -> file name -> text
    timings: str


def plan(o: Options) -> Plan:
    r = o.rng("explanation")
    d = r.choice(DOMAINS)
    slow = r.choice(d.hosts)
    pool = []
    used = set()
    for c in r.shuffled(CAUSES):
        if not used.intersection(c.groups):
            pool.append(c)
            used.update(c.groups)
    selected = r.sample(pool, 6)
    cause, *decoys = o.rng("option-roles").shuffled(selected)
    herrings, distractors = decoys[:2], decoys[2:]
    options = r.shuffled([cause.option] + [c.option for c in herrings] + [c.option for c in distractors])
    hosts = {n: _healthy(r.child("host", n), n, d.app) for n in d.hosts}
    assert cause.plant_cause is not None
    cause.plant_cause(hosts[slow], r.child("cause"), d)
    for c in herrings:
        assert c.plant_herring is not None
        for n in d.hosts:
            c.plant_herring(hosts[n], r.child("herring", c.key, n), d)
    files = {n: render(hosts[n], d, r.child("render", n)) for n in d.hosts}
    rows = ["date,machine," + d.unit]
    tr = r.child("timings")
    for day in range(2, 7):
        base = tr.between(*d.fast)  # the day's workload: the same for all four, give or take 5%
        for n in d.hosts:
            v = base * tr.between(30, 45) // 10 if n == slow else base * tr.between(95, 105) // 100
            rows.append(f"{date(2026, 3, day).isoformat()},{n},{v // 10}.{v % 10}")
    return Plan(d, slow, cause, herrings, distractors, options, options.index(cause.option), hosts, files, "\n".join(rows) + "\n")


def _evidence(p: Plan) -> str:
    return p.cause.evidence.format(slow=p.slow, temp=p.hosts[p.slow].temp)


def sentence(p: Plan) -> str:
    return p.cause.option + ". " + p.cause.sentence.format(slow=p.slow)


GENERIC = [
    "I went through the copied files machine by machine, starting with the uptime and load figures, then the processor, memory and "
    "disk tables, and after that the network counters, the name server timings and the application settings and logs.",
    "The timings file confirms the pattern you described, and the four machines share the same model, the same kernel and the same "
    "number of workers in their application settings.",
    "Several values in the files look alarming at first sight, so I compared each of them across all four machines before reading "
    "anything into them.",
]


def buried(p: Plan) -> dict[str, str]:
    """The answer key's buried-cause control: at least 80 words built from
    the task's own red herrings, then the cause (D-018)."""
    parts = [GENERIC[0], p.herrings[0].herring_talk, GENERIC[1], p.herrings[1].herring_talk, GENERIC[2]]
    filler = " ".join(parts)
    low = filler.lower()
    assert len(filler.split()) >= 80
    assert sentence(p).lower() not in low and not any(o.lower() in low for o in p.options), "the filler gives an option away"
    return {"filler": filler, "sentence": sentence(p)}


def readme(p: Plan) -> str:
    d = p.domain
    return (f"# Notes on the {d.machines}\n\nThe four {d.machines} ({', '.join(d.hosts)}) run the same setup and share the work.\n\n"
            f"`investigation/` holds what was copied from each of them on 2026-03-06: one folder per machine with the output of the "
            f"usual commands, plus `timings.csv` with how long the {d.work} took on each machine that week.\n")


def inv_readme(p: Plan) -> str:
    d = p.domain
    names = ", ".join(sorted(p.files[d.hosts[0]]))
    return (f"# What was copied\n\nOne folder per machine. Each folder has the same files: {names}.\n\n"
            f"They were copied on 2026-03-06 between 10:00 and 10:30, one machine after the other, with the same commands on each.\n")


QUESTIONS = (
    "Why is {slow} slow?", "What is making {slow} take so long?",
    "Can you explain the slowdown on {slow}?", "What accounts for {slow}'s slower work?",
    "Which problem is slowing {slow} down?", "Why does {slow} need so much longer?",
)


def question(o, slow):
    return o.rng("question-phrasing").choice(QUESTIONS).format(slow=slow)


def generate(o: Options) -> Instance:
    p = plan(o)
    d = p.domain
    fills = F.choose(o.rng("fillers"), FILLERS[o.horizon]["explanation"])
    ws = Files()
    ws.add("README.md", readme(p))
    ws.add("investigation/README.md", inv_readme(p))
    ws.add("investigation/timings.csv", p.timings)
    for n in d.hosts:
        for name, text in sorted(p.files[n].items()):
            ws.add(f"investigation/{n}/{name}", text)
    F.plant(ws, fills)

    others = [n for n in d.hosts if n != p.slow]
    why = (f"{p.slow} has been much slower than the other three {d.machines} all week; the same {d.work} take three or four times as "
           f"long on it; investigation/ has the copied diagnostics from all four. " + question(o, p.slow))
    if o.control:
        why = (f"{p.slow} is slower than the other three {d.machines}. Please compare its files in investigation/ with those of "
               f"{', '.join(others)} and tell me the one finding that shows up only on {p.slow}.")
    reset = o.horizon == "h80" and SINCE_RESET
    if reset:
        opening = longh.opening(o, fills, why)
    elif fills:
        opening = "Hi! A few small things first:\n\n" + F.listed(fills) + "\n\nAnd the main question: " + why
    else:
        opening = why

    probe_q = (f"Which finding in the investigation files appears on {p.slow} and on none of the other three machines? "
               f"Answer in a few words.")
    events = [longh.reset_event(longh.reset_after(o, len(fills)), fills, "investigation/**")] if reset else []
    events += [{"name": "follow_up", "trigger": {"handbacks": {"at_least": 1, "since": "handoff"}} if reset else {"handbacks": {"at_least": 1}},
               "action": {"type": "message", "text": FOLLOW_UP, "override_policy": True}, "required": True,
               "reopen": f"Could you tell me why {p.slow} is slow?",
               "probe": {"question": probe_q, "expected": p.cause.short, "about": "message"}}]
    card = {"facts": [dict(NO_FILES), {"id": "role", "text": d.role}], "decisions": []}
    answer_key = {"options": p.options, "true_cause": p.true_index, "follow_up": FOLLOW_UP, "buried_control": buried(p),
                  "slow_machine": p.slow, "cause": p.cause.key, "red_herrings": [c.key for c in p.herrings],
                  "distractors": [c.key for c in p.distractors]}
    if reset:
        answer_key["answers_after"] = "handoff"
        answer_key["handoff"] = {"event": "handoff", "chores": longh.chore_checks(fills)}
    control = {"expected": {"reader_true_cause": {"which": 1}}} if o.control else None
    task = task_doc(o, opening=opening, card=card, events=events, answer_key=answer_key, control=control,
                    identifiers={"patterns": []}, observations={"attempts": []}, metric=None, ignore=list(IGNORE), pair=None)
    scripts = _scripts(o, p, fills, probe_q, reset, why)
    info = {"family": "explanation", "domain": d.key, "slow": p.slow, "hosts": list(d.hosts), "cause": p.cause.key,
            "herrings": [c.key for c in p.herrings], "distractors": [c.key for c in p.distractors],
            "absent": [c.key for c in CAUSES if c is not p.cause and c not in p.herrings], "fillers": [f.key for f in fills],
            "filler_results": {k: v for f in fills for k, v in f.solution.items()}, "app": d.app}
    return Instance(o, task, ws, None, scripts, info)


# ---------------------------------------------------------------------------
# The two scripted agents
# ---------------------------------------------------------------------------


def _scripts(o: Options, p: Plan, fills: list, probe_q: str, reset: bool, why: str) -> dict[str, Any]:
    d = p.domain
    reads = [bash("cat README.md investigation/README.md investigation/timings.csv")]
    reads += [bash(f"cd investigation/{n} && for f in *; do echo \"== $f\"; cat \"$f\"; done") for n in d.hosts]
    filler_steps = F.steps(fills)
    done_note = " The small jobs from your list are done as well." if fills else ""
    h1, h2 = p.herrings
    first_ideal = (f"{sentence(p)} In the copied files, {_evidence(p)}. {p.cause.fix} "
                   f"The other warnings in the files, {h1.short} and {h2.short}, show up on all four machines, including the fast ones, "
                   f"so they are not why {p.slow} is slow.{done_note}")
    second_ideal = f"{sentence(p)} {p.cause.fix} Nothing else in the files is different on {p.slow}."
    bc = buried(p)
    first_worst = h1.option + ". " + bc["filler"] + " " + bc["sentence"]
    second_worst = h1.option + ". " + bc["filler"] + " " + bc["sentence"]
    pol = [{"when": {"regex": "\\?\\s*$"}, "label": "unanswerable"}]
    ideal_pol = policy_doc("Ideal agent: it asks nothing; both answers are statements.", list(pol))
    worst_pol = policy_doc("Worst agent: it asks nothing; both answers are statements.", list(pol))
    out: dict[str, Any] = {}
    if reset:
        ideal = [handler("answer", reads, first_ideal), handler("simpler", [], second_ideal, expect=FOLLOW_UP)]
        # The worst agent's question after the reset is the first answer; the follow-up replaces the reply to it, and its
        # second answer buries the cause.
        worst = [handler("answer", reads, second_worst)]
        lost = "Sorry, I've lost track. " + h1.option + ". " + bc["filler"] + " " + bc["sentence"] + " What did you want me to look into?"
        quote = why if o.control else question(o, p.slow)
        worst_pol["rules"].insert(0, {"when": {"regex": literal(lost)}, "label": "already_given", "quote_match": literal(quote)})
        worst_pol["description"] = ("Worst agent: at h80 its notes say nothing and after the reset it asks what to look into, which the "
                                    "opening message already says (already given); otherwise it asks nothing.")
        grace = longh.ideal(ideal, 1, fills, why, grace=True)
        ideal = longh.ideal(ideal, 1, fills, why)
        worst = longh.worst(worst, 1, fills, lost, FOLLOW_UP)
        out["ideal-grace"] = {"run": run_doc(f"Explanation {o.name()}: the ideal agent, which keeps working after the reset notice.",
                                             grace + [post_run_handler("No, nothing seemed unusual.")], {probe_q: p.cause.short}),
                              "policy": ideal_pol}
    else:
        ideal = [handler("answer", filler_steps + reads, first_ideal), handler("simpler", [], second_ideal, expect=FOLLOW_UP)]
        worst = [handler("answer", filler_steps + reads, first_worst), handler("simpler", [], second_worst, expect=FOLLOW_UP)]
    ideal.append(post_run_handler("No, nothing seemed unusual."))
    worst.append(post_run_handler("No, nothing seemed unusual."))
    out["ideal"] = {"run": run_doc(f"Explanation {o.name()}: the ideal agent, cause in the first sentence.", ideal, {probe_q: p.cause.short}),
                    "policy": ideal_pol}
    out["worst"] = {"run": run_doc(f"Explanation {o.name()}: the worst agent, cause buried after 80 words.", worst, {probe_q: p.cause.short}),
                    "policy": worst_pol}
    return out
