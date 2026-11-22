"""Start as root only long enough to become two unprivileged users.

In the container image the entry point starts as root with exactly the
capabilities SETUID and SETGID. Before anything reads the workspace it:

1. forks the metric server, which drops to the metric user (default
   1500:1500) and serves metric runs over two pipes (metrics.serve);
2. drops itself to the watcher user (default 1000:1000) and runs the watcher.

Neither keeps any capability: setuid to a non-zero uid clears them, and
the container's no-new-privileges flag stops them coming back. The image
places the live workspace and the output directory under /srv/w, mode 0700
and owned by the watcher user, so the metric user can neither read nor
traverse either. The metric user builds each copy at the workspace's own
path (/work/project, a link in the image to its private directory under
/tmp), so an absolute link into the workspace resolves into the copy.
"""

from __future__ import annotations

import errno
import os
import stat
import sys

from .metrics import IsolatedExecutor, clean_env


def parse_user(spec: str) -> tuple[int, int]:
    u, _, g = spec.partition(":")
    return int(u), int(g or u)


def drop(uid: int, gid: int) -> None:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.getuid() != uid or os.geteuid() != uid or os.getgid() != gid or os.getegid() != gid:
        raise RuntimeError("privilege drop did not take")
    try:
        os.setuid(0)
    except PermissionError:
        return
    raise RuntimeError("privilege drop is reversible; refusing to continue")


def check_unreachable(uid: int, paths: list[str]) -> list[str]:
    """Paths the given user could reach (by mode bits along the way)."""
    bad = []
    for p in paths:
        cur = "/"
        for comp in [c for c in os.path.realpath(p).split("/") if c]:
            st = os.stat(cur)
            ok = True
            if st.st_uid == uid:
                ok = bool(st.st_mode & 0o100)
            else:
                ok = bool(st.st_mode & 0o001)  # the metric user has no shared group
            if not ok:
                break
            cur = os.path.join(cur, comp)
        else:
            bad.append(p)
    return bad


ACL_XATTRS = ("system.posix_acl_access", "system.posix_acl_default")


def check_children(root: str, out: str) -> None:
    """The second half of the layout check, as the watcher user (root
    without capabilities cannot search the private directory): both paths
    are real directories, reached without any link."""
    for name, p in (("root", root), ("out", out)):
        st = os.lstat(p)
        if not stat.S_ISDIR(st.st_mode) or os.path.realpath(p, strict=True) != p.rstrip("/"):
            raise RuntimeError(f"{name} {p!r} must be a directory reached without links")


def check_layout(root: str, out: str, watcher_uid: int, trusted_uids: tuple[int, ...] = (0,), children: bool = True) -> None:
    """The documented layout, and nothing else: the live tree and the
    output are plain directories directly inside one private directory
    (the image's /srv/w), which is a real directory owned by the watcher
    user, mode exactly 0700, with no ACL; every directory above it is owned
    by a trusted user (root) and writable by nobody else. Raises
    RuntimeError naming the first deviation. With children=False the two
    directories themselves are left to check_children."""
    def lst(p: str) -> os.stat_result:
        return os.lstat(p)

    for name, p in (("root", root), ("out", out)):
        if not os.path.isabs(p) or os.path.normpath(p) != p.rstrip("/") or p.rstrip("/") == "":
            raise RuntimeError(f"{name} {p!r} must be an absolute path without dot components")
    if children:
        check_children(root, out)
    w = os.path.dirname(root.rstrip("/"))
    if os.path.dirname(out.rstrip("/")) != w or root.rstrip("/") == out.rstrip("/"):
        raise RuntimeError(f"root and out must be two directories directly inside one private directory (got {root!r} and {out!r})")
    st = lst(w)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != watcher_uid or stat.S_IMODE(st.st_mode) != 0o700:
        raise RuntimeError(f"{w} must be a directory owned by uid {watcher_uid} with mode 0700 (is uid {st.st_uid}, mode {stat.S_IMODE(st.st_mode):04o})")
    listx = getattr(os, "listxattr", None)
    if listx is not None:
        try:
            names = listx(w, follow_symlinks=False)
        except OSError as e:
            # A filesystem without extended attributes cannot carry an ACL;
            # any other failure to look is a refusal.
            if e.errno not in (errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
                raise RuntimeError(f"cannot inspect {w} for an ACL: {e}") from e
            names = []
        acl = [n for n in names if n in ACL_XATTRS]
        if acl:
            raise RuntimeError(f"{w} carries an ACL ({acl}); the layout allows mode bits only")
    cur = os.path.dirname(w)
    while True:
        st = lst(cur)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid not in trusted_uids or st.st_mode & 0o022:
            raise RuntimeError(f"{cur} must be a directory owned by root and writable by nobody else (is uid {st.st_uid}, mode {stat.S_IMODE(st.st_mode):04o})")
        if cur == "/":
            break
        cur = os.path.dirname(cur)


def launch(root: str, out: str, need_metrics: bool, watcher_user: str, metric_user: str, workspace: str):
    """Returns the executor for the watcher (or None without metrics),
    having dropped this process to the watcher user."""
    if os.geteuid() != 0:
        raise RuntimeError("isolated metric runs need the watcher to start as root (with SETUID and SETGID only)")
    wu, wg = parse_user(watcher_user)
    mu, mg = parse_user(metric_user)
    if mu == wu or mg == wg or mu == 0 or wu == 0:
        raise RuntimeError("the watcher and metric users must be distinct and not root")
    check_layout(root, out, wu, children=False)
    exposed = check_unreachable(mu, [root, out])
    if exposed:
        raise RuntimeError(f"the metric user could reach {exposed}; mount them under a directory only the watcher user can enter")
    executor = None
    if need_metrics:
        to_r, to_w = os.pipe()
        from_r, from_w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(to_w)
                os.close(from_r)
                os.chdir("/")
                drop(mu, mg)
                work = os.path.join("/tmp", "m")
                os.makedirs(work, mode=0o700, exist_ok=True)
                os.chmod(work, 0o700)
                exec_server(to_r, from_w, workspace, os.path.join(work, "work"))
            except BaseException as e:  # never fall back into the watcher's code
                print(f"metric server: {type(e).__name__}: {e}", file=sys.stderr)
                os._exit(1)
            os._exit(0)
        os.close(to_r)
        os.close(from_w)
        executor = (to_w, from_r, pid)
    os.chdir("/")
    drop(wu, wg)
    check_children(root, out)
    if executor is None:
        return None
    to_w, from_r, pid = executor
    ex = IsolatedExecutor(to_w, from_r, blobs=None, workspace=workspace)
    ex.server_pid = pid
    return ex


def exec_server(in_fd, out_fd, workspace, work_dir, kill_strays=True):
    """Replace inherited argv and environment before receiving any commands."""
    os.set_inheritable(in_fd, True)
    os.set_inheritable(out_fd, True)
    script = os.path.join(os.path.dirname(__file__), "metric_server.py")
    os.execve(sys.executable, [sys.executable, script, str(in_fd), str(out_fd), workspace, work_dir, str(int(kill_strays))], clean_env(work_dir))
