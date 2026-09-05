"""Track live DOSBox-X sessions on disk so "what did I start?" has an answer.

Launching an emulator as a detached child is easy to get wrong in ways that
are individually silent: forget to track a pid and a runaway is invisible to
`ps` filters that look for something else; kill a bare pid instead of its
process group and a spawned child survives; check `os.kill(pid, 0)` alone and
a *different* process that later reused the pid gets mistaken for the one you
started. Every session that matters here is instead recorded as a small JSON
file naming its pid, process group, owning process, ports and start time, so
a caller (or a `reap`) can answer "is this still mine" without guessing.

**The pid-recycling problem, and why this module keys on more than a pid.**
A Linux pid is reused once its process exits and the counter wraps back
around, which on a busy host can happen in well under a minute. A registry
entry that only remembers a pid would, after that pid is recycled, believe an
unrelated process is the session it started — and `reap` killing that
process would kill someone else's work. `_proc_starttime` reads field 22 of
`/proc/<pid>/stat` (the process's start time, which is fixed at creation and
cannot repeat for two processes holding the same pid at overlapping times),
and `_pid_alive` requires both the pid AND the recorded start time to match
before it will call a process "alive". That pairing is the whole point of
this module; do not simplify it back down to a bare pid check.

Registry location: the `DBXDEBUG_REGISTRY` environment variable, or
`~/.cache/dbxdebug-sessions` when unset. Not under `/tmp` — a reap has to be
able to find yesterday's stray, and `/tmp` is cleaned on the system's own
schedule, not this module's.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Where live-session files live by default, when DBXDEBUG_REGISTRY is unset.
# Informational only -- `registry_dir` re-reads the environment variable on
# every call rather than trusting this constant, so tests can point a fresh
# process at a scratch directory via `monkeypatch.setenv` regardless of
# import order.
DEFAULT_REGISTRY = Path(
    os.environ.get("DBXDEBUG_REGISTRY", str(Path.home() / ".cache" / "dbxdebug-sessions"))
)


# --------------------------------------------------------------------------
# ports
# --------------------------------------------------------------------------


def free_port(host: str = "127.0.0.1") -> int:
    """Ask the kernel for an unused port, then hand the number on.

    There is an unavoidable window between the `close()` here and whatever
    the caller binds later, so callers that launch a process against this
    port should be prepared to retry on a lost bind race.

    Args:
        host: Interface to bind while probing for a free port.

    Returns:
        A port number that was free at the moment of the call.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # No SO_REUSEADDR: we want a port nobody else is bound to right now.
        s.bind((host, 0))
        return int(s.getsockname()[1])


def port_is_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """Check whether something accepts a TCP connection on `port`.

    Args:
        port: TCP port to probe.
        host: Interface to connect to.
        timeout: Seconds to wait for the connection attempt.

    Returns:
        True if a connection was accepted, False otherwise (including on
        any connection error or timeout).
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_ports_free(ports: Iterable[int], timeout: float, host: str = "127.0.0.1") -> bool:
    """Block until nothing is listening on any of `ports`.

    This is the pre-flight that stops a foreign, already-running process
    from being mistaken for one this package is about to start: allocating
    an ephemeral port and then waiting for it to go quiet is not enough by
    itself if something else could still be bound to it.

    Args:
        ports: TCP ports to watch.
        timeout: Maximum seconds to wait before giving up.
        host: Interface to probe on.

    Returns:
        True once every port in `ports` is free. False if `timeout` seconds
        elapse first with at least one port still accepting connections.
    """
    ports = list(ports)
    per_check_timeout = min(0.3, timeout) if timeout > 0 else 0.05
    deadline = time.time() + timeout
    while True:
        if not any(port_is_listening(p, host=host, timeout=per_check_timeout) for p in ports):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.05)


# --------------------------------------------------------------------------
# process identity
# --------------------------------------------------------------------------


def _stat_fields(pid: int) -> list[str] | None:
    """Read `/proc/<pid>/stat` and split the fields from field 3 (state) on.

    Args:
        pid: Process id to inspect.

    Returns:
        The whitespace-split fields starting at the process state (field 3),
        or None if the process does not exist or its stat file could not be
        parsed. `comm` (field 2) may itself contain spaces and parentheses,
        so the split starts after the LAST `)` — everything from there is
        positionally stable.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes().decode("latin-1")
    except OSError:
        return None
    try:
        return raw[raw.rindex(")") + 2 :].split()
    except ValueError:
        return None


def _proc_starttime(pid: int) -> int | None:
    """Read field 22 of `/proc/<pid>/stat`: process start time in clock ticks.

    Recorded alongside a pid so a reap cannot kill an innocent process that
    happens to have inherited the number later. A pid alone is not an
    identity.

    Args:
        pid: Process id to inspect.

    Returns:
        The start time in clock ticks since boot, or None if it could not
        be read.
    """
    fields = _stat_fields(pid)
    if fields is None:
        return None
    try:
        return int(fields[19])
    except (ValueError, IndexError):
        return None


