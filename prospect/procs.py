"""Process liveness on Windows, done correctly.

The obvious idiom is a trap here:

    os.kill(pid, 0)          # DO NOT use this to test if a process is alive

On POSIX signal 0 is a permission probe that changes nothing. On Windows CPython
has no signals to send, so os.kill routes anything that is not a console control
event to TerminateProcess, and TerminateProcess(handle, 0) *terminates the
process* with exit code 0. The "check" is the kill. Worse, when the pid is gone
it raises SystemError rather than OSError, so the usual `except OSError` guard
does not catch it and the caller dies instead.

That combination silently took down the autopilot worker every time a second
copy checked whether the first one was running.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

if os.name == "nt":                 # wintypes does not import on POSIX
    from ctypes import wintypes

_STILL_ACTIVE = 259
_QUERY_LIMITED_INFORMATION = 0x1000

# Flags that only mean anything to CreateProcess. Passing creationflags at all
# raises ValueError on POSIX, so every spawn site uses this instead of a literal.
# CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP keeps background jobs from
# flashing a console window on Windows; on Linux there is no window to hide.
SPAWN_FLAGS = 0x08000200 if os.name == "nt" else 0


def is_alive(pid: int) -> bool:
    """True if pid names a running process. Opens a query-only handle, so it
    can never affect the process it is asking about."""
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        # On POSIX signal 0 really is the permission probe it looks like, and
        # EPERM means the process exists but belongs to somebody else.
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = k32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        # A process that genuinely exits with 259 reads as alive. Nothing here
        # exits with that code, and erring toward "alive" only ever costs us a
        # skipped respawn rather than a duplicate worker.
        return code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def alive_pid(pidfile: Path) -> int | None:
    """The pid recorded in pidfile if that process is still running, else None.
    A stale file is left in place; callers decide whether to claim it."""
    try:
        pid = int(Path(pidfile).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if is_alive(pid) else None


def kill_tree(pid: int) -> None:
    """Kill a process and its children, on either platform.

    Only ever called on a pid confirmed alive. On Windows taskkill /T walks the
    tree; on POSIX the process group is the tree, and uvicorn's supervisor leads
    its own group when started by a service manager."""
    if not is_alive(pid):
        return
    if os.name == "nt":
        import subprocess
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=0x08000000)
        return
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def claim_pidfile(pidfile: Path) -> bool:
    """Atomically become the single owner of pidfile.

    O_EXCL is the claim, so two processes racing from a clean start cannot both
    win. A file left behind by a killed process is taken over, but only after
    confirming its pid is really gone."""
    pidfile = Path(pidfile)
    for _ in range(3):
        try:
            fd = os.open(pidfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if alive_pid(pidfile) is not None:
                return False
            try:
                pidfile.unlink()
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        return True
    return False
