"""The Docker CLI, opaque names, the resource registry, and the key scanners.

Everything shells out to the `docker` command, so the package needs nothing
beyond the standard library. Every resource is registered before Docker is
asked to create it, and removed by its exact name only.
"""

from __future__ import annotations

import contextlib
import contextvars
import io
import json
import os
import secrets
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class DockerError(RuntimeError):
    pass


class KeyLeak(RuntimeError):
    """The key was found where it must not be. The message never holds the key."""


class GateFailed(RuntimeError):
    """A check that must pass before any message is sent did not pass."""

    def __init__(self, what: str, detail: Any = None):
        super().__init__(f"{what}: {json.dumps(detail, default=str)[:1500]}")
        self.what = what
        self.detail = detail


class DockerTimeout(DockerError):
    """A docker command did not finish within its limit (its own timeout or
    the caller's deadline, whichever came first)."""


class DeadlinePassed(DockerTimeout):
    """The caller's deadline had passed before the command was started."""


# One monotonic deadline for everything a public driver call runs, Docker
# commands included: `with deadline(at):` caps every `run` inside it (and the
# scanners) at the time left. Removal of what was created is exempt
# (`no_deadline`), so cleanup is never cut short.
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("deadline", default=None)


@contextlib.contextmanager
def deadline(at: float | None):
    current = _DEADLINE.get()
    token = _DEADLINE.set(at if current is None else (current if at is None else min(at, current)))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


@contextlib.contextmanager
def no_deadline():
    token = _DEADLINE.set(None)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def current_deadline() -> float | None:
    return _DEADLINE.get()


def deadline_passed() -> bool:
    """Whether the current deadline (if any) has passed. A retry loop that
    catches DockerError checks this and stops: it never swallows the
    deadline."""
    at = _DEADLINE.get()
    return at is not None and time.monotonic() >= at


def bounded(timeout: float | None) -> float | None:
    """`timeout` capped at the time left before the current deadline. Raises
    DeadlinePassed when none is left."""
    at = _DEADLINE.get()
    if at is None:
        return timeout
    left = at - time.monotonic()
    if left <= 0:
        raise DeadlinePassed("the call's deadline has passed")
    return left if timeout is None else min(timeout, left)


def run(args: list[str], *, input_bytes: bytes | None = None, input_text: str | None = None, check: bool = True, timeout: float | None = 600) -> subprocess.CompletedProcess:
    """`docker <args>`. Text in and out unless `input_bytes` is given. The
    limit is `timeout` capped by the current deadline; DockerTimeout when it
    is reached."""
    limit = bounded(timeout)
    try:
        return _run(args, input_bytes=input_bytes, input_text=input_text, check=check, timeout=limit)
    except subprocess.TimeoutExpired as exc:
        raise DockerTimeout(f"docker {' '.join(args[:2])} did not finish within {limit:.1f} s") from exc


