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


def started_at(pid: int) -> float | None:
    """When a process started, as a Unix time; None where /proc is not there
    (Windows) or the process is gone."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[1].split()
        ticks = int(fields[19])                 # starttime, field 22 of stat
        with open("/proc/stat") as fh:
            boot = next(int(line.split()[1]) for line in fh if line.startswith("btime"))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration, AttributeError):
        return None


def alive_pid(pidfile: Path) -> int | None:
    """The pid recorded in pidfile if that process is still running, else None.
    A stale file is left in place; callers decide whether to claim it.

    A pid file outlives its process, and in a container the numbers start
    again from 1 after every restart, so the number in an old file often
    belongs to some other live process. A process that started after the
    file was written cannot be the one that wrote it: that file is stale."""
    pidfile = Path(pidfile)
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    if not is_alive(pid):
        return None
    began = started_at(pid)
    if began is not None:
        try:
            if began > pidfile.stat().st_mtime + 2:
                return None
        except OSError:
            return None
    return pid


def group_kwargs() -> dict:
    """Popen arguments that give a child its own process group, so it and
    everything it starts can be stopped together without touching the parent:
    a new session on POSIX, a new process group (and no console window) on
    Windows."""
    if os.name == "nt":
        return {"creationflags": SPAWN_FLAGS}
    return {"start_new_session": True}


def kill_group(pid: int, hard: bool = True) -> None:
    """Stop a child started with group_kwargs() and everything it started (a
    headless browser, a shard). Never the caller's own group."""
    if not pid or pid <= 0:
        return
    if os.name == "nt":
        import subprocess
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=0x08000000)
        return
    import signal
    try:
        if os.getpgid(pid) == os.getpgid(0):
            os.kill(pid, signal.SIGKILL if hard else signal.SIGTERM)
            return
    except (OSError, ProcessLookupError):
        pass
    try:
        os.killpg(pid, signal.SIGKILL if hard else signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def kill_tree(pid: int) -> None:
    """Kill a process and its children, on either platform.

    Only ever called on a pid confirmed alive. On Windows taskkill /T walks the
    tree. On POSIX the process group is the tree, except when it is the
    caller's own group: then only the process itself is signalled, so stopping
    the worker can never take the web server down with it. A process that
    ignores SIGTERM for five seconds gets SIGKILL."""
    if not is_alive(pid):
        return
    if os.name == "nt":
        import subprocess
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=0x08000000)
        return
    import signal
    import time
    try:
        pgid = os.getpgid(pid)
        own = pgid == os.getpgid(0)
    except (OSError, ProcessLookupError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if own:
                os.kill(pid, sig)
            else:
                os.killpg(pgid, sig)
        except (OSError, ProcessLookupError):
            return
        for _ in range(50):
            if not is_alive(pid):
                return
            time.sleep(0.1)


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
            holder = alive_pid(pidfile)
            if holder == os.getpid():
                return True             # a file from before a restart that reused our pid
            if holder is not None:
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
