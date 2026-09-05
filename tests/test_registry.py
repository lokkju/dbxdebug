"""Tests for the session registry: ports, identity, listing, and reap."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dbxdebug.registry import (
    RegisteredSession,
    format_table,
    free_port,
    list_sessions,
    port_is_listening,
    registry_dir,
    wait_ports_free,
)


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """Point every test at a scratch registry, never the real one."""
    monkeypatch.setenv("DBXDEBUG_REGISTRY", str(tmp_path / "registry"))
    return tmp_path


# --------------------------------------------------------------------------
# ports
# --------------------------------------------------------------------------


def test_free_port_returns_a_bindable_port():
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_free_port_two_calls_differ():
    a = free_port()
    b = free_port()
    assert a != b


def test_port_is_listening_false_for_a_closed_port():
    port = free_port()  # nothing is bound to it any more
    assert port_is_listening(port, timeout=0.2) is False


def _serve_forever(srv: socket.socket) -> None:
    """Accept and immediately drop connections until `srv` is closed.

    A listening socket that nobody calls `accept()` on only answers a
    handful of connects before its backlog fills and it starts refusing --
    which would make `port_is_listening` flap during a polling test. Draining
    the backlog on a background thread keeps it answering for as long as the
    test needs it to.
    """
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        conn.close()


def test_port_is_listening_true_for_a_bound_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        t = threading.Thread(target=_serve_forever, args=(srv,), daemon=True)
        t.start()
        assert port_is_listening(port, timeout=0.5) is True


def test_wait_ports_free_returns_promptly_when_nothing_holds_the_port():
    port = free_port()
    start = time.monotonic()
    assert wait_ports_free([port], timeout=5.0) is True
    assert time.monotonic() - start < 2.0


def test_wait_ports_free_times_out_when_the_test_holds_the_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        t = threading.Thread(target=_serve_forever, args=(srv,), daemon=True)
        t.start()
        start = time.monotonic()
        assert wait_ports_free([port], timeout=0.5) is False
        assert time.monotonic() - start >= 0.5


# --------------------------------------------------------------------------
# registry round-trip
# --------------------------------------------------------------------------


def _write_session(registry_path: Path, **overrides) -> Path:
    record = {
        "pid": os.getpid(),
        "pgid": os.getpid(),
        "proc_starttime": None,
        "owner_pid": os.getpid(),
        "owner_starttime": None,
        "gdb_port": 12345,
        "qmp_port": 54321,
        "workdir": "/tmp/does-not-matter",
        "owns_workdir": False,
        "started_at": time.time(),
    }
    record.update(overrides)
    registry_path.mkdir(parents=True, exist_ok=True)
    path = registry_path / f"{record['pid']}-test.json"
    path.write_text(json.dumps(record))
    return path


def test_registry_dir_honors_the_env_var(tmp_path):
    d = registry_dir()
    assert d == tmp_path / "registry"
    assert d.is_dir()


def test_registered_session_round_trips_its_fields(tmp_path):
    d = registry_dir()
    _write_session(d, gdb_port=2159, qmp_port=4444, workdir=str(tmp_path / "wd"))

    sessions = list_sessions()
    assert len(sessions) == 1
    sess = sessions[0]
    assert sess.pid == os.getpid()
    assert sess.pgid == os.getpid()
    assert sess.owner_pid == os.getpid()
    assert sess.gdb_port == 2159
    assert sess.qmp_port == 4444
    assert sess.workdir == tmp_path / "wd"
    assert sess.started_at > 0
    assert sess.age_s >= 0


def test_alive_is_true_for_the_current_process():
    d = registry_dir()
    _write_session(d)
    sess = list_sessions()[0]
    assert sess.alive is True


def test_alive_is_false_for_a_pid_that_does_not_exist():
    d = registry_dir()
    # Spawn and immediately reap a subprocess so its pid is guaranteed dead,
    # then record that dead pid with no starttime pinned.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = proc.pid
    proc.wait()
    _write_session(d, pid=dead_pid, owner_pid=os.getpid())

    sess = list_sessions()[0]
    assert sess.alive is False


def test_unreadable_registry_files_are_skipped():
    d = registry_dir()
    (d / "garbage.json").write_text("{not json")
    _write_session(d)
    sessions = list_sessions()
    assert len(sessions) == 1


# --------------------------------------------------------------------------
# format_table
# --------------------------------------------------------------------------


def test_format_table_handles_an_empty_list():
    out = format_table([])
    assert isinstance(out, str)
    assert "no registered" in out


def test_format_table_handles_a_populated_list():
    d = registry_dir()
    _write_session(d)
    out = format_table(list_sessions())
    assert "PID" in out
    assert str(os.getpid()) in out


def test_format_table_accepts_a_bare_registered_session(tmp_path):
    """format_table takes any Iterable[RegisteredSession], not just list_sessions()."""
    sess = RegisteredSession(
        path=tmp_path / "fake.json",
        data={"pid": 999999, "started_at": time.time()},
    )
    out = format_table([sess])
    assert "999999" in out
