"""Tests for `DosboxSession`: conf rendering, port allocation, and (when an
emulator is available) the full start/stop lifecycle.

Split by whether `~/projects/eesystem/dosbox-x/src/dosbox-x` (overridable via
`DBXDEBUG_DOSBOX`) exists: the conf and port-allocation tests below run
unconditionally and touch no emulator, while the lifecycle tests skip
cleanly when the binary is absent, so the rest of the suite still runs.
Each emulator-backed test costs a real boot (`boot_settle` alone is 2.5s),
so they are kept to the minimum that proves `start()`/`stop()` actually work
end to end -- Task 11 adds the thorough live suite.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time

import pytest

import dbxdebug.session as session_module
from dbxdebug.gdb import IncompatibleStubError
from dbxdebug.paths import find_dosbox_x
from dbxdebug.registry import _pid_alive, list_sessions, registry_dir
from dbxdebug.session import DEFAULT_CONF, HEADLESS_ENV, DosboxSession, render_conf


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """Point every test at a scratch registry, never the real one."""
    monkeypatch.setenv("DBXDEBUG_REGISTRY", str(tmp_path / "registry"))
    return tmp_path


_found_dosbox_x = find_dosbox_x()

requires_emulator = pytest.mark.skipif(
    _found_dosbox_x is None,
    reason="no DOSBox-X binary found (checked $DBXDEBUG_DOSBOX, the conventional "
    "path, and $PATH; set DBXDEBUG_DOSBOX to override)",
)


# --------------------------------------------------------------------------
# render_conf
# --------------------------------------------------------------------------


def test_render_conf_substitutes_known_keys_only():
    out = render_conf("known={known} unknown={unknown}", {"known": "42"})
    assert out == "known=42 unknown={unknown}"


def test_render_conf_leaves_literal_braces_with_no_matching_key_alone():
    out = render_conf("{a}{b}{c}", {"b": "B"})
    assert out == "{a}B{c}"


def test_default_conf_uses_spaced_port_keys_not_bare_ones():
    text = render_conf(
        DEFAULT_CONF,
        {
            "gdbserver": "true",
            "gdb_port": 12345,
            "qmp_port": 54321,
            "workdir": "/tmp/x",
            "autoexec": "",
            "cycles": "max",
            "sdl_output": "surface",
        },
    )
    assert "gdbserver port = 12345" in text
    assert "qmpserver port = 54321" in text
    # These spellings are silently ignored by DOSBox-X, which leaves the
    # server on its compiled-in default port -- a session using them would
    # end up connected to someone else's emulator, not its own.
    assert "gdbport" not in text
    assert "qmpport" not in text


def test_default_conf_pins_autolock_off_so_a_visible_window_cannot_grab_the_mouse():
    # Only matters with `headless=False`, where the window is real: one
    # stray click in it would otherwise lock the host mouse to the guest.
    text = render_conf(DEFAULT_CONF, {"sdl_output": "surface"})
    assert "autolock = false" in text


# --------------------------------------------------------------------------
# headless / child environment
# --------------------------------------------------------------------------


def test_headless_is_the_default():
    # A behaviour change, and the one the whole feature turns on: an
    # unconfigured session must not open a window.
    assert DosboxSession().headless is True


def test_headless_puts_the_dummy_sdl_drivers_in_the_child_environment():
    env = DosboxSession()._child_env()
    assert env["SDL_VIDEODRIVER"] == "dummy"
    assert env["SDL_AUDIODRIVER"] == "dummy"


def test_headless_false_adds_no_sdl_overrides_of_its_own(monkeypatch):
    monkeypatch.delenv("SDL_VIDEODRIVER", raising=False)
    monkeypatch.delenv("SDL_AUDIODRIVER", raising=False)
    env = DosboxSession(headless=False)._child_env()
    assert "SDL_VIDEODRIVER" not in env
    assert "SDL_AUDIODRIVER" not in env


def test_headless_beats_an_inherited_sdl_videodriver(monkeypatch):
    # Otherwise a developer who exports SDL_VIDEODRIVER gets a window from a
    # session that explicitly asked not to have one.
    monkeypatch.setenv("SDL_VIDEODRIVER", "x11")
    assert DosboxSession()._child_env()["SDL_VIDEODRIVER"] == "dummy"


def test_caller_env_beats_headless():
    # `env` is the escape hatch, and an escape hatch a flag can override is
    # not one. This is also what keeps `env=` behaving as it did before
    # `headless` existed.
    env = DosboxSession(headless=True, env={"SDL_VIDEODRIVER": "x11"})._child_env()
    assert env["SDL_VIDEODRIVER"] == "x11"
    assert env["SDL_AUDIODRIVER"] == "dummy"  # composed, not clobbered


def test_caller_env_still_passes_unrelated_variables_through():
    env = DosboxSession(env={"DBXDEBUG_MARKER": "hello"})._child_env()
    assert env["DBXDEBUG_MARKER"] == "hello"
    assert env["SDL_VIDEODRIVER"] == "dummy"


def test_child_env_inherits_the_parent_environment(monkeypatch):
    monkeypatch.setenv("DBXDEBUG_INHERITED", "yes")
    assert DosboxSession()._child_env()["DBXDEBUG_INHERITED"] == "yes"


def test_child_env_does_not_mutate_headless_env_or_os_environ():
    before = dict(HEADLESS_ENV)
    DosboxSession(env={"DBXDEBUG_MARKER": "hello"})._child_env()
    assert dict(HEADLESS_ENV) == before
    assert "DBXDEBUG_MARKER" not in os.environ


# --------------------------------------------------------------------------
# port allocation
# --------------------------------------------------------------------------


def test_allocate_ports_are_dynamic_never_the_stock_defaults():
    session = DosboxSession()
    session._allocate_ports()
    assert session.gdb_port not in (None, 2159, 4444)
    assert session.qmp_port not in (None, 2159, 4444)
    assert session.gdb_port != session.qmp_port


def test_allocate_ports_skips_ports_claimed_by_a_live_registered_session(monkeypatch):
    """A port already claimed by another alive session in the registry is skipped.

    `free_port()` is monkeypatched to hand back the claimed ports first, so
    the exclusion logic in `_allocate_ports` is actually exercised rather
    than passing by chance (real ephemeral ports essentially never collide
    with a fixed test value on their own).
    """
    d = registry_dir()
    record = {
        "pid": os.getpid(),  # this test process: always "alive"
        "gdb_port": 55001,
        "qmp_port": 55002,
        "started_at": time.time(),
    }
    (d / f"{os.getpid()}-fake.json").write_text(json.dumps(record))

    real_free_port = session_module.free_port
    scripted = iter([55001, 55002, real_free_port(), real_free_port()])
    monkeypatch.setattr(session_module, "free_port", lambda: next(scripted))

    session = DosboxSession()
    session._allocate_ports()
    assert session.gdb_port not in (55001, 55002)
    assert session.qmp_port not in (55001, 55002)


# --------------------------------------------------------------------------
# _LIVE registration: early enough that a mid-start failure still cleans up
# --------------------------------------------------------------------------


def test_start_registers_into_live_before_spawning_so_a_failure_still_cleans_up():
    """A failure in `_make_workdir()` must not leave the session in `_LIVE`.

    `_LIVE` is populated at the very top of `start()`, before anything is
    spawned, so a signal arriving during the several-second window before
    `_register()` still finds something to stop (see the comment in
    `start()`). That means an early failure -- staging a nonexistent file,
    here -- has to route through `stop()` to clear the entry back out,
    exactly like a later failure does.
    """
    session = DosboxSession(files=["/no/such/file"])
    with pytest.raises(FileNotFoundError):
        session.start()
    assert session._key not in session_module._LIVE


# --------------------------------------------------------------------------
# capability mismatch: fail fast, never retry
# --------------------------------------------------------------------------


def test_connect_with_retry_reraises_incompatible_stub_immediately():
    """An `IncompatibleStubError` is permanent, not a bind race -- it must not be retried.

    Retrying it to `connect_timeout` against an older build costs tens of
    seconds and hundreds of connect/handshake cycles, then reports a
    misleading "never accepted a connection" even though the stub answered
    every single attempt. No emulator needed: a fake factory that always
    raises is enough to prove the fast path.
    """
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        raise IncompatibleStubError("build lacks dosbox-x-linear-bp+")

    session = DosboxSession(connect_timeout=30.0)
    start = time.time()
    with pytest.raises(IncompatibleStubError):
        session._connect_with_retry(factory, "gdbserver")
    # No 0.25s retry sleep and no waiting out the 30s deadline: one call, fast.
    assert time.time() - start < 1.0
    assert calls == 1


# --------------------------------------------------------------------------
# process-group sweep: a child that outlives its leader must still die
# --------------------------------------------------------------------------


def test_kill_process_sweeps_a_child_that_outlives_its_leader(tmp_path):
    """`kill_group(pid=...)` checks only the LEADER's liveness, never the group.

    A child the leader forked, that ignores SIGTERM and keeps running after
    the leader itself has been killed and reaped, would otherwise survive
    `_kill_process` entirely: `kill_group` reports the leader gone the
    moment it dies, without the process group ever being probed again. This
    builds that scenario directly, with no emulator involved -- a leader
    process with the default SIGTERM disposition (dies immediately) that
    forks a child which installs `SIG_IGN` for SIGTERM and sleeps well past
    this test's own bound. `_kill_process`'s sweep after `kill_group` is
    what has to catch that straggler.

    The child signals its own readiness by touching `ready_marker` right
    after installing `SIG_IGN`, and the test waits (bounded) for that file
    rather than guessing a sleep: sending the group's SIGTERM before the
    freshly-forked child has actually installed its handler is a real race
    -- caught empirically while writing this test, where the child still had
    its default (inherited) disposition for a few milliseconds after
    `os.fork()` and so died right along with the leader, making the sweep
    look unnecessary when it was really just outrunning the setup.

    Only ever signals the process group this test itself spawned
    (`start_new_session=True` guarantees a fresh, dedicated pgid), and a
    `finally` force-kills that same group so a failing assertion cannot
    leave the child running.
    """
    ready_marker = tmp_path / "child_ready"
    leader_script = (
        "import os, signal, sys, time\n"
        f"ready_marker = {str(ready_marker)!r}\n"
        "child_pid = os.fork()\n"
        "if child_pid == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    open(ready_marker, 'w').close()\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "else:\n"
        "    sys.stdout.write(str(child_pid) + chr(10))\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(30)\n"  # default SIGTERM disposition: dies at once on SIGTERM
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", leader_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    assert proc.stdout is not None  # requested via stdout=subprocess.PIPE above
    try:
        child_pid = int(proc.stdout.readline().strip())
        assert proc.poll() is None  # leader is up and blocked on its own sleep

        deadline = time.time() + 5.0
        while time.time() < deadline and not ready_marker.exists():
            time.sleep(0.01)
        assert ready_marker.exists(), "child never installed SIG_IGN in time"

        session = DosboxSession(term_timeout=2.0)
        session.proc = proc
        session._kill_process()

        deadline = time.time() + 2.0
        while time.time() < deadline and _pid_alive(child_pid):
            time.sleep(0.05)
        assert not _pid_alive(child_pid)
        assert not _pid_alive(proc.pid)
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=2.0)
        if proc.stdout is not None:
            proc.stdout.close()


# --------------------------------------------------------------------------
# full lifecycle (needs a real emulator)
# --------------------------------------------------------------------------


# Marked `integration` as well as guarded by `requires_emulator`: the marker
# means "launches a real emulator", not "lives in tests/integration", and
# this test launches one. Without it a plain `uv run pytest` on a developer
# machine boots DOSBox-X, which is exactly what the marker exists to avoid.
@pytest.mark.integration
@requires_emulator
def test_session_starts_connects_and_tears_down_idempotently():
    # No explicit `workdir=`: the session must allocate and own its own
    # private tempdir for `_cleanup_workdir` to remove it on stop().
    session = DosboxSession(
        # headless=True is the default; not repeated here, so a regression in
        # that default opens a window during the test run.
        install_hooks=False,  # keep atexit/signal hooks out of the test run
    )
    try:
        session.start()
        assert session.running is True
        assert session.pid is not None
        assert session.gdb is not None
        assert session.qmp is not None
        assert session.gdb_port not in (2159, 4444)
        assert session.qmp_port not in (2159, 4444)
        assert len(list_sessions()) == 1

        pid = session.pid
        workdir = session.workdir

        session.stop()
        session.stop()  # idempotent: must not raise or attempt a double kill

        deadline = time.time() + 5.0
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        assert not _pid_alive(pid)
        assert workdir is not None
        assert not workdir.exists()
        assert list_sessions() == []
    finally:
        session.stop()
