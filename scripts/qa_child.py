"""A stand-in job for the worker's tests (scripts/qa_jobs.py, qa_requirements).

    python -m scripts.qa_child ok                print a line and exit 0
    python -m scripts.qa_child fail              print to stderr and exit 1
    python -m scripts.qa_child sleep N           sleep N seconds
    python -m scripts.qa_child spawn PIDFILE N   start a grandchild that sleeps N
                                                 seconds, write its pid, then sleep
    python -m scripts.qa_child runlog N          open a run_log row, then sleep N
                                                 (the worker must close it)
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    what = sys.argv[1] if len(sys.argv) > 1 else "ok"
    if what == "ok":
        print("stand-in job did its work")
        return 0
    if what == "fail":
        print("stand-in job failed on purpose", file=sys.stderr)
        return 1
    if what == "sleep":
        time.sleep(float(sys.argv[2]))
        return 0
    if what == "spawn":
        child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({float(sys.argv[3])})"])
        Path(sys.argv[2]).write_text(str(child.pid))
        time.sleep(float(sys.argv[3]))
        return 0
    if what == "runlog":
        from prospect import db, runlog
        conn = db.connect()
        with runlog.Run(conn, "qa_child", "test", "qa"):
            time.sleep(float(sys.argv[2]))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