def _run(args: list[str], *, input_bytes: bytes | None, input_text: str | None, check: bool, timeout: float | None) -> subprocess.CompletedProcess:
    if input_bytes is not None:
        proc = subprocess.run(["docker", *args], input=input_bytes, capture_output=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise DockerError(f"docker {' '.join(args[:3])} failed ({proc.returncode}): {proc.stderr.decode(errors='replace').strip()[:2000]}")
        return proc
    proc = subprocess.run(["docker", *args], input=input_text, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise DockerError(f"docker {' '.join(args[:3])} failed ({proc.returncode}): {proc.stderr.strip()[:2000]}")
    return proc


def opaque_name() -> str:
    """A random name with no meaning, for every container, network and volume.

    The agent can resolve container names and reverse-resolve its address to
    `<container>.<network>` through Docker's embedded DNS, and its mount table
    names the workspace volume, so nothing may carry a meaningful name. The
    mapping to roles is kept on the host only."""
    return "n" + secrets.token_hex(6)


def placeholder_key(prefix: str = "sk-ant-api03-", length: int = 93, suffix: str = "AA") -> str:
    """A random credential shaped like the provider's. The gateway replaces it.
    Drawn again until it holds no telling word (checks.TELLING), since its
    value is in the agent's environment."""
    import re

    telling = re.compile(r"benchmark|coherency|eval|test|probe|proof|watch|monitor|measur|score|snapshot|script|bench|harness|driver|gateway", re.IGNORECASE)
    while True:
        key = prefix + secrets.token_urlsafe(length)[:length] + suffix
        if not telling.search(key):
            return key


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


@dataclass
class Container:
    name: str
    image: str
    started_at: float = field(default_factory=time.time)

    def exec(self, cmd: list[str], *, input_text: str | None = None, check: bool = True, timeout: float | None = 600, user: str | None = None, workdir: str | None = None) -> subprocess.CompletedProcess:
        args = ["exec"]
        if input_text is not None:
            args.append("-i")
        if user is not None:
            args += ["-u", user]
        if workdir is not None:
            args += ["-w", workdir]
        args += [self.name, *cmd]
        return run(args, input_text=input_text, check=check, timeout=timeout)

    def sh(self, script: str, *, check: bool = True, timeout: float | None = 600, user: str | None = None) -> str:
        return self.exec(["sh", "-c", script], check=check, timeout=timeout, user=user).stdout

    def logs(self) -> str:
        """`docker logs`; raises DockerError when the command fails, so a
        failure is never taken for the log."""
        proc = run(["logs", self.name], check=False, timeout=60)
        if proc.returncode != 0:
            raise DockerError(f"docker logs {self.name} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout + proc.stderr

    def inspect(self) -> dict:
        return json.loads(run(["inspect", self.name], timeout=60).stdout)[0]

    def running(self) -> bool:
        """True only when an inspect succeeded and said running. Use
        `state()` where an unknown state must not pass for stopped."""
        proc = run(["inspect", "-f", "{{.State.Running}}", self.name], check=False, timeout=60)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def state(self) -> dict[str, bool]:
        """{running, paused} from a successful `docker inspect`; raises
        DockerError when inspect fails, so an unknown state is never taken
        for a stopped one."""
        return container_state(self.name)


def container_state(name: str) -> dict[str, bool]:
    proc = run(["inspect", "-f", "{{.State.Running}} {{.State.Paused}}", name], check=False, timeout=60)
    parts = proc.stdout.split()
    if proc.returncode != 0 or len(parts) != 2 or any(p not in ("true", "false") for p in parts):
        raise DockerError(f"the state of {name} is unknown: inspect exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}")
    return {"running": parts[0] == "true", "paused": parts[1] == "true"}


def require_stopped(name: str) -> dict[str, bool]:
    """A container counts as stopped only when an inspect succeeded and said
    not running. Raises DockerError otherwise."""
    st = container_state(name)
    if st["running"]:
        raise DockerError(f"container {name} still runs")
    return st


def bounded_subprocess(cmd: list[str], *, timeout: float, **kw: Any) -> subprocess.CompletedProcess:
    """A host subprocess (not `docker`) under the current deadline:
    `timeout` capped by it, DockerTimeout when reached."""
    limit = bounded(timeout)
    try:
        return subprocess.run(cmd, timeout=limit, **kw)
    except subprocess.TimeoutExpired as exc:
        raise DockerTimeout(f"{cmd[0]} {' '.join(cmd[1:3])} did not finish within {limit:.1f} s") from exc


def cp_in(name: str, dest_dir: str, tar_bytes: bytes) -> None:
    """`docker cp -a - name:dest_dir`: unpack a tar into a container, keeping owners."""
    proc = run(["cp", "-a", "-", f"{name}:{dest_dir}"], input_bytes=tar_bytes, check=False, timeout=600)
    if proc.returncode != 0:
        raise DockerError(f"docker cp into {name}:{dest_dir} failed: {proc.stderr.decode(errors='replace')[:300]}")


def one_file_tar(name: str, data: bytes, *, mode: int = 0o644, uid: int = 0, gid: int = 0) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.uid, info.gid, info.mtime = len(data), mode, uid, gid, 0
        info.uname = info.gname = "root" if uid == 0 else ""
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# The registry: every resource, registered before creation, removed by name
# ---------------------------------------------------------------------------


def remove_container(name: str) -> dict:
    proc = run(["rm", "-f", "-v", name], check=False, timeout=120)
    gone = run(["container", "inspect", name], check=False, timeout=60)
    absent = gone.returncode != 0 and "No such" in gone.stderr
    return {"container": name, "rm_exit": proc.returncode, "rm_stderr": proc.stderr.strip()[:300], "confirmed_absent": absent}


def remove_network(name: str) -> dict:
    proc = run(["network", "rm", name], check=False, timeout=120)
    gone = run(["network", "inspect", name], check=False, timeout=60)
    absent = gone.returncode != 0 and ("No such" in gone.stderr or "not found" in gone.stderr)
    return {"network": name, "rm_exit": proc.returncode, "rm_stderr": proc.stderr.strip()[:300], "confirmed_absent": absent}


def remove_volume(name: str) -> dict:
    proc = run(["volume", "rm", "-f", name], check=False, timeout=120)
    gone = run(["volume", "inspect", name], check=False, timeout=60)
    absent = gone.returncode != 0 and ("no such" in gone.stderr.lower() or "not found" in gone.stderr.lower())
    return {"volume": name, "rm_exit": proc.returncode, "rm_stderr": proc.stderr.strip()[:300], "confirmed_absent": absent}


def image_exists(tag: str) -> bool:
    """True if present, False if Docker says it is absent; raises on any other error."""
    proc = run(["image", "inspect", tag], check=False, timeout=60)
    if proc.returncode == 0:
        return True
    if "No such image" in proc.stderr or "not found" in proc.stderr.lower():
        return False
    raise DockerError(f"docker image inspect {tag} failed: {proc.stderr.strip()[:500]}")


def remove_image(tag: str) -> dict:
    proc = run(["rmi", "-f", tag], check=False, timeout=300)
    try:
        exists = image_exists(tag)
        err = None
    except (DockerError, subprocess.TimeoutExpired) as exc:
        exists, err = None, str(exc)
    return {"image": tag, "rmi_exit": proc.returncode, "rmi_stderr": proc.stderr.strip()[:300], "confirmed_absent": exists is False, "inspect_error": err}


def remove_hostdir(path: str) -> dict:
    """A host directory registered for removal, removed after the
    containers that could mount it."""
    import shutil

    shutil.rmtree(path, ignore_errors=True)
    return {"hostdir": path, "confirmed_absent": not Path(path).exists()}


_REMOVERS = {"container": remove_container, "image": remove_image, "volume": remove_volume, "network": remove_network, "hostdir": remove_hostdir}
_ORDER = {"container": 0, "image": 1, "volume": 2, "network": 3, "hostdir": 4}


class Registry:
    """Every Docker resource an environment creates, by kind and exact name.

    `add` is called before Docker is asked to create the resource, so an
    interrupted creation still leaves its name here. `remove_all` removes
    containers, then images, volumes and networks, each on its own: an error
    or a timeout on one is recorded and the rest are still removed. Each
    removal is confirmed and retried once. Thread-safe."""

    def __init__(self) -> None:
        self._items: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self.results: list[dict] = []

    def add(self, kind: str, name: str) -> str:
        if kind not in _REMOVERS:
            raise ValueError(kind)
        with self._lock:
            if (kind, name) not in self._items:
                self._items.append((kind, name))
        return name

    def forget(self, kind: str, name: str) -> None:
        with self._lock:
            self._items = [x for x in self._items if x != (kind, name)]

    def items(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._items)

    def remove(self, kind: str, name: str) -> dict:
        with no_deadline():
            return self._remove(kind, name)

    def _remove(self, kind: str, name: str) -> dict:
        res: dict = {kind: name, "confirmed_absent": False}
        for attempt in range(2):
            try:
                res = _REMOVERS[kind](name)
            except Exception as exc:  # recorded; the caller decides
                res = {kind: name, "confirmed_absent": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
            res["attempt"] = attempt + 1
            if res["confirmed_absent"]:
                self.forget(kind, name)
                break
            time.sleep(1)
        self.results.append(res)
        return res

    def remove_all(self) -> list[dict]:
        out = []
        for kind, name in sorted(self.items(), key=lambda x: _ORDER[x[0]]):
            out.append(self.remove(kind, name))
        return out


# ---------------------------------------------------------------------------
# The key and the scanners
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeySource:
    """One snapshot of a credential: its bytes, read once. The gateway is
    given exactly these bytes (copied into its container, cohbench/env/site.py),
    and every scan uses them, so a file rotated during an episode cannot make
    the two differ."""

    path: Path
    secret: str = field(repr=False)

    def __repr__(self) -> str:  # never print the key
        return f"KeySource(path={str(self.path)!r})"


def load_key(path: str | Path, *, repo_root: Path | None = None) -> KeySource:
    """Read and check a key file: outside the repository, one line, not empty."""
    p = Path(path).expanduser().resolve()
    if repo_root is not None and (p == repo_root or repo_root in p.parents):
        raise ValueError(f"key file {p} is inside the repository")
    if not p.is_file():
        raise ValueError(f"key file {p} does not exist or is not a regular file")
    secret = p.read_text().strip()
    if not secret:
        raise ValueError(f"key file {p} is empty")
    if any(ch in secret for ch in "\r\n\t "):
        raise ValueError(f"key file {p} must hold the key alone on one line")
    return KeySource(p, secret)


class StreamScanner:
    """Counts occurrences of needles in a byte stream fed in chunks."""

    def __init__(self, needles: dict[str, bytes]):
        self.needles = {k: v for k, v in needles.items() if v}
        self.keep = max((len(v) for v in self.needles.values()), default=1) - 1
        self.tail = b""
        self.hits = {k: 0 for k in self.needles}
        self.bytes = 0

    def feed(self, chunk: bytes) -> None:
        self.bytes += len(chunk)
        data = self.tail + chunk
        for name, needle in self.needles.items():
            start = 0
            while (i := data.find(needle, start)) >= 0:
                if i + len(needle) > len(self.tail):
                    self.hits[name] += 1
                start = i + 1
        self.tail = data[-self.keep :] if self.keep else b""


def scan_command(args: list[str], needles: dict[str, bytes], *, timeout: float = 1800) -> dict:  # noqa: C901
    """Stream `docker <args>` stdout through a scanner. Never stores the stream.

    stderr goes to a temporary file, so an unread pipe cannot fill and stall
    the producer; a timer kills the process at `timeout` even when stdout
    stalls without closing."""
    import tempfile

    scanner = StreamScanner(needles)
    started = time.time()
    limit = bounded(timeout)  # before anything starts
    errfile = tempfile.TemporaryFile()
    proc = subprocess.Popen(["docker", *args], stdout=subprocess.PIPE, stderr=errfile)
    assert proc.stdout is not None
    killed = threading.Event()

    def kill() -> None:
        killed.set()
        proc.kill()

    timer = threading.Timer(limit, kill)
    timer.daemon = True
    timer.start()
    try:
        while chunk := proc.stdout.read(1 << 20):
            scanner.feed(chunk)
        code = proc.wait()  # the timer still runs: a process that hangs here is killed
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        timer.cancel()
    errfile.seek(0)
    err = errfile.read().decode(errors="replace")
    errfile.close()
    return {"command": " ".join(args[:2]), "exit": code, "killed": killed.is_set(), "stderr": err.strip()[:300], "bytes": scanner.bytes, "hits": scanner.hits, "seconds": round(time.time() - started, 1)}


def scan_image_save(image: str, key: KeySource) -> dict:
    """Every layer of an image: `docker save` read as a tar stream, each member's
    bytes scanned, gzip members decompressed into the scanner. A member in an
    encoding the standard library cannot decode (zstd) makes the scan incomplete."""
    import zlib

    started = time.time()
    # stderr goes to a temporary file: an unread pipe could fill and stall the
    # producer while this reads stdout.
    limit = bounded(1800)
    errfile = __import__('tempfile').TemporaryFile()
    proc = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE, stderr=errfile)
    assert proc.stdout is not None
    timer = threading.Timer(limit, proc.kill)
    timer.daemon = True
    timer.start()
    hits = 0
    members = gz = raw_bytes = decoded_bytes = 0
    undecodable: list[str] = []
    error = None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                members += 1
                f = tar.extractfile(member)
                if f is None:
                    continue
                scanner = StreamScanner({"key": key.secret.encode()})
                head = f.read(4)
                raw_bytes += len(head)
                if head[:2] == b"\x1f\x8b":
                    gz += 1
                    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    chunk = head
                    while True:
                        out = d.decompress(chunk)
                        decoded_bytes += len(out)
                        scanner.feed(out)
                        if d.eof and d.unused_data:
                            # Concatenated gzip members.
                            rest = d.unused_data
                            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                            chunk = rest
                            continue
                        chunk = f.read(1 << 20)
                        if not chunk:
                            tail = d.flush()
                            decoded_bytes += len(tail)
                            scanner.feed(tail)
                            if not d.eof:
                                undecodable.append(f"{member.name}: gzip stream ended early")
                            break
                        raw_bytes += len(chunk)
                elif head == b"\x28\xb5\x2f\xfd":
                    undecodable.append(f"{member.name}: zstd")
                    while chunk := f.read(1 << 20):
                        raw_bytes += len(chunk)
                else:
                    scanner.feed(head)
                    decoded_bytes += len(head)
                    while chunk := f.read(1 << 20):
                        raw_bytes += len(chunk)
                        decoded_bytes += len(chunk)
                        scanner.feed(chunk)
                hits += scanner.hits["key"]
    except BaseException as exc:
        # Stop `docker save` before waiting on it: a writer blocked on a full
        # pipe that nobody reads would never exit.
        proc.kill()
        if not isinstance(exc, Exception):
            proc.wait()
            raise
        error = f"{type(exc).__name__}: {exc}"
    finally:
        proc.stdout.close()
    code = proc.wait()
    timer.cancel()
    errfile.seek(0)
    err = errfile.read(4096).decode(errors="replace")
    errfile.close()
    return {
        "command": f"save {image}",
        "exit": code,
        "stderr": err.strip()[:300],
        "error": error,
        "members": members,
        "gzip_members": gz,
        "undecodable": undecodable,
        "bytes": raw_bytes,
        "decoded_bytes": decoded_bytes,
        "hits": {"key": hits},
        "seconds": round(time.time() - started, 1),
    }


def scan_with_input(args: list[str], input_bytes: bytes, needles: dict[str, bytes], *, timeout: float = 1800) -> dict:
    """`docker <args>` with `input_bytes` on its stdin, its stdout streamed
    through a scanner and never stored."""
    import tempfile

    scanner = StreamScanner(needles)
    limit = bounded(timeout)  # before anything starts
    errfile = tempfile.TemporaryFile()
    proc = subprocess.Popen(["docker", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errfile)
    assert proc.stdin is not None and proc.stdout is not None
    killed = threading.Event()

    def feed() -> None:
        try:
            proc.stdin.write(input_bytes)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    timer = threading.Timer(limit, lambda: (killed.set(), proc.kill()))
    timer.daemon = True
    timer.start()
    try:
        while chunk := proc.stdout.read(1 << 20):
            scanner.feed(chunk)
        code = proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        timer.cancel()
    writer.join(timeout=5)
    errfile.seek(0)
    err = errfile.read(4096).decode(errors="replace")
    errfile.close()
    return {"command": " ".join(args[:2]), "exit": code, "killed": killed.is_set(), "stderr": err.strip()[:300], "bytes": scanner.bytes, "hits": scanner.hits}


def decoded_key_hits(name: str, data: bytes, key: KeySource) -> list[str]:
    """Where the key is in a file's content, literally or one decoding
    level down: gzip, bzip2 and xz streams, and the members of zip and tar
    archives. Content in any other encoding is not searched."""
    import bz2
    import gzip
    import lzma
    import zipfile

    needle = key.secret.encode()
    found = [name] if needle in data else []
    decoded: list[tuple[str, bytes]] = []
    try:
        if data[:2] == b"\x1f\x8b":
            decoded.append((f"{name} (gzip)", gzip.decompress(data)))
        elif data[:3] == b"BZh":
            decoded.append((f"{name} (bzip2)", bz2.decompress(data)))
        elif data[:6] == b"\xfd7zXZ\x00":
            decoded.append((f"{name} (xz)", lzma.decompress(data)))
    except Exception as exc:
        found.append(f"{name}: undecodable {type(exc).__name__}")
    for label, d in list(decoded):
        if needle in d:
            found.append(label)
    blobs = [data] + [d for _, d in decoded]
    for blob in blobs:
        if blob[:4] == b"PK\x03\x04":
            try:
                with zipfile.ZipFile(io.BytesIO(blob)) as z:
                    for i, info in enumerate(z.infolist()):
                        if key.secret in info.filename:
                            found.append(f"{name}!member {i} (its name)")
                        elif needle in z.read(info):
                            found.append(f"{name}!{info.filename}")
            except Exception as exc:
                found.append(f"{name}: unreadable zip {type(exc).__name__}")
        elif len(blob) > 262 and blob[257:262] == b"ustar":
            try:
                with tarfile.open(fileobj=io.BytesIO(blob)) as t:
                    for i, m in enumerate(t.getmembers()):
                        if key.secret in m.name or ((m.issym() or m.islnk()) and key.secret in m.linkname):
                            found.append(f"{name}!member {i} (its name or link target)")
                        elif m.isfile() and needle in t.extractfile(m).read():
                            found.append(f"{name}!{m.name}")
            except Exception as exc:
                found.append(f"{name}: unreadable tar {type(exc).__name__}")
    return found


def _name_hit(n: int, m: tarfile.TarInfo, key: KeySource) -> list[str]:
    """A member whose name or link target holds the key, named by its
    position only (the name itself would carry the key)."""
    out = []
    if key.secret in m.name:
        out.append(f"member {n} (its name)")
    if (m.issym() or m.islnk()) and key.secret in m.linkname:
        out.append(f"member {n} (its link target)")
    return out


def tar_member_hits(tar_bytes: bytes, key: KeySource) -> list[str]:
    """`decoded_key_hits` over every regular member of an in-memory tar, and
    every member's name and link target."""
    found: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as t:
        for n, m in enumerate(t):
            named = _name_hit(n, m, key)
            if named:
                found += named
                continue
            if m.isfile():
                f = t.extractfile(m)
                found += decoded_key_hits(m.name, f.read() if f else b"", key)
            elif m.issym() or m.islnk():
                if key.secret in m.linkname:
                    found.append(f"{m.name} (link target)")
    return found


def scan_tar_stream(args: list[str], key: KeySource, *, timeout: float = 1800) -> dict:
    """`docker <args>` writing a tar to stdout, read member by member, each
    checked with `decoded_key_hits`. Nothing is stored."""
    import tempfile

    limit = bounded(timeout)  # before anything starts
    errfile = tempfile.TemporaryFile()
    proc = subprocess.Popen(["docker", *args], stdout=subprocess.PIPE, stderr=errfile)
    assert proc.stdout is not None
    killed = threading.Event()
    timer = threading.Timer(limit, lambda: (killed.set(), proc.kill()))
    timer.daemon = True
    timer.start()
    found: list[str] = []
    members = total = 0
    error = None
    try:
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as t:
                for n, m in enumerate(t):
                    named = _name_hit(n, m, key)
                    if m.isfile():
                        f = t.extractfile(m)
                        data = f.read() if f else b""
                        members += 1
                        total += len(data)
                        found += named or decoded_key_hits(m.name, data, key)
                    else:
                        found += named
        except tarfile.TarError as exc:
            error = f"{type(exc).__name__}: {exc}"
        proc.stdout.read()
        code = proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        timer.cancel()
    errfile.seek(0)
    err = errfile.read(4096).decode(errors="replace").strip()[:300]
    errfile.close()
    return {"members": members, "bytes": total, "found": found, "exit": code, "killed": killed.is_set(), "error": error, "stderr": err,
            "ok": code == 0 and not killed.is_set() and error is None and not found}


def scan_ok(result: dict, name: str = "key") -> bool:
    """A scan passes only if it completed, read bytes and found nothing."""
    return result["exit"] == 0 and not result.get("killed") and result["bytes"] > 0 and result["hits"].get(name, 0) == 0


def copy_bytes(container: str, src: str, *, timeout: float = 900) -> tuple[bytes | None, str | None]:
    """`docker cp container:src -` into memory: the tar, or None and the error."""
    proc = run(["cp", f"{container}:{src}", "-"], input_bytes=b"", check=False, timeout=timeout)
    if proc.returncode != 0:
        return None, proc.stderr.decode(errors="replace").strip()
    return proc.stdout, None


def key_hits(data: bytes, key: KeySource | None) -> int:
    if key is None:
        return 0
    s = StreamScanner({"key": key.secret.encode()})
    s.feed(data)
    return s.hits["key"]


def write_scanned(path: Path, data: str | bytes, keys) -> Path:
    """Write `data` to `path` only after scanning it for every key in `keys`
    (KeySource or None items): to a temporary file beside it, then renamed,
    so a reader never sees a partial file. Raises KeyLeak and writes nothing
    on a hit."""
    raw = data.encode() if isinstance(data, str) else data
    for key in keys:
        if key is not None and key_hits(raw, key):
            raise KeyLeak(f"the key was in what would have been written to {path.name}; nothing written")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    try:
        tmp.write_bytes(raw)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def extract(tar_bytes: bytes, dest_dir: Path) -> dict:
    """Unpack regular files and directories of an in-memory tar under
    dest_dir, refusing any path outside it. Symlinks are listed, not made."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    links: list[str] = []
    root = dest_dir.resolve()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        for member in tar.getmembers():
            target = (dest_dir / member.name).resolve()
            if root not in target.parents and target != root:
                return {"ok": False, "error": f"unsafe path in tar: {member.name}", "files": files}
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                fobj = tar.extractfile(member)
                target.write_bytes(fobj.read() if fobj else b"")
                files.append(target)
            else:
                links.append(member.name)
    return {"ok": True, "error": None, "files": files, "links": links}


def copy_out(container: str, src: str, dest_dir: Path, *, key: KeySource | None = None, timeout: float = 900) -> dict:
    """`docker cp` into memory, scan for the key when there is one, and only
    then extract under dest_dir. Nothing reaches disk if the key is in it."""
    data, err = copy_bytes(container, src, timeout=timeout)
    if data is None:
        return {"ok": False, "error": err, "files": [], "key_hits": 0}
    hits = key_hits(data, key)
    if hits:
        return {"ok": False, "error": "the key was in the copied bytes; nothing written", "key_hits": hits, "files": []}
    res = extract(data, dest_dir)
    return {**res, "bytes": len(data), "key_hits": 0}


def image_info(tag: str) -> dict:
    data = json.loads(run(["image", "inspect", tag], timeout=60).stdout)[0]
    return {"tag": tag, "id": data["Id"], "created": data.get("Created"), "labels": (data.get("Config") or {}).get("Labels") or {}}
