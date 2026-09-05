"""Launch one DOSBox-X instance with dynamic ports, an isolated workdir, and guaranteed teardown.

WHY THIS EXISTS. Hand-rolling a `subprocess.Popen` around DOSBox-X per
caller means hand-rolling cleanup too, and cleanup done N different times is
cleanup done wrong at least once. The costs this module exists to avoid have
all been measured directly:

  * a QMP connect lost a bind race roughly 1 trial in 27 when the port was
    fixed and the connect had no retry -- ephemeral port allocation makes
    the race window small, and `DosboxSession` retrying a failed launch with
    FRESH ports is what makes losing it survivable anyway;
  * a scratch workdir cleaned up with a shell `rm -rf` gets orphaned outright
    in sandboxes that refuse that command; `shutil.rmtree` called from
    Python does not have that problem;
  * a launcher that only tracks a bare pid cannot tell "my emulator, still
    running" from "someone else's process that happens to have inherited
    the number" -- see `registry.py`, which this module registers into.

This class is deliberately the ONLY sanctioned way to launch or talk to
DOSBox-X in this package. If a caller needs a capability this does not have,
add it here rather than hand-rolling a parallel `Popen` -- every launcher
written around this one is work that has to be redone if the launch and
teardown logic ever has to change, and every capability added outside it is
capability every other caller does not get.

This harness cannot work against stock DOSBox-X: it depends on the
gdbserver and qmpserver remote-debug features, which are additions to this
project's fork and are not upstream.

The upstream `dosbox_debug.DOSBoxInstance.start()` does not solve this and
cannot: it opens with `pkill -9 -f dosbox-x`, which kills every *other*
caller's emulator too, and it keeps the fixed 2159/4444 ports with no
workdir isolation and no record of what it started. That command is the
origin of the rule this package follows everywhere instead: **never**
`pkill -f dosbox-x`. Reap strays through `registry.reap`, which only ever
touches a process it can identify by pid and `/proc` starttime.

CONCURRENCY, as measured on a 16-core host with
`probe_concurrency.py --levels 1,2,4,8 --rounds 2`:

    level   sessions   booted   median boot   emulated/wall
      1         2        2/2       2.12 s        1.0068
      2         4        4/4       2.12 s        1.0069
      4         8        8/8       2.14 s        1.0069
      8        16       16/16      2.25 s        1.0068

Eight sessions at once, every one reaching the DOS prompt, with boot time up
6% and no throughput cost: there is no single-instance guard, SDL's video
and audio devices do not serialise instances, and nothing fought over a
port. What that run does NOT establish, and the distinction matters: those
guests sat IDLE at the prompt, and an idle DOSBox-X costs almost nothing,
because it detects the keyboard wait and stops burning host CPU. With a
program actually running, one instance measured 0.19 host cores -- so 8
concurrent captures are plausible on 16 cores and are still unmeasured;
`cycles = max`, which by construction takes what it can get, is unmeasured
too. Treat 8 as a floor established for launching, not a ceiling
established for working.

USAGE::

    from dbxdebug.session import DosboxSession

    with DosboxSession(program=Path("PROG.EXE"), mounts={"c": src_dir}) as s:
        s.gdb_port, s.qmp_port, s.pid, s.workdir   # the handle
        s.gdb.read_registers()                     # connected clients
        s.qmp.type_text("PROG\\r")

Everything the session created dies with the `with` block: the emulator and
any child it spawned (process-GROUP kill, SIGTERM then SIGKILL), the private
workdir (`shutil.rmtree` from Python, never a shell `rm`), and the registry
file. `atexit` and, by default, SIGINT/SIGTERM handlers repeat that teardown
for a process that leaves the `with` block by some other door -- three
independent paths converging on the same idempotent `stop()`.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, TypeVar

from .addressing import linear
from .gdb import GDBClient, IncompatibleStubError
from .paths import DOSBOX_X_ENV_VAR, configured_dosbox_x_path
from .qmp import QMPClient
from .registry import (
    _proc_starttime,
    free_port,
    kill_group,
    list_sessions,
    port_is_listening,
    registry_dir,
)
from .video import decode_text_screen

# `[sdl] output`. NOT `opengl`: measured back to back on an idle host,
# `opengl` accumulates emulated time roughly 8.6x more slowly than `surface`
# for the same work per emulated second, which turns any wall-clock bound
# into a throughput failure rather than a rendering choice.
DEFAULT_SDL_OUTPUT = "surface"

# `{gdb_port}`, `{qmp_port}`, `{workdir}`, `{autoexec}`, `{cycles}` and
# `{sdl_output}` are substituted by `render_conf`. Note the SPACE in
# "gdbserver port" / "qmpserver port" -- those are the actual DOSBox-X conf
# key names. "gdbport"/"qmpport" (no space) are silently ignored by
# DOSBox-X, which leaves the server on its compiled-in default port and
# makes a session connect to whatever else happens to be listening there.
DEFAULT_CONF = """\
[dosbox]
gdbserver = {gdbserver}
gdbserver port = {gdb_port}
qmpserver = true
qmpserver port = {qmp_port}
quit warning = false

