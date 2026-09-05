"""The bulk-read path: `DosboxSession.read_bulk`, `GDBClient.resume`, the wrapped refusal.

A bulk read is the single biggest performance win this package offers -- one
QMP `memdump` reply in place of thousands of GDB `m` round-trips -- and it is
guarded by two rules that neither client's API reveals:

1. `memdump` is refused while the CPU is running.
2. The obvious way to stop it, `qmp.stop()`, is a trap: it parks the
   emulation thread, and DOSBox-X polls the GDB stub FROM that thread, so the
   dump succeeds and every GDB request afterwards goes unanswered.

`read_bulk` holds both rules so a caller does not have to. These tests pin
down what it does in each run state, that it restores the one it found, and
that it restores it even when the dump fails. They drive fakes, so no
emulator is involved; the live counterparts -- byte-for-byte equality against
`gdb.read_memory`, and the measured speed difference -- are in
`tests/integration/test_live_session.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from dbxdebug.gdb import GDBClient, GDBTimeoutError
from dbxdebug.qmp import CpuNotStoppedError, QMPError
from dbxdebug.session import DosboxSession
from tests.test_gdb_framing import Timeout, connect, gdb_packet

# The refusal a running DOSBox-X sends back, verbatim off the wire.
STUB_REFUSAL = (
    "memdump requires the CPU to be stopped for debugging; halt via GDB or QMP stop first"
)

# What `query-status` reports in each of the three states that matter. Taken
# from live replies, so the shape -- a flat `emulator-paused` beside a nested
# `debug` object -- is the real one and not an invention of these tests.
RUNNING = {
    "status": "running",
    "running": True,
    "emulator-paused": False,
    "debug": {"active": True, "paused": False},
}
GDB_HALTED = {
    "status": "paused",
    "running": False,
    "emulator-paused": False,
    "debug": {"active": True, "paused": True, "reason": "gdb"},
}
QMP_STOPPED = {
    "status": "paused",
    "running": False,
    "emulator-paused": True,
    "debug": {"active": True, "paused": False},
}


class FakeGDB:
    """Records the run-control calls `read_bulk` makes, in order."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def halt(self) -> bytes:
        """Record a halt.

        Returns:
            The stop reply a real stub sends.
        """
        self.calls.append("halt")
        return b"S05"

    def resume(self) -> None:
        """Record a resume."""
        self.calls.append("resume")


class FakeQMP:
    """A `QMPClient` stand-in that reports a fixed run state and canned bytes."""

    def __init__(self, status: dict[str, Any], payload: bytes | Exception = b"\xde\xad") -> None:
        """Set the state this fake reports and what its dump does.

        Args:
            status: The `query-status` reply to return.
            payload: Bytes for `memdump` to return, or an exception for it
                to raise.
        """
        self._status = status
        self._payload = payload
        self.calls: list[tuple[str, Any]] = []

    def query_status(self) -> dict[str, Any]:
        """Return the fixed run state.

        Returns:
            The status dict this fake was built with.
        """
        self.calls.append(("query_status", None))
        return self._status

    def memdump(self, address: int, size: int) -> bytes:
        """Return the canned dump, or raise what the fake was given.

        Args:
            address: Recorded, not used.
            size: Recorded, not used.

        Returns:
            The canned payload.

        Raises:
            Exception: Whatever `payload` was, if it is an exception.
        """
        self.calls.append(("memdump", (address, size)))
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def session_with(gdb: object, qmp: object) -> DosboxSession:
    """Build an UNSTARTED session wired to fake clients.

    `read_bulk` only ever touches `self.gdb` and `self.qmp`, so no emulator
    and no `start()` are needed to exercise every branch of it.

    Args:
        gdb: Stand-in for the GDB client, or None.
        qmp: Stand-in for the QMP client, or None.

    Returns:
        A session that must NOT be started -- it has no process behind it.
    """
    session = DosboxSession(connect=False, install_hooks=False)
    session.gdb = gdb  # type: ignore[assignment]
    session.qmp = qmp  # type: ignore[assignment]
    return session


# -- read_bulk: run state in, same run state out ---------------------------


def test_a_running_cpu_is_halted_for_the_dump_and_resumed_after() -> None:
    """The whole point: halt, dump, resume, in that order, from one call."""
    gdb, qmp = FakeGDB(), FakeQMP(RUNNING, b"\x01\x02\x03\x04")

    result = session_with(gdb, qmp).read_bulk(0xF0000, 4)

    assert result == b"\x01\x02\x03\x04"
    assert gdb.calls == ["halt", "resume"]
    assert qmp.calls == [("query_status", None), ("memdump", (0xF0000, 4))]


def test_an_already_gdb_halted_cpu_is_left_halted() -> None:
    """A CPU stopped at a breakpoint stays stopped: do not resume what you did not stop.

    Resuming here would restart a guest the caller deliberately stopped, and
    would do it invisibly from inside what reads like a pure read -- losing
    exactly the state the caller halted to inspect.
    """
    gdb, qmp = FakeGDB(), FakeQMP(GDB_HALTED)

    session_with(gdb, qmp).read_bulk(0xF0000, 2)

    assert gdb.calls == []


