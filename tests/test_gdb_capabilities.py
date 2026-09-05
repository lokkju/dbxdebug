"""GDB capability handshake: the change that makes the vendor flags load-bearing.

DOSBox-X advertises `dosbox-x-linear-bp+` and `dosbox-x-eip-offset+` in its
`qSupported` reply, but nothing reads them until now. Neither semantics
change is detectable by probing -- `Z0` answers `OK` whether the stub reads
a linear address or a packed far pointer, and a breakpoint above 64 KB set
against an old stub is silently stored at a garbage location. These tests
exercise the handshake against a fake socket, with no emulator involved.
"""

import pytest

from dbxdebug.addressing import PackedAddressError
from dbxdebug.gdb import GDBClient, IncompatibleStubError

# The exact qSupported reply a current DOSBox-X build sends.
CURRENT_BUILD_REPLY = (
    b"PacketSize=3fff;swbreak+;hwbreak+;vContSupported+;QStartNoAckMode+;"
    b"dosbox-x-linear-bp+;dosbox-x-eip-offset+"
)

# What an old, pre-fix build sends: no vendor features at all.
OLD_BUILD_REPLY = b"PacketSize=3fff;swbreak+;hwbreak+;vContSupported+;QStartNoAckMode+"


def _gdb_packet(data: bytes) -> bytes:
    """Wrap `data` as a GDB remote-serial-protocol packet with checksum."""
    checksum = sum(data) & 0xFF
    return b"$" + data + b"#" + f"{checksum:02x}".encode()


class FakeSocket:
    """A minimal stand-in for `socket.socket` that replays canned reads.

    `recv` ignores the requested buffer size and returns one queued chunk
    per call, which is enough to drive `GDBClient`'s packet parser through
    the ack-then-packet sequence it expects.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []

    def connect(self, address: tuple[str, int]) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _bufsize: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self) -> None:
        pass


def _connect(
    monkeypatch: pytest.MonkeyPatch, reply: bytes, **kwargs
) -> tuple[GDBClient, FakeSocket]:
    """Connect a `GDBClient` against a fake socket replaying `reply`."""
    fake = FakeSocket([b"+", _gdb_packet(reply)])
    monkeypatch.setattr("socket.socket", lambda *_args, **_kwargs: fake)
    client = GDBClient(**kwargs)
    return client, fake


def test_sends_qsupported_multiprocess_on_connect(monkeypatch: pytest.MonkeyPatch):
    _client, fake = _connect(monkeypatch, CURRENT_BUILD_REPLY)
    assert fake.sent[0] == _gdb_packet(b"qSupported:multiprocess+")


def test_capabilities_parsed_from_reply_with_both_vendor_features(monkeypatch: pytest.MonkeyPatch):
    client, _fake = _connect(monkeypatch, CURRENT_BUILD_REPLY)
    assert "dosbox-x-linear-bp+" in client.capabilities
    assert "dosbox-x-eip-offset+" in client.capabilities
    assert "swbreak+" in client.capabilities
    assert "PacketSize=3fff" in client.capabilities


def test_require_linear_breakpoints_passes_when_present(monkeypatch: pytest.MonkeyPatch):
    client, _fake = _connect(monkeypatch, CURRENT_BUILD_REPLY)
    client.require_linear_breakpoints()  # must not raise


def test_connect_raises_when_linear_bp_capability_absent(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(IncompatibleStubError, match="dosbox-x-linear-bp"):
        _connect(monkeypatch, OLD_BUILD_REPLY)


def test_require_linear_breakpoints_raises_naming_missing_feature(monkeypatch: pytest.MonkeyPatch):
    client, _fake = _connect(monkeypatch, OLD_BUILD_REPLY, require_capabilities=False)
    with pytest.raises(IncompatibleStubError, match="dosbox-x-linear-bp"):
        client.require_linear_breakpoints()


def test_require_capabilities_false_suppresses_the_connect_check(monkeypatch: pytest.MonkeyPatch):
    client, _fake = _connect(monkeypatch, OLD_BUILD_REPLY, require_capabilities=False)
    assert "dosbox-x-linear-bp+" not in client.capabilities


def test_set_breakpoint_rejects_a_packed_far_pointer_before_sending(
    monkeypatch: pytest.MonkeyPatch,
):
    client, fake = _connect(monkeypatch, CURRENT_BUILD_REPLY)
    sent_before = len(fake.sent)
    with pytest.raises(PackedAddressError):
        client.set_breakpoint(0x08245A90)
    assert len(fake.sent) == sent_before


def test_read_memory_still_accepts_a_bare_hex_linear_string(monkeypatch: pytest.MonkeyPatch):
    """Existing CLI call sites pass bare hex/decimal strings with no colon
    (e.g. "0x1000"); routing through addressing.parse_address must not
    regress that, since parse_address alone rejects strings without ":"."""
    client, fake = _connect(monkeypatch, CURRENT_BUILD_REPLY)
    fake._chunks.extend([b"+", _gdb_packet(b"deadbeef")])
    data = client.read_memory("0x1000", 4)
    assert data == bytes.fromhex("deadbeef")