[sdl]
output = {sdl_output}
fullscreen = false

[cpu]
core = normal
cputype = 386
cycles = {cycles}

[autoexec]
{autoexec}
"""

_TOKEN = re.compile(r"\{(\w+)\}")

# Bound to either debug client, so `_connect_with_retry` can hand back the
# exact type its factory builds instead of a lossy `GDBClient | QMPClient`.
_ClientT = TypeVar("_ClientT", GDBClient, QMPClient)


class DosboxLaunchError(RuntimeError):
    """The emulator never came up in a usable state."""


def render_conf(template: str, ctx: Mapping[str, Any]) -> str:
    """Substitute `{name}` for known keys only, leaving other braces alone.

    Args:
        template: Conf text containing `{name}` placeholders.
        ctx: Mapping of placeholder name to substitution value.

    Returns:
        `template` with every `{name}` where `name` is a key of `ctx`
        replaced by `str(ctx[name])`. A brace whose name is not in `ctx` is
        left exactly as written -- `str.format` would raise on any literal
        brace a caller's own conf text happens to contain, and a conf is not
        the place to make callers double their braces.
    """

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        return str(ctx[key]) if key in ctx else m.group(0)

    return _TOKEN.sub(sub, template)


# --------------------------------------------------------------------------
# teardown of our own sessions, from anywhere
# --------------------------------------------------------------------------

# Every live session, keyed by its own `_key`, so a signal handler or atexit
# hook running in a completely different call stack can still find and stop
# it.
_LIVE: dict[str, DosboxSession] = {}
_LIVE_LOCK = threading.Lock()
_HOOKS_INSTALLED = False


def _reap_live(*_args: object) -> None:
    """Stop every still-registered session. Used as an atexit/signal hook."""
    with _LIVE_LOCK:
        sessions = list(_LIVE.values())
    for sess in sessions:
        with contextlib.suppress(Exception):
            sess.stop()


def _install_hooks() -> None:
    """Install atexit and SIGINT/SIGTERM teardown hooks exactly once.

    `atexit` alone does not fire on a signal, and a signal handler alone
    does not fire on a normal return. Both of those, plus `__exit__`, are
    three independent paths to the same `stop()`; a session survives only a
    SIGKILL of its owner, which `registry.reap` exists to clean up after the
    fact.
    """
    global _HOOKS_INSTALLED
    if _HOOKS_INSTALLED:
        return
    _HOOKS_INSTALLED = True
    atexit.register(_reap_live)
    if threading.current_thread() is not threading.main_thread():
        # Python only lets the main thread install signal handlers.
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous = signal.getsignal(sig)
        except (ValueError, OSError):
            continue

        def handler(signum: int, frame: object, _prev: Any = previous) -> None:
            _reap_live()
            if callable(_prev) and _prev not in (signal.SIG_IGN, signal.SIG_DFL):
                _prev(signum, frame)
            elif _prev == signal.SIG_IGN:
                return
            else:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handler)


# --------------------------------------------------------------------------
# the session
# --------------------------------------------------------------------------


@dataclass
class DosboxSession:
    """One isolated DOSBox-X instance, with its teardown guaranteed.

    Parameters that matter:

    `conf`        conf text, or a Path to a conf file, used as a template.
                  `{gdb_port}`, `{qmp_port}`, `{workdir}`, `{autoexec}`,
                  `{cycles}` and `{sdl_output}` are substituted. Default:
                  `DEFAULT_CONF`.
    `mounts`      `{"c": host_path}` -> `mount c <path>` in `[autoexec]`. If
                  omitted, an empty per-session `workdir/c` is mounted as C:,
                  so no two sessions ever share a path.
    `program`     a host file copied onto the first mounted drive, or a bare
                  guest name. Recorded on the handle; NOT auto-run, because a
                  break-on-exec caller has to arm before the exec happens.
    `files`       further host files copied onto that same drive. An entry
                  may be a path, or a `(host_path, guest_name)` pair to stage
                  it under a different name. The session owns them, so a
                  caller no longer needs its own tempdir (and its own
                  cleanup) just to put a couple of files in front of the
                  guest.
    `connect`     connect GDB and QMP once both ports accept. False for
                  callers that want the handle only.
    """

    conf: str | Path | None = None
    mounts: Mapping[str, str | Path] | None = None
    program: str | Path | None = None
    files: Iterable[str | Path | tuple[str | Path, str]] | None = None
    autoexec: str | Iterable[str] | None = None
    cycles: str = "max"
    sdl_output: str = DEFAULT_SDL_OUTPUT
    executable: str | Path = field(default_factory=configured_dosbox_x_path)
    env: Mapping[str, str] | None = None
    connect: bool = True
    # Whether the conf turns DOSBox-X's gdbserver on. `False` means no
    # gdbserver line is expected in the conf: `start()` waits for the QMP
    # port only and connects no GDB client.
    #
    # This exists because measuring what the GDB stub COSTS the guest
    # requires an arm that runs without it -- the stub is polled from the
    # emulation thread, so "gdbserver on" is not a free observation. Without
    # this flag, a conf with no gdbserver line would still have to satisfy a
    # startup wait that polls BOTH ports, so the launch would time out after
    # `startup_timeout` seconds and that arm would be unrunnable.
    gdbserver: bool = True
    # Wall seconds to wait AFTER the debug ports accept and the clients
    # connect, before `start()` hands back the handle.
    #
    # The ports accept as soon as DOSBox-X has opened its listeners, which
    # is well before the guest has finished booting DOS. A caller that types
    # at the guest immediately -- `qmp.type_text("PROG\r")` with
    # break-on-exec armed -- sends those keystrokes into a machine that is
    # not at a prompt yet, and they are simply lost. The capture then runs
    # to completion against whatever IS on screen and exits 0.
    #
    # Every hand-rolled launcher this class is meant to replace slept
    # roughly 2.5 seconds between spawning the process and its first
    # connect. That sleep IS this parameter, and skipping it is not
    # theoretical: converting callers onto a shared session without it made
    # them capture 24 frames of the DOSBox-X welcome banner and pass their
    # own content gate anyway, because the banner is text and a
    # non-blank-cell threshold cannot tell it apart from the program under
    # test. Default non-zero for that reason: a caller that has not thought
    # about boot timing gets safe behaviour by default. `assert_screen_readable`
    # is the belt-and-suspenders check for a caller that reads the screen
    # right after `start()` returns.
    boot_settle: float = 2.5
    # Wall seconds to wait for BOTH debug ports to accept.
    startup_timeout: float = 30.0
    # Wall seconds for each client's connect retry loop, after readiness.
    #
    # This package's `GDBClient`/`QMPClient` connect (and, for GDB, complete
    # the capability handshake) inside `__init__`, so this bounds RETRIES of
    # a refused connection, not a hung one. `GDBClient` arms its own read
    # timeout (`gdb.DEFAULT_TIMEOUT`), so a GDB stub that accepts the TCP
    # connection and never answers the handshake fails one attempt rather
    # than blocking. `QMPClient` still has no timeout of its own, so the
    # same stall on the QMP side can still block past this deadline.
    connect_timeout: float = 30.0
    # How many times a failed launch is retried with FRESH ports. The bind
    # race documented in the module docstring is why this is not zero.
    port_retries: int = 3
    term_timeout: float = 5.0
    registry: Path | None = None
    workdir: Path | None = None
    keep_workdir: bool = False
    install_hooks: bool = True
    log: str | Path | None = None
    label: str = "dosbox"

    # --- filled in by start() ---
    gdb_port: int | None = field(default=None, init=False)
    qmp_port: int | None = field(default=None, init=False)
    pid: int | None = field(default=None, init=False)
    proc: subprocess.Popen | None = field(default=None, init=False)
    conf_path: Path | None = field(default=None, init=False)
    gdb: GDBClient | None = field(default=None, init=False)
    qmp: QMPClient | None = field(default=None, init=False)
    program_name: str | None = field(default=None, init=False)
    drive_paths: dict[str, Path] = field(default_factory=dict, init=False)
    attempts: int = field(default=0, init=False)

    _owns_workdir: bool = field(default=False, init=False)
    _registry_path: Path | None = field(default=None, init=False)
    _key: str = field(default="", init=False)
    _stopped: bool = field(default=False, init=False)
    _log_fh: Any = field(default=None, init=False)

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> DosboxSession:
        """Start the emulator and return self."""
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        """Tear the emulator down unconditionally. Never suppresses an exception."""
        self.stop()
        return False

    def start(self) -> DosboxSession:
        """Launch DOSBox-X, wait for it to become usable, and register it.

        Returns:
            self, for `session = DosboxSession(...).start()` as an
            alternative to the context-manager form.

        Raises:
            DosboxLaunchError: If the emulator did not come up within
                `port_retries + 1` attempts.
        """
        if self.install_hooks:
            _install_hooks()
        self._key = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        # Registered into `_LIVE` immediately, before anything is spawned --
        # not deferred to `_register()`, which only runs after the process
        # is up, both clients are connected, and `boot_settle` has elapsed.
        # That window is several seconds long, and a SIGTERM arriving inside
        # it would otherwise find `_LIVE` empty: `_reap_live()` would have
        # nothing to stop, the handler would fall through to the previous
        # (default) disposition, and the owner would die with the emulator
        # already running but recorded nowhere -- invisible to `stop()` AND
        # to `registry.reap`, since the on-disk registry file is written
        # even later than this. `stop()` already tolerates being called
        # before `proc`/`_registry_path` exist, so registering this early is
        # safe.
        with _LIVE_LOCK:
            _LIVE[self._key] = self
        try:
            self._make_workdir()
        except Exception:
            # Staging can fail (a missing file, an unreadable mount) after
            # the workdir already exists. Nothing has been launched yet, but
            # the directory is already ours, and `self` is already in
            # `_LIVE` -- `stop()` (not just `_cleanup_workdir()`) is what
            # unwinds both.
            self.stop()
            raise
        exe = Path(self.executable)
        if not exe.is_file():
            # Fail once, clearly, before burning port_retries+1 attempts on
            # a failure mode that will not change between attempts. This is
            # also where an explicit-but-wrong DBXDEBUG_DOSBOX surfaces: it
            # is trusted as-is by `configured_dosbox_x_path()` rather than
            # silently replaced by a PATH binary, so the failure has to
            # happen here instead.
            self.stop()
            raise DosboxLaunchError(
                f"dosbox-x executable not found: {exe} "
                f"(set {DOSBOX_X_ENV_VAR}, place a build at the conventional "
                "path, or add dosbox-x to PATH)"
            )
        last: Exception | None = None
        # BaseException, not Exception: a KeyboardInterrupt or a socket
        # error from the port allocator is just as capable of leaving a
        # half-built session behind as any ordinary exception.
        try:
            for attempt in range(self.port_retries + 1):
                self.attempts = attempt + 1
                try:
                    self._spawn()
                    if self.connect:
                        self._connect_clients()
                    if self.boot_settle:
                        # See `boot_settle`: the ports accept long before
                        # the guest is at a prompt.
                        time.sleep(self.boot_settle)
                    self._register()
                    return self
                except DosboxLaunchError as exc:
                    last = exc
                    self._kill_process()
                    if attempt < self.port_retries:
                        # A lost bind race, a port taken between allocation
                        # and bind, or an emulator that died on its own: all
                        # three look the same from here and all three are
                        # retried with fresh port numbers.
                        time.sleep(0.3 + 0.3 * attempt)
                        continue
            raise DosboxLaunchError(
                f"dosbox-x did not come up after {self.port_retries + 1} attempts (last: {last})"
            ) from last
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        """Tear the session down. Idempotent, and safe to call more than once.

        Safe from `__exit__`, `atexit`, and a signal handler -- see the
        module docstring. Kills the emulator's process group (SIGTERM then
        SIGKILL), removes the registry entry, and removes the workdir this
        session owns via `shutil.rmtree` (never a shell `rm`).
        """
        if self._stopped:
            return
        self._stopped = True
        with _LIVE_LOCK:
            _LIVE.pop(self._key, None)
        for client in (self.gdb, self.qmp):
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()
        self.gdb = None
        self.qmp = None
        self._kill_process()
        if self._log_fh is not None:
            with contextlib.suppress(Exception):
                self._log_fh.close()
            self._log_fh = None
        if self._registry_path is not None:
            with contextlib.suppress(OSError):
                self._registry_path.unlink()
            self._registry_path = None
        self._cleanup_workdir()

    # -- internals ---------------------------------------------------------

    def _make_workdir(self) -> None:
        """Create (or adopt) the private workdir and stage drives, program, and files."""
        if self.workdir is not None:
            self.workdir = Path(self.workdir)
            self.workdir.mkdir(parents=True, exist_ok=True)
            self._owns_workdir = False
        else:
            self.workdir = Path(tempfile.mkdtemp(prefix=f"{self.label}_"))
            self._owns_workdir = True
        drives = dict(self.mounts or {})
        if not drives:
            auto = self.workdir / "c"
            auto.mkdir(exist_ok=True)
            drives = {"c": auto}
        self.drive_paths = {k.lower(): Path(v).resolve() for k, v in drives.items()}
        first = self.drive_paths[sorted(self.drive_paths)[0]]
        for extra in self.files or ():
            # A `(host_path, guest_name)` pair stages the file under a
            # different name -- some guest programs insist on one
            # filename, and without this a caller would need its own
            # tempdir and its own cleanup just for one rename.
            if isinstance(extra, tuple | list):
                src, name = Path(extra[0]), str(extra[1])
            else:
                src, name = Path(extra), Path(extra).name
            if not src.is_file():
                raise FileNotFoundError(f"no such file to mount: {src}")
            dest = first / name
            if src.resolve() != dest.resolve():
                shutil.copy(src, dest)
        if self.program is not None:
            prog = Path(self.program)
            if prog.exists() and prog.is_file():
                if first.is_dir() and prog.parent.resolve() != first:
                    shutil.copy(prog, first / prog.name)
                self.program_name = prog.name
            else:
                self.program_name = str(self.program)

    def _autoexec_lines(self) -> str:
        """Build the `[autoexec]` body: explicit override, or mount + drive-switch."""
        if self.autoexec is not None:
            if isinstance(self.autoexec, str):
                return self.autoexec
            return "\n".join(self.autoexec)
        lines = []
        for letter in sorted(self.drive_paths):
            path = self.drive_paths[letter]
            verb = "mount" if path.is_dir() else "imgmount"
            lines.append(f"{verb} {letter} {path}")
        first = sorted(self.drive_paths)[0] if self.drive_paths else "c"
        lines.append(f"{first}:")
        return "\n".join(lines)

    def _write_conf(self) -> Path:
        """Render the conf template into the workdir and return its path."""
        template = DEFAULT_CONF
        if isinstance(self.conf, Path):
            template = self.conf.read_text()
        elif isinstance(self.conf, str):
            template = self.conf
        assert self.workdir is not None  # set by _make_workdir before this runs
        text = render_conf(
            template,
            {
                "gdb_port": self.gdb_port,
                "qmp_port": self.qmp_port,
                "workdir": self.workdir,
                "autoexec": self._autoexec_lines(),
                "cycles": self.cycles,
                "sdl_output": self.sdl_output,
                "gdbserver": "true" if self.gdbserver else "false",
                "program": self.program_name or "",
                **{f"drive_{k}": v for k, v in self.drive_paths.items()},
            },
        )
        path = self.workdir / "session.conf"
        path.write_text(text)
        return path

    def _allocate_ports(self) -> None:
        """Pick two distinct ephemeral ports nobody else in the registry claims.

        Raises:
            DosboxLaunchError: If 64 attempts did not turn up two free,
                unclaimed ports.
        """
        claimed: set[int] = set()
        try:
            for sess in list_sessions(self.registry):
                if sess.alive:
                    claimed.update(p for p in (sess.gdb_port, sess.qmp_port) if p)
        except OSError:
            pass
        ports: list[int] = []
        for _ in range(64):
            p = free_port()
            if p not in claimed and p not in ports:
                ports.append(p)
            if len(ports) == 2:
                break
        if len(ports) < 2:
            raise DosboxLaunchError("could not allocate two free ports")
        self.gdb_port, self.qmp_port = ports

    def _spawn(self) -> None:
        """Allocate ports, write the conf, and launch DOSBox-X in its own process group."""
        self._allocate_ports()
        self.conf_path = self._write_conf()
        stdout: int | IO[bytes] = subprocess.DEVNULL
        if self.log is not None:
            # The handle outlives this function -- it is closed in stop() --
            # so it cannot be opened with a `with` block here.
            self._log_fh = open(Path(self.log), "ab")  # noqa: SIM115
            stdout = self._log_fh
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        self.proc = subprocess.Popen(
            [str(self.executable), "-conf", str(self.conf_path)],
            stdout=stdout,
            stderr=subprocess.STDOUT if self.log is not None else subprocess.DEVNULL,
            cwd=str(self.workdir),
            env=env,
            # A new session means a new process GROUP, which is what makes
            # `_kill_process` able to take the emulator's children with it.
            start_new_session=True,
        )
        self.pid = self.proc.pid
        self._wait_for_ports()

    def _wait_for_ports(self) -> None:
        """Block until every debug port the conf actually configures accepts.

        Raises:
            DosboxLaunchError: If the process exits first, or the ports do
                not all accept within `startup_timeout`.
        """
        assert self.proc is not None
        # Only the ports the conf actually configures. With `gdbserver=False`
        # nothing ever binds the gdb port, so waiting on it would be a
        # guaranteed timeout rather than a readiness test.
        wanted: list[tuple[str, int | None]] = [("qmp", self.qmp_port)]
        if self.gdbserver:
            wanted.insert(0, ("gdb", self.gdb_port))
        names = " / ".join(f"{n}={p}" for n, p in wanted)
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise DosboxLaunchError(
                    f"dosbox-x exited with {self.proc.returncode} before its "
                    f"debug ports came up ({names})"
                )
            if all(port_is_listening(p) for _, p in wanted if p is not None):
                return
            time.sleep(0.1)
        raise DosboxLaunchError(f"{names} did not all accept within {self.startup_timeout}s")

    def _connect_clients(self) -> None:
        """Connect GDB (if enabled) then QMP, each with its own retry loop.

        GDB first: the GDB connection must be established before
        break-on-exec is armed over QMP. QMP gets the SAME retry as GDB --
        the two servers bind independently, so "gdbserver is accepting"
        does not imply "qmpserver is accepting"; an unretried attempt loses
        that race whenever the GDB connect wins it.
        """
        assert self.gdb_port is not None and self.qmp_port is not None
        gdb_port, qmp_port = self.gdb_port, self.qmp_port
        if self.gdbserver:
            self.gdb = self._connect_with_retry(lambda: GDBClient(port=gdb_port), "gdbserver")
        self.qmp = self._connect_with_retry(lambda: QMPClient(port=qmp_port), "qmpserver")

    def _connect_with_retry(self, factory: Callable[[], _ClientT], what: str) -> _ClientT:
        """Retry `factory()` until it succeeds, the process dies, or the deadline passes.

        Args:
            factory: Builds and connects a fresh client. Both `GDBClient`
                and `QMPClient` connect inside `__init__`, so retrying means
                constructing a new instance each attempt, not reusing one
                across a separate `.connect()` call.
            what: Name used in the raised error, e.g. `"gdbserver"`.

        Returns:
            The connected client.

        Raises:
            IncompatibleStubError: If the stub connects and completes its
                handshake but does not advertise a required capability
                (currently only `GDBClient` can raise this). Re-raised
                immediately, without retrying: a missing capability is a
                property of the running build, not a transient bind race,
                and retrying it to a 30s deadline against an older build
                spends roughly 120 seconds and hundreds of connect/handshake
                cycles (port_retries + 1 launch attempts, each retrying the
                connect to its own deadline) only to report a misleading
                "never accepted a connection" -- the stub accepted every one.
            DosboxLaunchError: If the process exits while connecting, or no
                attempt succeeds within `connect_timeout`.
        """
        deadline = time.time() + self.connect_timeout
        last: Exception | None = None
        while True:
            if self.proc is not None and self.proc.poll() is not None:
                raise DosboxLaunchError(f"dosbox-x exited while connecting to {what}")
            try:
                return factory()
            except IncompatibleStubError:
                raise  # permanent: the stub will not grow the capability on retry
            except Exception as exc:  # a refused/slow connect is retried to a deadline
                last = exc
                if time.time() > deadline:
                    raise DosboxLaunchError(
                        f"{what} never accepted a connection within "
                        f"{self.connect_timeout}s (last: {last!r})"
                    ) from last
                time.sleep(0.25)

    def _register(self) -> None:
        """Write this session's on-disk registry file.

        The in-memory `_LIVE` registration happens much earlier, at the top
        of `start()` -- see the comment there for why.
        """
        assert self.pid is not None
        record = {
            "pid": self.pid,
            "pgid": self.pid,  # start_new_session: pgid == pid
            "proc_starttime": _proc_starttime(self.pid),
            "owner_pid": os.getpid(),
            "owner_starttime": _proc_starttime(os.getpid()),
            "gdb_port": self.gdb_port,
            "qmp_port": self.qmp_port,
            "workdir": str(self.workdir),
            "owns_workdir": self._owns_workdir,
            "conf": str(self.conf_path),
            "program": self.program_name,
            "label": self.label,
            "executable": str(self.executable),
            "started_at": time.time(),
            "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "attempts": self.attempts,
        }
        path = registry_dir(self.registry) / f"{self.pid}-{self._key}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2))
        tmp.replace(path)  # atomic: a --list never reads a half-written file
        self._registry_path = path

    def _kill_process(self) -> None:
        """Kill the emulator's process group via `registry.kill_group`, then reap it.

        Delegates the SIGTERM-then-SIGKILL escalation for the LEADER to
        `kill_group` instead of duplicating it here: a hand-rolled version
        that only catches `ProcessLookupError` on its first `killpg` lets a
        `PermissionError` propagate out of `stop()` -- and `stop()` runs
        from `__exit__`, where a newly raised exception would mask whatever
        exception the `with` block was already unwinding for. `kill_group`
        handles that case itself, returning `"denied"` rather than raising.
        It has no `Popen` handle to reap the zombie with, though, so that
        part is still done here.

        `pid=proc.pid` is passed through so `kill_group` checks the LEADER's
        liveness with `_pid_alive` (which treats a zombie as dead) instead
        of probing the whole process group with `killpg(pgid, 0)` (which
        does not: a child we have not yet `wait()`-ed on still answers
        signal 0 as "alive" even after it has exited). Without `pid=`,
        `kill_group` would see "alive" for the entire `term_timeout` -- and
        then the whole second, 5s "is it stuck" window on top of that --
        every single time, since nothing reaps the zombie until the
        `proc.wait()` below runs.

        That `pid=` speedup has a real cost, though: `kill_group` then
        checks ONLY the leader, never the group. A child that forks off the
        leader, ignores SIGTERM, and outlives it is invisible to that check
        -- `kill_group` would report `"gone"` the moment the (already-dead)
        leader's zombie satisfies `_pid_alive`, without ever having sent the
        group a signal at all, leaving that child running. The explicit
        group-wide sweep below is what the hand-rolled version this method
        replaced did unconditionally, and it is why: after the leader is
        both killed AND reaped (so its own zombie cannot make `killpg(pgid,
        0)` falsely report the group as still alive), anything left
        answering in the group gets one unconditional `SIGKILL`.
        """
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        if proc.poll() is not None:
            proc.wait()
            return
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            proc.wait()
            return
        kill_group(pgid, self.term_timeout, pid=proc.pid)
        proc.wait()  # reap the leader BEFORE the group probe below
        # A child that ignored SIGTERM can outlive its leader in the same
        # process group -- `kill_group(pid=...)` above deliberately checks
        # only the leader, not the group, so this sweep is what catches
        # that straggler. `ProcessLookupError` (nothing left) is the normal
        # case; `PermissionError` is swallowed rather than raised, since
        # this runs from `stop()`, which runs from `__exit__`.
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)

    def _cleanup_workdir(self) -> None:
        """Remove the workdir this session owns, unless told to keep it."""
        if self.keep_workdir or not self._owns_workdir:
            return
        if self.workdir and Path(self.workdir).exists():
            # shutil.rmtree, never a shell `rm -rf` -- some sandboxes refuse
            # that command outright, and that refusal is exactly how
            # orphaned scratch dirs happen.
            shutil.rmtree(self.workdir, ignore_errors=True)

    # -- small conveniences the callers all need ---------------------------

    @property
    def running(self) -> bool:
        """True while the emulator process is alive."""
        return self.proc is not None and self.proc.poll() is None

    def screen_lines(self, width: int = 80, height: int = 25) -> list[str]:
        """Read the guest's VGA text screen through this session's GDB client.

        Reads video memory at linear address `0xB8000` -- the standard
        text-mode framebuffer, page 0 -- and hands it to
        `video.decode_text_screen`, the one decode path this package has.
        This method used to carry its own copy of that loop, because
        `DOSVideoTools` could not be given a client and so could not be
        reached from here without opening a second one; both halves of
        lokkju/dbxdebug#7's first item.

        Errors from the read propagate. `DOSVideoTools.screen_dump`, which
        returns `None` instead, is the other caller of the same decode.

        Args:
            width: Screen columns.
            height: Screen rows.

        Returns:
            `height` strings of `width` characters each. See
            `video.decode_text_screen` for the cell encoding.

        Raises:
            RuntimeError: If no GDB client is connected (`connect=False`).
        """
        if self.gdb is None:
            raise RuntimeError("no GDB client (connect=False?)")
        memory = self.gdb.read_memory(0xB8000, width * height * 2)
        return decode_text_screen(memory, width, height)

    def set_breakpoint(self, seg: int, off: int) -> bool:
        """Break at `seg:off`, translated to a LINEAR address.

        **BREAKING CHANGE from the ported source.** The original packed
        `seg:off` as `(seg << 16) | off` because the DOSBox-X GDB stub it
        was written against decoded `Z0`/`z0` addresses as a far pointer.
        Current DOSBox-X builds -- the ones this package's `GDBClient`
        requires via `dosbox-x-linear-bp+` -- decode `Z0`/`z0` as a LINEAR
        address instead. Packing here would silently set the breakpoint at
        the wrong physical location while gdbserver still answers `OK`. See
        `addressing.linear` for the conversion and `addressing.bp_addr`,
        which now refuses to pack a pair at all, loudly, for exactly this
        reason.

        Args:
            seg: Segment value.
            off: Offset within the segment.

        Returns:
            True if the stub acknowledged the breakpoint.

        Raises:
            RuntimeError: If no GDB client is connected (`connect=False`).
        """
        if self.gdb is None:
            raise RuntimeError("no GDB client (connect=False?)")
        return self.gdb.set_breakpoint(linear(seg, off))

    def remove_breakpoint(self, seg: int, off: int) -> bool:
        """Undo `set_breakpoint` for the same `seg:off` pair.

        Args:
            seg: Segment value.
            off: Offset within the segment.

        Returns:
            True if the stub acknowledged the removal.

        Raises:
            RuntimeError: If no GDB client is connected (`connect=False`).
        """
        if self.gdb is None:
            raise RuntimeError("no GDB client (connect=False?)")
        return self.gdb.remove_breakpoint(linear(seg, off))

    def wait_for_text(self, want: str, timeout: float = 60.0, poll: float = 0.5) -> float | None:
        """Poll the guest's screen until `want` appears, or `timeout` elapses.

        Readiness observed rather than assumed: a caller that instead
        sleeps a fixed interval cannot tell a slow boot from a failed one,
        and captures have been taken against a guest whose screen never
        became readable for exactly that reason.

        Args:
            want: Substring to look for in any screen line.
            timeout: Maximum wall seconds to wait.
            poll: Wall seconds between screen reads.

        Returns:
            The observed number of seconds until `want` appeared (rounded
            to 2 decimals), or None if the process died or the timeout
            elapsed first.
        """
        start = time.time()
        deadline = start + timeout
        while time.time() < deadline:
            if not self.running:
                return None
            try:
                if any(want in line for line in self.screen_lines()):
                    return round(time.time() - start, 2)
            except Exception:  # a slow or not-yet-ready guest may not answer yet
                pass
            time.sleep(poll)
        return None

    def assert_screen_readable(self) -> None:
        """Raise unless the guest screen holds real content, not blank or boot banner.

        A screen that is still blank has not been painted by the guest yet.
        A screen still showing DOSBox-X's own "DOSBox-X command shell" /
        "Welcome to DOSBox-X" boot banner has not reached the point where an
        autoexec'd program has run. Both are the emulator's readiness, not a
        consumer's job to guard against, and this is why two downstream
        capture scripts once shipped 24 frames of exactly that banner: it is
        text, so a plain non-blank-cell check cannot tell it apart from the
        program under test. `boot_settle` makes both cases unlikely, not
        impossible; a caller reading the screen immediately after `start()`
        should call this first.

        Raises:
            DosboxLaunchError: If the screen is entirely blank, or still
                shows the DOSBox-X boot banner.
        """
        lines = self.screen_lines()
        if all(not line.strip() for line in lines):
            raise DosboxLaunchError("guest screen is blank -- nothing has been painted yet")
        if any("DOSBox-X" in line for line in lines):
            raise DosboxLaunchError("guest screen still shows the DOSBox-X boot banner")
