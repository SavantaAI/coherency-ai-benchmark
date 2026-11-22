"""The workspace volume (D-014): create, populate, clone, list.

The harness container never gets a bind mount. A short-lived helper
container from the harness image (no network, root, entrypoint replaced)
mounts the volume and unpacks a tar from its stdin (cohbench/env/workspace.py
makes the tar from the task's manifest). A clone is `cp -a` between two
volumes in the same kind of helper: owners, modes, nanosecond times and hard
links kept.
"""

from __future__ import annotations

from .docker import Container, DockerError, Registry, opaque_name, run

AGENT_UID = 1000
AGENT_GID = 1000


def _helper(image: str, mounts: list[str], script: str, *, input_bytes: bytes | None, registry: Registry) -> None:
    """One shell script in a throwaway container: no network, root,
    entrypoint replaced. Registered by name before it is created."""
    name = registry.add("container", opaque_name())
    args = ["run", "--rm", "--security-opt", "seccomp=builtin", "--name", name, "--network", "none", "--user", "0", "--entrypoint", "sh"]
    if input_bytes is not None:
        args.append("-i")
    for m in mounts:
        args += ["--mount", m]
    args += [image, "-c", script]
    try:
        proc = run(args, input_bytes=input_bytes if input_bytes is not None else b"", check=False, timeout=900)
    finally:
        registry.remove("container", name)
    if proc.returncode != 0:
        raise DockerError(f"volume helper failed ({proc.returncode}): {proc.stderr.decode(errors='replace')[-800:]}")


def create(registry: Registry) -> str:
    name = registry.add("volume", opaque_name())
    run(["volume", "create", name], timeout=120)
    return name


def populate(volume: str, tar_bytes: bytes, image: str, registry: Registry, *, uid: int = AGENT_UID, gid: int = AGENT_GID) -> None:
    """Unpack a tar into an empty volume, owners and modes as in the tar, and
    the volume's root owned by the agent's user."""
    script = f"set -e; cd /v; tar -x -p --same-owner -f -; chown {uid}:{gid} /v"
    _helper(image, [f"type=volume,src={volume},dst=/v,volume-nocopy"], script, input_bytes=tar_bytes, registry=registry)


def clone(src: str, dst: str, image: str, registry: Registry) -> None:
    """Copy one volume's whole content into another (`cp -a`). The source is
    mounted read-only."""
    script = "set -e; cp -a /a/. /b/; chown --reference=/a /b; chmod --reference=/a /b; touch -r /a /b"
    _helper(image, [f"type=volume,src={src},dst=/a,readonly,volume-nocopy", f"type=volume,src={dst},dst=/b,volume-nocopy"], script, input_bytes=None, registry=registry)


LISTING = (
    "cd /v && find . -printf '%P|%y|%m|%u|%g|%T@|%l|' \\( -type d -printf '-\\n' -o -printf '%s %n\\n' \\) | sort"
    " && find . -type f -print0 | sort -z | xargs -0 -r sha256sum"
)


def listing(volume: str, image: str, registry: Registry) -> list[str]:
    """Every entry with type, mode, owner, size, link count, nanosecond mtime
    and a content hash; for comparing two volumes."""
    name = registry.add("container", opaque_name())
    try:
        proc = run(["run", "--rm", "--security-opt", "seccomp=builtin", "--name", name, "--network", "none", "--user", "0", "--entrypoint", "sh", "--mount", f"type=volume,src={volume},dst=/v,readonly,volume-nocopy", image, "-c", LISTING], check=False, timeout=600)
    finally:
        registry.remove("container", name)
    if proc.returncode != 0:
        raise DockerError(f"volume listing failed: {proc.stderr[-400:]}")
    return proc.stdout.splitlines()


