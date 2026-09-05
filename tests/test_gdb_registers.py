"""Register semantics in `GDBClient`: the `g`/`G`/`P` register list.

DOSBox-X's GDB stub was fixed so that register 8 (EIP) is an *offset within
CS*, not a linear address. Older builds returned `SegPhys(cs) + reg_eip`
from the `g` packet while `G` wrote `reg_eip` verbatim, so a `g`/`G`
round-trip silently moved the program counter. These tests exercise the
register list decode/encode and the client-side linear PC computation
against a fake socket, with no emulator involved.
"""

import pytest

from dbxdebug.gdb import GDBClient

# The exact qSupported reply a current DOSBox-X build sends.
CURRENT_BUILD_REPLY = (
    b"PacketSize=3fff;swbreak+;hwbreak+;vContSupported+;QStartNoAckMode+;"
    b"dosbox-x-linear-bp+;dosbox-x-eip-offset+"
)

# The 16 registers `g`/`G` exchange, in order: EAX, ECX, EDX, EBX, ESP, EBP,
# ESI, EDI, EIP, EFLAGS, CS, SS, DS, ES, FS, GS.
SAMPLE_REGISTERS = [
    0x00000001,  # eax
    0x00000002,  # ecx
    0x00000003,  # edx
    0x00000004,  # ebx
    0x0000FFF0,  # esp
    0x0000FFF0,  # ebp
    0x00000005,  # esi
    0x00000006,  # edi
    0x00001234,  # eip -- an OFFSET within cs, not a linear address
    0x00000202,  # eflags
    0x0000F000,  # cs
    0x00000010,  # ss
    0x00000020,  # ds
    0x00000030,  # es
    0x00000040,  # fs
    0x00000050,  # gs
]


def _gdb_packet(data: bytes) -> bytes:
    """Wrap `data` as a GDB remote-serial-protocol packet with checksum."""
    checksum = sum(data) & 0xFF
    return b"$" + data + b"#" + f"{checksum:02x}".encode()


def _encode_g_reply(values: list[int]) -> bytes:
    """Encode a list of 32-bit register values as a `g` packet reply."""
    return b"".join(value.to_bytes(4, "little").hex().encode() for value in values)


class FakeSocket:
    """A minimal stand-in for `socket.socket` that replays canned reads.

    `recv` ignores the requested buffer size and returns one queued chunk
    per call, which is enough to drive `GDBClient`'s packet parser through
    the ack-then-packet sequence it expects.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self._timeout: float | None = None

    def connect(self, address: tuple[str, int]) -> None:
        pass

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def gettimeout(self) -> float | None:
        return self._timeout

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _bufsize: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self) -> None:
        pass


def _connect(
    monkeypatch: pytest.MonkeyPatch, reply: bytes = CURRENT_BUILD_REPLY, **kwargs
) -> tuple[GDBClient, FakeSocket]:
    """Connect a `GDBClient` against a fake socket replaying `reply`."""
    fake = FakeSocket([b"+", _gdb_packet(reply)])
    monkeypatch.setattr("socket.socket", lambda *_args, **_kwargs: fake)
    client = GDBClient(**kwargs)
    return client, fake


def test_read_register_list_decodes_sixteen_little_endian_words(
    monkeypatch: pytest.MonkeyPatch,
):
    client, fake = _connect(monkeypatch)
    fake._chunks.extend([b"+", _gdb_packet(_encode_g_reply(SAMPLE_REGISTERS))])
    assert client.read_register_list() == SAMPLE_REGISTERS


def test_read_register_list_sends_g_packet(monkeypatch: pytest.MonkeyPatch):
    client, fake = _connect(monkeypatch)
    fake._chunks.extend([b"+", _gdb_packet(_encode_g_reply(SAMPLE_REGISTERS))])
    client.read_register_list()
    assert fake.sent[-2] == _gdb_packet(b"g")


def test_read_registers_eip_key_holds_the_offset_not_a_linear_address(
    monkeypatch: pytest.MonkeyPatch,
):
    """`registers["eip"]` is an offset within CS, not a linear address --
    that changed meaning when the stub was fixed. Old code that treated it
    as a linear address is now silently wrong; this test pins the new,
    correct meaning."""
    client, fake = _connect(monkeypatch)
    fake._chunks.extend([b"+", _gdb_packet(_encode_g_reply(SAMPLE_REGISTERS))])
    registers = client.read_registers()
    assert registers["eip"] == 0x00001234
    assert registers["cs"] == 0x0000F000


def test_read_registers_preserves_existing_shape_and_key_names(
    monkeypatch: pytest.MonkeyPatch,
):
    client, fake = _connect(monkeypatch)
    fake._chunks.extend([b"+", _gdb_packet(_encode_g_reply(SAMPLE_REGISTERS))])
    registers = client.read_registers()
    assert set(registers) == {
        "eax",
        "ecx",
        "edx",
        "ebx",
        "esp",
        "ebp",
        "esi",
        "edi",
        "eip",
        "eflags",
        "cs",
        "ss",
        "ds",
        "es",
        "fs",
        "gs",
    }


def test_linear_pc_combines_cs_and_eip(monkeypatch: pytest.MonkeyPatch):
    client, fake = _connect(monkeypatch)
    fake._chunks.extend([b"+", _gdb_packet(_encode_g_reply(SAMPLE_REGISTERS))])
    assert client.linear_pc() == 0x0000F000 * 16 + 0x00001234


def test_write_register_frames_a_p_packet_with_little_endian_hex(
    monkeypatch: pytest.MonkeyPatch,
):
    client, fake = _connect(monkeypatch)
    fake._chunks.extend([b"+", _gdb_packet(b"OK")])
    result = client.write_register(8, 0x00001234)
    assert result is True
    assert fake.sent[-2] == _gdb_packet(b"P8=34120000")


def test_write_register_returns_false_on_error_reply(monkeypatch: pytest.MonkeyPatch):
    client, fake = _connect(monkeypatch)
    fake._chunks.extend([b"+", _gdb_packet(b"E01")])
    result = client.write_register(8, 0x00001234)
    assert result is False