def test_a_qmp_stopped_emulator_is_left_stopped() -> None:
    """A QMP-stopped emulator is dumped as-is, and NOT handed to the GDB stub.

    `emulator-paused` means the emulation thread is parked, and that thread
    is what services the GDB stub -- so a `halt()` here would go unanswered
    until the socket timed out. `memdump` needs no such help: the stub
    accepts it in this state too.
    """
    gdb, qmp = FakeGDB(), FakeQMP(QMP_STOPPED)

    session_with(gdb, qmp).read_bulk(0xF0000, 2)

    assert gdb.calls == []


def test_the_cpu_is_resumed_even_when_the_dump_fails() -> None:
    """A failed dump must not leave the guest halted behind it."""
    gdb = FakeGDB()
    qmp = FakeQMP(RUNNING, QMPError("GenericError: Failed to dump memory"))

    with pytest.raises(QMPError):
        session_with(gdb, qmp).read_bulk(0xF0000, 4)

    assert gdb.calls == ["halt", "resume"]


def test_a_running_cpu_with_no_gdb_client_refuses_rather_than_qmp_stopping_it() -> None:
    """With no way to halt through GDB, `read_bulk` says so instead of reaching for `stop()`.

    `qmp.stop()` would make the dump legal and wedge the GDB stub. Refusing
    keeps the session in the state the caller left it in.
    """
    session = session_with(None, FakeQMP(RUNNING))

    with pytest.raises(RuntimeError, match="qmp.stop\\(\\) is NOT a substitute"):
        session.read_bulk(0xF0000, 4)


def test_no_qmp_client_is_a_clear_error() -> None:
    """`connect=False` leaves nothing to dump through."""
    with pytest.raises(RuntimeError, match="no QMP client"):
        session_with(FakeGDB(), None).read_bulk(0xF0000, 4)


# -- the wrapped refusal ---------------------------------------------------


def test_the_running_cpu_refusal_names_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw stub refusal is kept, and the trap it does not mention is spelled out."""
    from tests.test_qmp_commands import _connect

    client, _fake = _connect(
        monkeypatch, {"error": {"class": "GenericError", "desc": STUB_REFUSAL}}
    )

    with pytest.raises(CpuNotStoppedError) as caught:
        client.memdump(0x1000, 4)

    message = str(caught.value)
    # The stub's own words survive, so anything matching on them still does.
    assert STUB_REFUSAL in message
    # And the two things the stub cannot tell the caller.
    assert "gdb.halt()" in message
    assert "read_bulk" in message
    assert "Do NOT reach for `qmp.stop()`" in message


def test_the_wrapped_refusal_is_still_a_qmp_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing callers catching `QMPError` keep catching this one."""
    from tests.test_qmp_commands import _connect

    client, _fake = _connect(
        monkeypatch, {"error": {"class": "GenericError", "desc": STUB_REFUSAL}}
    )

    with pytest.raises(QMPError):
        client.memdump(0x1000, 4)


def test_other_memdump_failures_are_not_relabelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dump that fails for an unrelated reason keeps its plain `QMPError`."""
    from tests.test_qmp_commands import _connect

    client, _fake = _connect(
        monkeypatch,
        {"error": {"class": "GenericError", "desc": "Size too large (max 16MB)"}},
    )

    with pytest.raises(QMPError) as caught:
        client.memdump(0x1000, 1 << 30)

    assert not isinstance(caught.value, CpuNotStoppedError)


# -- GDBClient.resume ------------------------------------------------------


def test_resume_sends_continue_and_does_not_wait_for_a_stop_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resume` returns on the ACK alone; the stub owes nothing until it stops again."""
    client, fake = connect(monkeypatch, [b"+"])

    client.resume()

    assert fake.sent[-1] == gdb_packet(b"c")


def test_resume_leaves_the_next_request_free_to_proceed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After `resume`, the next packet is sent rather than draining a reply that never comes.

    This is the whole reason `resume` exists next to `continue_execution`.
    With `_owed_reply` left set, the next request resynchronises first and
    eats this read's own answer -- observed as "Discarded abandoned reply to
    b'c': b'f803'" with the clearing line removed. Against a real stub, where
    no reply follows `c` at all, the same resync burns the full socket
    timeout and marks the client permanently unusable.
    """
    client, _fake = connect(monkeypatch, [b"+", b"+", gdb_packet(b"f803")])

    client.resume()
    assert client.read_memory(0x400, 2) == b"\xf8\x03"


def test_continue_execution_still_waits_for_the_stop_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blocking sibling is unchanged: `c` whose answer never arrives still raises."""
    client, _fake = connect(monkeypatch, [b"+", Timeout])

    with pytest.raises(GDBTimeoutError):
        client.continue_execution()


def test_a_stop_after_resume_is_queued_rather_than_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A breakpoint that fires later lands in `pending_stops`, not in someone's read.

    Once `resume` has been called the CPU is free-running, so the next stop
    reply is by definition unsolicited -- which is exactly the case
    `pending_stops` was built for.
    """
    client, _fake = connect(monkeypatch, [b"+", b"+", gdb_packet(b"S05"), gdb_packet(b"f803")])

    client.resume()
    read = client.read_memory(0x400, 2)

    assert read == b"\xf8\x03"
    assert client.pending_stops == (b"S05",)


def test_resume_is_reachable_from_the_public_client() -> None:
    """`resume` is API, not a private helper `read_bulk` reaches through."""
    assert callable(GDBClient.resume)
