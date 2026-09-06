"""Packet framing in `GDBClient`: timeouts, resynchronisation, stray stops.

The wire is NOT a strict request/response alternation. Two things break it,
both reproduced against a live emulator before these tests were written:

1. The stub sends stop replies nobody asked for -- QMP break-on-exec arms
   and immediately activates a breakpoint while the CPU free-runs, so on the
   hit a `$S05#b8` lands mid-exchange. The pre-fix client read that `$` where
   it expected its own ACK and raised `Failed to receive ACK. Got: b'$'`.
2. A request whose reply is abandoned leaves the stub owing bytes that arrive
   later. The pre-fix client read them as the answer to whatever it asked
   next and stayed one packet behind, silently, for the rest of its life.

Every test here drives a fake socket, so the whole matrix -- including the
paths a live emulator will not produce on demand, like a NACK or a reply that
never arrives at all -- is exercised with no emulator involved. The two live
counterparts are in `tests/integration/test_live_session.py`.
"""

import socket
import time

import pytest

from dbxdebug.gdb import (
    DEFAULT_TIMEOUT,
    GDBClient,
    GDBDesyncError,
    GDBTimeoutError,
    looks_like_stop_reply,
)

# The exact qSupported reply a current DOSBox-X build sends.
CURRENT_BUILD_REPLY = (
    b"PacketSize=3fff;swbreak+;hwbreak+;vContSupported+;QStartNoAckMode+;"
    b"dosbox-x-linear-bp+;dosbox-x-eip-offset+"
)

# The BIOS data area's first eight bytes, and the reset vector's sixteen, as
# the stub hexes them: two DIFFERENT lengths, so a reply handed to the wrong
# request is detectable by length alone and not merely by content.
BDA_HEX = b"f803f80200000000"
ROM_HEX = b"ea5be000f030312f30312f393200fc55"


class Timeout:
    """Queued in place of a chunk to make the next `recv` time out."""


def gdb_packet(data: bytes) -> bytes:
    """Wrap `data` as a GDB remote-serial-protocol packet with checksum.

    Args:
        data: The payload to frame.

    Returns:
        `$data#xx` with a correct two-digit hex checksum.
    """
    checksum = sum(data) & 0xFF
    return b"$" + data + b"#" + f"{checksum:02x}".encode()


class FakeSocket:
    """A `socket.socket` stand-in replaying canned reads, timeouts included.

    `recv` ignores the requested size and returns one queued chunk per call.
    A queued `Timeout` raises `TimeoutError` instead -- which is what a real
    socket raises past its `settimeout` deadline, and is the only way to
    exercise the abandoned-reply paths without a live emulator.
    """

    def __init__(self, chunks: list[bytes | type[Timeout]]) -> None:
        """Queue the chunks `recv` will hand out, in order.

        Args:
            chunks: Byte strings to return, and `Timeout` markers to raise
                at. An empty queue reads as a closed connection.
        """
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self._timeout: float | None = None
        self.closed = False

    def connect(self, address: tuple[str, int]) -> None:
        """Accept any address without connecting anywhere."""

    def settimeout(self, timeout: float | None) -> None:
        """Record the timeout the client asked for.

        Args:
            timeout: Seconds, or None for blocking.
        """
        self._timeout = timeout

    def gettimeout(self) -> float | None:
        """Return the recorded timeout.

        Returns:
            Whatever `settimeout` was last given.
        """
        return self._timeout

    def sendall(self, data: bytes) -> None:
        """Record outbound bytes.

        Args:
            data: Bytes the client wrote.
        """
        self.sent.append(data)

    def setsockopt(self, level: int, optname: int, value: int) -> None:
        """No-op: the real client sets TCP_NODELAY on connect."""

    def recv(self, _bufsize: int) -> bytes:
        """Return the next queued chunk, or raise at a queued `Timeout`.

        Args:
            _bufsize: Ignored.

        Returns:
            The next queued chunk, or `b""` once the queue is empty.

        Raises:
            TimeoutError: At a queued `Timeout` marker.
        """
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if chunk is Timeout:
            raise TimeoutError("timed out")
        assert isinstance(chunk, bytes)
        return chunk

    def feed(self, *chunks: bytes | type[Timeout]) -> None:
        """Queue more chunks, as bytes arriving after the reader has looked.

        Args:
            *chunks: Byte strings to return from later `recv` calls, and
                `Timeout` markers to raise at.
        """
        self._chunks.extend(chunks)

    def close(self) -> None:
        """Mark the socket closed."""
        self.closed = True


