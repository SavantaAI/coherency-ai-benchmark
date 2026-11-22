"""Private pipe entry point, with no metric command in argv or environment."""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from cohbench.watcher.metrics import serve

    serve(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4], kill_strays=bool(int(sys.argv[5])))