def worker(volume: str, image: str, registry: Registry, *, path: str, uid: int = AGENT_UID) -> Container:
    """A long-lived helper with the volume at `path`, as the agent's user, no
    network: for Git work on a clone. The caller removes it."""
    name = registry.add("container", opaque_name())
    run(["run", "-d", "--security-opt", "seccomp=builtin", "--name", name, "--network", "none", "--user", f"{uid}:{uid}", "--entrypoint", "sleep",
         "--mount", f"type=volume,src={volume},dst={path},volume-nocopy", image, "infinity"], timeout=120)
    return Container(name, image)


# Every git directory under /x: a directory holding a HEAD file, an
# objects/ directory and a refs/ directory (working-tree `.git` directories,
# bare repositories, submodule directories under `.git/modules`). Nothing is
# pruned, so a task directory that happens to be named `objects` is
# searched like any other and a repository under it is found. Each
# repository's every object, loose and packed, is written decoded to
# stdout. One "R" on stderr per repository; a failed read exits 3.
GIT_OBJECTS = (
    "find /x -type f -name HEAD -exec sh -c '"
    "for h; do d=${h%/HEAD}; [ -d \"$d/objects\" ] && [ -d \"$d/refs\" ] || continue; printf R >&2; "
    "git -c \"safe.directory=*\" --git-dir=\"$d\" cat-file --batch-all-objects --batch || { printf \" failed %s\" \"$d\" >&2; exit 3; }; "
    "done' _ {} +"
)


def _scan_result(res: dict) -> dict:
    err = res.get("stderr", "")
    return {"repositories": err.count("R") if "failed" not in err else None, "bytes": res["bytes"], "hits": res["hits"],
            "exit": res["exit"], "killed": res["killed"], "stderr": err if "failed" in err or res["exit"] != 0 else "",
            "ok": res["exit"] == 0 and not res["killed"] and not any(res["hits"].values())}


def scan_git_objects_tar(tar_bytes: bytes, image: str, registry: Registry, needles: dict[str, bytes]) -> dict:
    """Unpack a workspace tar in a throwaway container (no network) and scan
    every git object of every repository in it, decoded. Nothing of the
    stream is stored. `ok` is False on a hit or on any read failure."""
    from .docker import scan_with_input

    name = registry.add("container", opaque_name())
    try:
        res = scan_with_input(["run", "--rm", "--security-opt", "seccomp=builtin", "-i", "--name", name, "--network", "none", "--user", "0", "--entrypoint", "sh", image,
                               "-c", "set -e; mkdir -p /x; tar -x -f - -C /x; " + GIT_OBJECTS], tar_bytes, needles, timeout=900)
    finally:
        registry.remove("container", name)
    return _scan_result(res)


def scan_git_objects_volume(vol: str, image: str, registry: Registry, needles: dict[str, bytes]) -> dict:
    """The same scan over a volume, mounted read-only."""
    from .docker import scan_command

    name = registry.add("container", opaque_name())
    try:
        res = scan_command(["run", "--rm", "--security-opt", "seccomp=builtin", "--name", name, "--network", "none", "--user", "0", "--entrypoint", "sh",
                            "--mount", f"type=volume,src={vol},dst=/x,readonly,volume-nocopy", image, "-c", GIT_OBJECTS], needles, timeout=900)
    finally:
        registry.remove("container", name)
    return _scan_result(res)


def scan_files(vol: str, image: str, registry: Registry, key) -> dict:
    """Every file of a volume, as a tar stream read member by member: each
    member's bytes, and one decoding level down (see docker.decoded_key_hits)."""
    from .docker import scan_tar_stream

    name = registry.add("container", opaque_name())
    try:
        return scan_tar_stream(["run", "--rm", "--security-opt", "seccomp=builtin", "--name", name, "--network", "none", "--user", "0", "--entrypoint", "tar",
                                "--mount", f"type=volume,src={vol},dst=/v,readonly,volume-nocopy", image, "-c", "-f", "-", "-C", "/v", "."], key)
    finally:
        registry.remove("container", name)
