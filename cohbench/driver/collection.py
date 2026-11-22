"""Read declared files as the agent user, returning data only to the host."""

import base64
import json

from cohbench.trajectory.collection import LIMIT

READ = '''
import base64, json, os, pwd, stat, sys
path = sys.argv[1]
if path.startswith("~/"):
    path = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, path[2:])
path = os.path.realpath(path)
limit = int(sys.argv[2])
if path == "/work/project" or path.startswith("/work/project/"):
    raise ValueError("collection path resolves inside the workspace")
try:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
except FileNotFoundError:
    print(json.dumps({"status": "absent", "bytes": 0, "data": None}))
else:
    with os.fdopen(fd, "rb") as f:
        opened = os.path.realpath("/proc/self/fd/" + str(f.fileno()))
        if opened == "/work/project" or opened.startswith("/work/project/"):
            raise ValueError("collection file is inside the workspace")
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("collection path is not a regular file")
        data = f.read(limit + 1)
    size = max(info.st_size, len(data))
    print(json.dumps({"status": "oversize" if size > limit else "collected", "bytes": size,
                      "data": base64.b64encode(data).decode() if size <= limit else None}))
'''


def read(container, path):
    proc = container.exec(["python3", "-c", READ, path, str(LIMIT)], user="node", check=False, timeout=60)
    if proc.returncode:
        raise ValueError("collection read failed")
    row = json.loads(proc.stdout)
    data = base64.b64decode(row.pop("data"), validate=True) if row.get("data") is not None else None
    row.pop("data", None)
    return row, data