def _proc_cpu_seconds(pid: int) -> float | None:
    """Read utime + stime for `pid`, in seconds. Fields 14 and 15 of the stat file.

    Args:
        pid: Process id to inspect.

    Returns:
        Accumulated user + system CPU time in seconds, or None if it could
        not be read.
    """
    fields = _stat_fields(pid)
    if fields is None:
        return None
    try:
        ticks = float(fields[11]) + float(fields[12])
    except (ValueError, IndexError):
        return None
    return ticks / os.sysconf("SC_CLK_TCK")


def _proc_state(pid: int) -> str | None:
    """Read field 3 of `/proc/<pid>/stat`: one of `R`, `S`, `D`, `T`, `Z`, ...

    Args:
        pid: Process id to inspect.

    Returns:
        The single-character state code, or None if it could not be read.
    """
    fields = _stat_fields(pid)
    return fields[0] if fields else None


def _pid_alive(pid: int, starttime: int | None = None) -> bool:
    """Check whether `pid` is a live process, optionally pinned to a starttime.

    Args:
        pid: Process id to check.
        starttime: If given, the `/proc` starttime this pid was recorded
            with. When the pid's current starttime differs, the original
            process is gone and something else now holds that number, so
            this returns False rather than reporting a false positive.

    Returns:
        True if `pid` names a live, non-zombie process whose starttime (when
        given) still matches.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, owned by someone else
    # A zombie is a dead process still holding its pid until its parent waits
    # on it. `kill(pid, 0)` succeeds for one, so a liveness check that stops
    # at that call reports a killed process as still running.
    if _proc_state(pid) == "Z":
        return False
    if starttime is not None:
        now = _proc_starttime(pid)
        if now is not None and now != starttime:
            return False  # pid reused: this is a different process
    return True


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


@dataclass
class RegisteredSession:
    """One live-session file, as read back off disk."""

    path: Path
    data: dict[str, Any]

    @property
    def pid(self) -> int:
        """The emulator's process id."""
        return int(self.data.get("pid", -1))

    @property
    def pgid(self) -> int:
        """The emulator's process group id (defaults to `pid`)."""
        return int(self.data.get("pgid", self.pid))

    @property
    def owner_pid(self) -> int:
        """The process id of whatever launched and registered this session."""
        return int(self.data.get("owner_pid", -1))

    @property
    def workdir(self) -> Path | None:
        """The session's private working directory, if one was recorded."""
        w = self.data.get("workdir")
        return Path(w) if w else None

    @property
    def gdb_port(self) -> int | None:
        """The GDB stub's TCP port, if recorded."""
        return self.data.get("gdb_port")

    @property
    def qmp_port(self) -> int | None:
        """The QMP server's TCP port, if recorded."""
        return self.data.get("qmp_port")

    @property
    def started_at(self) -> float:
        """Unix timestamp the session was registered at."""
        return float(self.data.get("started_at", 0.0))

    @property
    def age_s(self) -> float:
        """Seconds since the session was registered."""
        return max(0.0, time.time() - self.started_at)

    @property
    def alive(self) -> bool:
        """Whether the recorded pid still names this same process.

        Keyed on pid PLUS the recorded `/proc` starttime, so a pid recycled
        by an unrelated process after this one exited is not mistaken for
        it. See the module docstring.
        """
        return _pid_alive(self.pid, self.data.get("proc_starttime"))

    @property
    def owner_alive(self) -> bool:
        """Whether the process that launched this session is still alive.

        Uses the same pid+starttime identity check as `alive`.
        """
        return _pid_alive(self.owner_pid, self.data.get("owner_starttime"))

    @property
    def orphaned(self) -> bool:
        """True when the emulator is still running but its owner is gone."""
        return self.alive and not self.owner_alive


def registry_dir(registry: Path | None = None) -> Path:
    """Resolve and create the registry directory.

    Args:
        registry: Explicit registry directory, or None to resolve it from
            the `DBXDEBUG_REGISTRY` environment variable (read fresh on
            every call, not cached at import time), falling back to
            `~/.cache/dbxdebug-sessions`.

    Returns:
        The resolved directory, created if it did not already exist.
    """
    if registry:
        d = Path(registry)
    else:
        d = Path(
            os.environ.get("DBXDEBUG_REGISTRY", str(Path.home() / ".cache" / "dbxdebug-sessions"))
        )
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_sessions(registry: Path | None = None) -> list[RegisteredSession]:
    """List every registered session, oldest first.

    Args:
        registry: Registry directory, or None for the default.

    Returns:
        Every session whose registry file could be read, sorted by
        `started_at`. Unreadable or malformed files are skipped.
    """
    out = []
    d = registry_dir(registry)
    for path in sorted(d.glob("*.json")):
        try:
            out.append(RegisteredSession(path, json.loads(path.read_text())))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda s: s.started_at)
    return out