class ConnectedSocket:
    """A REAL socket presented as one `GDBClient` can connect: `connect` no-ops.

    `FakeSocket` cannot show that the unsolicited-stop poll does not block --
    its `recv` answers instantly whatever the timeout is. This wraps one end
    of a live `socketpair` so that `recv`, `settimeout` and non-blocking mode
    are the kernel's own, which is the only way to prove the poll returns
    while the socket is idle rather than waiting out its 30s deadline.
    """

    def __init__(self, sock: socket.socket) -> None:
        """Wrap an already-connected socket.

        Args:
            sock: The live socket to delegate to.
        """
        self._sock = sock

    def connect(self, address: tuple[str, int]) -> None:
        """Accept any address; the socket is connected already."""

    def setsockopt(self, level: int, optname: int, value: int) -> None:
        """No-op: TCP_NODELAY is meaningless on an AF_UNIX socketpair."""

    def __getattr__(self, name: str) -> object:
        """Delegate everything else -- `recv`, `sendall`, `settimeout` -- to the real socket.

        Args:
            name: Attribute to fetch from the wrapped socket.

        Returns:
            The wrapped socket's attribute.
        """
        return getattr(self._sock, name)


def connect(
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[bytes | type[Timeout]],
    **kwargs: object,
) -> tuple[GDBClient, FakeSocket]:
    """Build a `GDBClient` over a fake socket that replays `chunks`.

    The handshake's own ACK and `qSupported` reply are prepended, so a
    caller only queues what its test needs after connect.

    Args:
        monkeypatch: Used to swap `socket.socket`.
        chunks: Post-handshake chunks.
        **kwargs: Passed to `GDBClient`.

    Returns:
        The connected client and the fake socket behind it.
    """
    fake = FakeSocket([b"+", gdb_packet(CURRENT_BUILD_REPLY), *chunks])
    monkeypatch.setattr("socket.socket", lambda *_args, **_kwargs: fake)
    return GDBClient(**kwargs), fake  # type: ignore[arg-type]


def connect_over_socketpair(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GDBClient, socket.socket]:
    """Build a `GDBClient` over one end of a real `socketpair`.

    The handshake's ACK and `qSupported` reply are written to the far end
    first, so the constructor's blocking read is satisfied without a thread.

    Args:
        monkeypatch: Used to swap `socket.socket`.

    Returns:
        The connected client and the far end of the pair, which stands in
        for the stub.
    """
    stub, ours = socket.socketpair()
    stub.sendall(b"+" + gdb_packet(CURRENT_BUILD_REPLY))
    monkeypatch.setattr("socket.socket", lambda *_a, **_k: ConnectedSocket(ours))
    client = GDBClient()  # type: ignore[call-arg]
    stub.recv(4096)  # the qSupported packet and its ACK, off the wire
    return client, stub


def test_connect_arms_the_default_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client with no `timeout` argument still bounds every read."""
    _client, fake = connect(monkeypatch, [])

    assert fake.gettimeout() == DEFAULT_TIMEOUT


def test_connect_honours_an_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit `timeout` reaches the socket."""
    _client, fake = connect(monkeypatch, [], timeout=2.5)

    assert fake.gettimeout() == 2.5


def test_connect_honours_timeout_none_for_unbounded_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`timeout=None` restores the pre-fix unbounded behaviour, deliberately."""
    _client, fake = connect(monkeypatch, [], timeout=None)

    assert fake.gettimeout() is None


def test_an_unanswered_packet_raises_naming_the_packet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read that times out says which packet went unanswered, and how long it waited."""
    client, _fake = connect(monkeypatch, [Timeout], timeout=4.0)

    with pytest.raises(GDBTimeoutError) as caught:
        client.read_memory(0xFFFF0, 16)

    assert "mffff0,10" in str(caught.value)
    assert "4.0" in str(caught.value)


