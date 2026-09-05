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

import json
import os
import time
from pathlib import Path

import pytest

import dbxdebug.session as session_module
from dbxdebug.registry import _pid_alive, list_sessions, registry_dir
from dbxdebug.session import DEFAULT_CONF, DosboxSession, render_conf


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """Point every test at a scratch registry, never the real one."""
    monkeypatch.setenv("DBXDEBUG_REGISTRY", str(tmp_path / "registry"))
    return tmp_path


def _dosbox_x_path() -> Path:
    """Resolve the emulator path the same way `session.py` does."""
    return Path(
        os.environ.get(
            "DBXDEBUG_DOSBOX",
            str(Path.home() / "projects/eesystem/dosbox-x/src/dosbox-x"),
        )
    )


requires_emulator = pytest.mark.skipif(
    not _dosbox_x_path().exists(),
    reason=f"no DOSBox-X binary at {_dosbox_x_path()} (set DBXDEBUG_DOSBOX to override)",
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
# full lifecycle (needs a real emulator)
# --------------------------------------------------------------------------


@requires_emulator
def test_session_starts_connects_and_tears_down_idempotently():
    # No explicit `workdir=`: the session must allocate and own its own
    # private tempdir for `_cleanup_workdir` to remove it on stop().
    session = DosboxSession(
        env={"SDL_VIDEODRIVER": "dummy"},
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
