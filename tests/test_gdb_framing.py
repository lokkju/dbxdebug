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

    def close(self) -> None:
        """Mark the socket closed."""
        self.closed = True


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