def test_a_timeout_is_catchable_as_a_plain_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GDBTimeoutError` is a `TimeoutError`, so existing socket-timeout handlers keep working."""
    client, _fake = connect(monkeypatch, [Timeout])

    with pytest.raises(TimeoutError):
        client.read_memory(0xFFFF0, 16)

    assert issubclass(GDBTimeoutError, TimeoutError)
    # The claim the line above rests on: since 3.10 there is only one type.
    assert socket.timeout is TimeoutError


def test_the_request_after_a_timeout_gets_its_own_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """The abandoned exchange is drained, so the next request is answered correctly.

    This is the unit counterpart of the recorded live symptom: without the
    drain, the 8-byte BDA read below comes back with the 16 ROM bytes owed to
    the request that timed out.
    """
    client, _fake = connect(
        monkeypatch,
        [
            Timeout,  # the ROM read's ACK never arrives in time
            b"+",  # ... but it does arrive, late
            gdb_packet(ROM_HEX),  # ... and so does its payload
            b"+",
            gdb_packet(BDA_HEX),
        ],
    )
    with pytest.raises(GDBTimeoutError):
        client.read_memory(0xFFFF0, 16)

    assert client.read_memory(0x400, 8) == bytes.fromhex(BDA_HEX.decode())


def test_a_timeout_after_the_ack_still_drains_only_the_owed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout mid-payload leaves one packet owed, not two."""
    client, _fake = connect(
        monkeypatch,
        [
            b"+",  # the ROM read IS acknowledged
            Timeout,  # ... then its payload is abandoned
            gdb_packet(ROM_HEX),  # ... and arrives late
            b"+",
            gdb_packet(BDA_HEX),
        ],
    )
    with pytest.raises(GDBTimeoutError):
        client.read_memory(0xFFFF0, 16)

    assert client.read_memory(0x400, 8) == bytes.fromhex(BDA_HEX.decode())


def test_a_drain_that_cannot_complete_marks_the_client_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the owed bytes never arrive, every later call fails loudly, forever.

    The alternative -- carrying on and hoping -- is what produced the silent
    one-packet lag in the first place. A client that cannot prove where it
    sits in the stream must not answer.
    """
    client, _fake = connect(monkeypatch, [Timeout, Timeout])
    with pytest.raises(GDBTimeoutError):
        client.read_memory(0xFFFF0, 16)

    with pytest.raises(GDBDesyncError, match="unusable"):
        client.read_memory(0x400, 8)
    # Still unusable on the call after that: the state is permanent, not a
    # one-shot warning that clears itself.
    with pytest.raises(GDBDesyncError, match="unusable"):
        client.read_memory(0x400, 8)


def test_an_unsolicited_stop_before_the_ack_does_not_break_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop reply arriving where the ACK was expected is queued, not mistaken for it.

    This is what QMP break-on-exec produces. The pre-fix client raised
    `Failed to receive ACK. Got: b'$'` here and never recovered.
    """
    client, _fake = connect(
        monkeypatch,
        [gdb_packet(b"S05"), b"+", gdb_packet(ROM_HEX)],
    )

    assert client.read_memory(0xFFFF0, 16) == bytes.fromhex(ROM_HEX.decode())
    assert client.pending_stops == (b"S05",)


def test_an_unsolicited_stop_where_a_payload_was_expected_is_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop reply arriving in the answer's place is queued and the read continues."""
    client, _fake = connect(
        monkeypatch,
        [b"+", gdb_packet(b"T05thread:01;"), gdb_packet(ROM_HEX)],
    )

    assert client.read_memory(0xFFFF0, 16) == bytes.fromhex(ROM_HEX.decode())
    assert client.pending_stops == (b"T05thread:01;",)


def test_a_resuming_packet_still_gets_its_stop_reply_as_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`c` and `s` are answered BY a stop reply, so theirs is not diverted."""
    client, _fake = connect(
        monkeypatch,
        [b"+", gdb_packet(b"S05"), b"+", gdb_packet(b"S05")],
    )

    assert client.continue_execution() == b"S05"
    assert client.step() == b"S05"
    assert client.pending_stops == ()


def test_take_pending_stops_drains_the_queue_and_the_property_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading `pending_stops` is non-destructive; `take_pending_stops` empties it."""
    client, _fake = connect(
        monkeypatch,
        [gdb_packet(b"S05"), gdb_packet(b"S05"), b"+", gdb_packet(ROM_HEX)],
    )
    client.read_memory(0xFFFF0, 16)

    assert client.pending_stops == (b"S05", b"S05")
    assert client.pending_stops == (b"S05", b"S05")
    assert client.take_pending_stops() == [b"S05", b"S05"]
    assert client.pending_stops == ()


def test_an_abandoned_stop_reply_is_kept_rather_than_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Draining a timed-out `c` queues its stop reply instead of dropping it.

    The CPU really did stop; a caller that retries after the timeout would
    otherwise never learn it.
    """
    client, _fake = connect(
        monkeypatch,
        [b"+", Timeout, gdb_packet(b"S05"), b"+", gdb_packet(ROM_HEX)],
    )
    with pytest.raises(GDBTimeoutError):
        client.continue_execution()

    assert client.read_memory(0xFFFF0, 16) == bytes.fromhex(ROM_HEX.decode())
    assert client.pending_stops == (b"S05",)


def test_a_nack_from_the_stub_raises_rather_than_being_read_as_an_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `-` means the stub rejected the packet; that is not an answer."""
    client, _fake = connect(monkeypatch, [b"-"])

    with pytest.raises(GDBDesyncError, match="rejected"):
        client.read_memory(0x400, 8)


def test_a_corrupt_packet_is_nacked_and_the_retransmission_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad checksum gets a `-`, and the resent packet is used."""
    client, fake = connect(
        monkeypatch,
        [b"+", b"$" + BDA_HEX + b"#00", gdb_packet(BDA_HEX)],
    )

    assert client.read_memory(0x400, 8) == bytes.fromhex(BDA_HEX.decode())
    assert b"-" in fake.sent


def test_a_non_hex_checksum_is_treated_as_corrupt_not_as_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage where the checksum belongs is NACKed like any other bad packet."""
    client, fake = connect(
        monkeypatch,
        [b"+", b"$" + BDA_HEX + b"#zz", gdb_packet(BDA_HEX)],
    )

    assert client.read_memory(0x400, 8) == bytes.fromhex(BDA_HEX.decode())
    assert b"-" in fake.sent


def test_leading_garbage_before_a_packet_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bytes that start no token are discarded, as the previous parser did."""
    client, _fake = connect(monkeypatch, [b"+", b"\x00\x07junk" + gdb_packet(BDA_HEX)])

    assert client.read_memory(0x400, 8) == bytes.fromhex(BDA_HEX.decode())


def test_a_packet_split_across_reads_is_reassembled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A payload arriving in pieces, checksum last, still parses."""
    framed = gdb_packet(ROM_HEX)
    client, _fake = connect(monkeypatch, [b"+", framed[:5], framed[5:-2], framed[-2:]])

    assert client.read_memory(0xFFFF0, 16) == bytes.fromhex(ROM_HEX.decode())


def test_a_closed_connection_still_raises_rather_than_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty read is a closed peer, not an empty packet."""
    client, _fake = connect(monkeypatch, [])

    with pytest.raises(ConnectionError, match="closed"):
        client.read_memory(0x400, 8)


@pytest.mark.parametrize("payload", [b"S05", b"T05thread:01;", b"W00", b"X0b"])
def test_stop_replies_are_recognised(payload: bytes) -> None:
    """Every stop reply form the protocol defines is detected."""
    assert looks_like_stop_reply(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b"OK",
        b"E01",
        b"",
        b"S",
        b"Szz",
        ROM_HEX,
        BDA_HEX,
        # A `p` reply for a register whose bytes happen to hex as `53...`:
        # lowercase, so the uppercase test cannot confuse it for `S05`.
        b"53303500",
    ],
)
def test_ordinary_replies_are_not_mistaken_for_stop_replies(payload: bytes) -> None:
    """No answer this stub gives to a non-resuming packet looks like a stop reply."""
    assert not looks_like_stop_reply(payload)


def test_pending_stops_reads_the_socket_rather_than_waiting_for_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queue services itself: nothing else has to read for a stop to show up.

    The queue used to fill only as a side effect of some other request
    passing through the framing layer, so an unsolicited `S05` sat unread in
    the kernel buffer and a caller polling this property spun forever on a
    stop that had genuinely happened (lokkju/dbxdebug#18). No request is
    issued here at all.
    """
    client, _fake = connect(monkeypatch, [gdb_packet(b"S05")])

    assert client.pending_stops == (b"S05",)
    assert client.take_pending_stops() == [b"S05"]
    assert client.pending_stops == ()


def test_take_pending_stops_reads_the_socket_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Draining polls first, so a stop that has arrived is returned, not missed."""
    client, _fake = connect(monkeypatch, [gdb_packet(b"S05"), gdb_packet(b"T05thread:01;")])

    assert client.take_pending_stops() == [b"S05", b"T05thread:01;"]


def test_polling_acknowledges_the_stop_it_picks_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """A packet read by the poll is ACKed like any other, so the stub does not resend."""
    client, fake = connect(monkeypatch, [gdb_packet(b"S05")])
    sent_before = len(fake.sent)

    assert client.pending_stops == (b"S05",)
    assert fake.sent[sent_before:] == [b"+"]


def test_polling_leaves_a_half_arrived_stop_reply_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A packet still in flight is not consumed in pieces; the next poll gets it whole."""
    client, fake = connect(monkeypatch, [b"$S0"])

    assert client.pending_stops == ()

    fake.feed(b"5#b8")
    assert client.pending_stops == (b"S05",)


def test_polling_does_not_touch_a_reply_that_is_still_owed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poll must not consume bytes belonging to a request already on the wire.

    This is the reentrancy hazard the self-servicing queue creates: after a
    `GDBTimeoutError` the stub still owes the abandoned request's reply, and
    those bytes are that request's, not the queue's. Reading them here would
    strand the resynchronisation that the next request performs -- so the
    poll is inert until nothing is owed, and the drain still recovers the
    stop exactly once.
    """
    client, _fake = connect(
        monkeypatch,
        [b"+", Timeout, gdb_packet(b"S05"), b"+", gdb_packet(ROM_HEX)],
    )
    with pytest.raises(GDBTimeoutError):
        client.continue_execution()

    assert client.pending_stops == ()
    assert client.take_pending_stops() == []

    assert client.read_memory(0xFFFF0, 16) == bytes.fromhex(ROM_HEX.decode())
    assert client.pending_stops == (b"S05",)


def test_polling_an_unusable_client_reports_an_empty_queue_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading a queue is not a request, so it does not resurrect a desync error."""
    client, _fake = connect(monkeypatch, [])
    client._unusable = "stream position unknown"

    assert client.pending_stops == ()
    assert client.take_pending_stops() == []


def test_wait_for_stop_returns_the_oldest_stop_and_removes_only_that_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One call yields one stop; anything behind it stays queued."""
    client, _fake = connect(monkeypatch, [gdb_packet(b"S05"), gdb_packet(b"T05thread:01;")])

    assert client.wait_for_stop(timeout=0.0) == b"S05"
    assert client.pending_stops == (b"T05thread:01;",)


def test_wait_for_stop_returns_none_when_nothing_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout is reported as None, not as an exception and not as a spin."""
    client, _fake = connect(monkeypatch, [])

    assert client.wait_for_stop(timeout=0.05, poll=0.01) is None


def test_wait_for_stop_refuses_while_a_reply_is_still_owed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waiting where the poll cannot run would spin forever, so it says so instead.

    The poll is inert while the stub owes an abandoned request's reply, so a
    `wait_for_stop` in that state could never see anything. Reporting it is
    the whole point of lokkju/dbxdebug#18: the original failure was a silent
    spin.
    """
    client, _fake = connect(monkeypatch, [b"+", Timeout, gdb_packet(b"S05")])
    with pytest.raises(GDBTimeoutError):
        client.continue_execution()

    with pytest.raises(GDBDesyncError, match="still owes a reply"):
        client.wait_for_stop(timeout=0.0)


def test_polling_an_idle_socket_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """The poll is genuinely non-blocking, against a real socket rather than a fake.

    `FakeSocket` answers instantly whatever timeout is armed, so it cannot
    tell a non-blocking read from a blocking one. Here the socket is the
    kernel's, the client's read deadline is the full default, and the stub
    end is silent: a blocking read would sit for `DEFAULT_TIMEOUT`.
    """
    client, stub = connect_over_socketpair(monkeypatch)
    try:
        assert client.sock is not None
        assert client.sock.gettimeout() == DEFAULT_TIMEOUT

        started = time.monotonic()
        assert client.pending_stops == ()
        assert client.wait_for_stop(timeout=0.0) is None
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"polling an idle socket took {elapsed:.1f}s"

        # And the timeout the caller armed is restored, not left at zero.
        assert client.sock.gettimeout() == DEFAULT_TIMEOUT
    finally:
        client.close()
        stub.close()


def test_polling_a_real_socket_picks_up_a_stop_and_leaves_the_stream_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Against a real socket: poll, see the stop, then read and get the read's own bytes."""
    client, stub = connect_over_socketpair(monkeypatch)
    try:
        stub.sendall(gdb_packet(b"S05"))
        assert client.wait_for_stop(timeout=5.0, poll=0.01) == b"S05"

        stub.sendall(b"+" + gdb_packet(ROM_HEX))
        assert client.read_memory(0xFFFF0, 16) == bytes.fromhex(ROM_HEX.decode())
        assert client.pending_stops == ()
    finally:
        client.close()
        stub.close()