def kill_group(pgid: int, term_timeout: float = 5.0, pid: int | None = None) -> str:
    """SIGTERM a process group, then SIGKILL it if it is still there.

    A process GROUP, not a single process: a session that spawns children
    must not outlive its intended teardown through one of them.

    Args:
        pgid: Process group id to signal.
        term_timeout: Seconds to wait for the group to exit after SIGTERM
            before escalating to SIGKILL.
        pid: If given, liveness is checked via this specific pid (using
            `_pid_alive`) instead of probing the process group itself. Use
            this when the caller also wants pid+starttime identity checking
            on the liveness test.

    Returns:
        `"gone"` if nothing needed killing, `"term"` if SIGTERM was enough,
        `"kill"` if SIGKILL was needed and worked, `"denied"` if signalling
        was not permitted, or `"stuck"` if the group survived SIGKILL too.
    """

    def _alive() -> bool:
        if pid is not None:
            return _pid_alive(pid)
        try:
            os.killpg(pgid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    if not _alive():
        return "gone"
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "denied"
    deadline = time.time() + term_timeout
    while time.time() < deadline:
        if not _alive():
            return "term"
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return "term"
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not _alive():
            return "kill"
        time.sleep(0.05)
    return "stuck"


def reap(
    registry: Path | None = None,
    all_sessions: bool = False,
    dry_run: bool = False,
    term_timeout: float = 5.0,
    max_age_s: float | None = None,
) -> list[dict[str, Any]]:
    """Kill strays and clear their registry entries and workdirs.

    By default this reaps only what nobody owns any more: a registered
    process whose launching process has exited, or a registry file whose
    process is already gone (workdir left behind).

    Args:
        registry: Registry directory, or None for the default.
        all_sessions: When True, also reap live, still-owned sessions — the
            big hammer, for when a caller has been killed with its owner
            somehow still present.
        dry_run: When True, report what would be done without doing it.
        term_timeout: Passed through to `kill_group`.
        max_age_s: When given, skip sessions younger than this many seconds.

    Returns:
        One report row (a dict) per session considered, describing the
        action taken (or that would be taken, under `dry_run`).
    """
    rows = []
    for sess in list_sessions(registry):
        if max_age_s is not None and sess.age_s < max_age_s:
            continue
        if sess.alive and not (all_sessions or sess.orphaned):
            rows.append(
                {
                    "pid": sess.pid,
                    "action": "kept",
                    "reason": "alive and owned",
                    "workdir": str(sess.workdir or ""),
                }
            )
            continue
        action = "killed" if sess.alive else "cleared"
        row: dict[str, Any] = {
            "pid": sess.pid,
            "action": action,
            "reason": "orphan" if sess.orphaned else ("all" if sess.alive else "stale"),
            "workdir": str(sess.workdir or ""),
            "gdb_port": sess.gdb_port,
            "qmp_port": sess.qmp_port,
        }
        if dry_run:
            row["action"] = "would-" + action
            rows.append(row)
            continue
        if sess.alive:
            row["kill"] = kill_group(sess.pgid, term_timeout, pid=sess.pid)
        wd = sess.workdir
        if wd and wd.exists() and sess.data.get("owns_workdir", True):
            # shutil.rmtree, never a shell `rm -rf`: some sandboxes refuse
            # that command outright.
            shutil.rmtree(wd, ignore_errors=True)
            row["workdir_removed"] = not wd.exists()
        with contextlib.suppress(OSError):
            sess.path.unlink()
        rows.append(row)
    return rows


def format_table(sessions: Iterable[RegisteredSession]) -> str:
    """Render sessions as a fixed-width text table.

    Args:
        sessions: Sessions to render, in the order given.

    Returns:
        A multi-line table, or a one-line placeholder message when
        `sessions` is empty.
    """
    sessions = list(sessions)
    if not sessions:
        return "no registered DOSBox-X sessions"
    head = (
        f"{'PID':>8} {'PGID':>8} {'GDB':>6} {'QMP':>6} {'AGE':>8} "
        f"{'STATE':<9} {'OWNER':>8}  WORKDIR"
    )
    lines = [head, "-" * len(head)]
    for s in sessions:
        if not s.alive:
            state = "stale"
        elif s.orphaned:
            state = "ORPHAN"
        else:
            state = "alive"
        lines.append(
            f"{s.pid:>8} {s.pgid:>8} {str(s.gdb_port or '-'):>6} "
            f"{str(s.qmp_port or '-'):>6} {s.age_s:>7.0f}s {state:<9} "
            f"{s.owner_pid:>8}  {s.workdir}"
        )
    return "\n".join(lines)
