"""Per-invocation storage and cleanup for the dedicated measuring user."""

import json
import os
import re
import stat
import sys


def writable_roots():
    """Discover searchable writable directories, without following links.

    Process and kernel control files are not persistent storage. /dev is
    walked so shared memory and message queues are included.
    """
    mounts = []
    with open('/proc/self/mountinfo') as f:
        for row in f:
            fields = row.split()
            path = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
            if path == '/proc' or path.startswith('/proc/') or path == '/sys' or path.startswith('/sys/') or path == '/dev/pts':
                continue
            if os.path.isdir(path) and not os.statvfs(path).f_flag & os.ST_RDONLY:
                mounts.append(path)
    roots = []
    for mount in mounts:
        for directory, children, _ in os.walk(mount, followlinks=False):
            children[:] = [n for n in children if not os.path.ismount(os.path.join(directory, n))]
            if os.access(directory, os.W_OK | os.X_OK):
                roots.append(directory)
                children.clear()
    return roots


def clean_owned(roots):
    """Dedicated UID only. Remove its entries, never follow their links."""
    uid = os.getuid()

    def visit(directory):
        for entry in os.scandir(directory):
            info = entry.stat(follow_symlinks=False)
            if info.st_uid == uid:
                if stat.S_ISDIR(info.st_mode):
                    os.chmod(entry.path, 0o700, follow_symlinks=False)
                    visit(entry.path)
                    os.rmdir(entry.path)
                else:
                    os.unlink(entry.path)
            elif stat.S_ISDIR(info.st_mode) and os.access(entry.path, os.R_OK | os.X_OK):
                visit(entry.path)

    for root in roots:
        if os.path.isdir(root):
            visit(root)


def command(cmd, paths):
    # Development runs share the host user. They must not sweep that user's
    # other files. macOS keeps its existing restriction to the work area.
    if sys.platform == 'darwin':
        rules = ' '.join('(subpath ' + json.dumps(os.path.realpath(p)) + ')' for p in paths)
        profile = '(version 1) (allow default) (deny file-write*) (allow file-write* (literal "/dev/null") ' + rules + ')'
        return ['/usr/bin/sandbox-exec', '-p', profile, *cmd]
    return cmd
