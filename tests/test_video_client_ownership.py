"""Client ownership in `DOSVideoTools` and the GDB-backed CLI groups.

The DOSBox-X stub serves one GDB client at a time, so anything that already
holds one must lend it out rather than let a second connection be opened --
that second connection hangs in the `qSupported` exchange instead of failing
(lokkju/dbxdebug#11, #8, #4). These tests pin both halves of the contract: a
BORROWED client is never closed by the borrower, and an OWNED one always is.
A leaked socket or a double close here resurfaces later as an unrelated hang,
which is why it is worth asserting rather than trusting.

The decode tests cover the other half of the same change: one decode path for
text-mode video memory, shared by `DOSVideoTools.screen_dump` and
`DosboxSession.screen_lines` (lokkju/dbxdebug#7).
"""

from typing import Any

import pytest
from click.testing import CliRunner

from dbxdebug.cli import GDB_CLIENT_KEY, main
from dbxdebug.session import DosboxSession
from dbxdebug.video import DOSVideoTools, decode_text_screen

# EAX ECX EDX EBX ESP EBP ESI EDI EIP EFLAGS CS SS DS ES FS GS
_SAMPLE_REGISTERS = [
    1,
    2,
    3,
    4,
    0xFFF0,
    0xFFF0,
    5,
    6,
    0x1234,
    0x202,
    0xF000,
    0x10,
    0x20,
    0x30,
    0x40,
    0x50,
]


class FakeGDBClient:
    """A stand-in for `GDBClient` that records whether it was closed.

    Only the methods these tests drive are implemented. `closed` is the
    assertion surface: the real client has no such flag, and a closed socket
    is not otherwise observable without reaching into it.

    Args:
        host: Recorded so a test can prove where an owned client was pointed.
        port: Likewise.
    """

    DEFAULT_PORT = 2159

    def __init__(self, host: str = "localhost", port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self.closed = False
        self.memory = bytes(4000)

    def read_memory(self, _address: Any, length: int) -> bytes:
        return self.memory[:length].ljust(length, b"\x00")

    def read_register_list(self) -> list[int]:
        return list(_SAMPLE_REGISTERS)

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeGDBClient":
        return self

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        self.close()


def fake_client(*args: Any, **kwargs: Any) -> Any:
    """Build a `FakeGDBClient` typed `Any`, so it can stand in for `GDBClient`.

    The fake deliberately does not subclass the real client -- it implements
    four methods, not the protocol -- so the substitution is made explicit
    here once rather than silenced at every call site.

    Args:
        *args: Passed to `FakeGDBClient`.
        **kwargs: Passed to `FakeGDBClient`.

    Returns:
        A `FakeGDBClient`, typed loosely enough to pass as a client.
    """
    return FakeGDBClient(*args, **kwargs)


def _screen_bytes(text: str, width: int = 80, height: int = 25) -> bytes:
    """Build video memory whose first row spells `text`, attribute 0x07.

    Args:
        text: Characters for row 0; the rest of the screen is zero cells.
        width: Screen columns.
        height: Screen rows.

    Returns:
        Character/attribute interleaved bytes, `width * height * 2` long.
    """
    cells = bytearray()
    for row in range(height):
        for col in range(width):
            char = ord(text[col]) if row == 0 and col < len(text) else 0
            cells += bytes((char, 0x07))
    return bytes(cells)


# --------------------------------------------------------------------------
# DOSVideoTools: borrowed vs owned
# --------------------------------------------------------------------------


def test_borrowed_client_is_not_closed_by_close():
    """The borrower must leave its lender's client open -- the whole point."""
    client = fake_client()
    tools = DOSVideoTools(gdb=client)
    tools.close()
    assert client.closed is False


def test_borrowed_client_is_not_closed_by_leaving_the_with_block():
    client = fake_client()
    with DOSVideoTools(gdb=client):
        pass
    assert client.closed is False


def test_borrowed_client_survives_repeated_closes():
    """No double close reaches the lender, however often `close` is called."""
    client = fake_client()
    tools = DOSVideoTools(gdb=client)
    tools.close()
    tools.close()
    assert client.closed is False


def test_borrowed_client_is_the_very_object_passed_in():
    client = fake_client()
    assert DOSVideoTools(gdb=client).gdb is client


def test_borrowing_reports_no_ownership():
    assert DOSVideoTools(gdb=fake_client()).owns_client is False


def test_owned_client_is_closed_by_close(monkeypatch):
    monkeypatch.setattr("dbxdebug.video.GDBClient", FakeGDBClient)
    tools = DOSVideoTools()
    client: Any = tools.gdb
    tools.close()
    assert client.closed is True


def test_owned_client_is_closed_by_leaving_the_with_block(monkeypatch):
    monkeypatch.setattr("dbxdebug.video.GDBClient", FakeGDBClient)
    with DOSVideoTools() as tools:
        client: Any = tools.gdb
    assert client.closed is True


def test_owning_reports_ownership(monkeypatch):
    monkeypatch.setattr("dbxdebug.video.GDBClient", FakeGDBClient)
    assert DOSVideoTools().owns_client is True


def test_host_and_port_still_reach_an_owned_client_positionally(monkeypatch):
    """The pre-existing call shape must keep working -- this is additive."""
    monkeypatch.setattr("dbxdebug.video.GDBClient", FakeGDBClient)
    client: Any = DOSVideoTools("example.test", 4321).gdb
    assert (client.host, client.port) == ("example.test", 4321)


def test_omitting_host_and_port_uses_the_documented_defaults(monkeypatch):
    monkeypatch.setattr("dbxdebug.video.GDBClient", FakeGDBClient)
    client: Any = DOSVideoTools().gdb
    assert (client.host, client.port) == ("localhost", 2159)


def test_borrowing_a_client_and_naming_a_port_is_refused():
    """Silently ignoring an explicit port would connect elsewhere unasked."""
    with pytest.raises(ValueError, match="not both"):
        DOSVideoTools(port=4321, gdb=fake_client())


def test_borrowing_a_client_and_naming_a_host_is_refused():
    with pytest.raises(ValueError, match="not both"):
        DOSVideoTools(host="example.test", gdb=fake_client())


def test_borrowed_client_actually_serves_the_reads():
    client = fake_client()
    client.memory = _screen_bytes("HELLO")
    with DOSVideoTools(gdb=client) as tools:
        lines = tools.screen_dump()
    assert lines is not None
    assert lines[0].startswith("HELLO")


# --------------------------------------------------------------------------
# One decode path
# --------------------------------------------------------------------------


def test_decode_maps_a_zero_cell_to_a_space():
    assert decode_text_screen(bytes(4000))[0] == " " * 80


def test_decode_discards_the_attribute_byte():
    memory = bytes((ord("A"), 0x4F)) + bytes(3998)
    assert decode_text_screen(memory)[0][0] == "A"


def test_decode_honours_a_non_default_geometry():
    lines = decode_text_screen(bytes(40 * 10 * 2), width=40, height=10)
    assert (len(lines), len(lines[0])) == (10, 40)


def test_decode_pads_a_short_read_with_spaces_rather_than_raising():
    lines = decode_text_screen(bytes((ord("A"), 0x07)))
    assert lines[0] == "A" + " " * 79
    assert len(lines) == 25


def test_decode_leaves_high_bytes_as_their_latin1_code_points():
    """Code page 437 is not mapped; 0xBA comes back as U+00BA, unchanged."""
    assert decode_text_screen(bytes((0xBA, 0x07)) + bytes(3998))[0][0] == chr(0xBA)


def test_screen_dump_and_screen_lines_decode_identically():
    """The duplication in lokkju/dbxdebug#7: one loop, so one result."""
    memory = _screen_bytes("SHARED DECODE PATH")

    video_client = fake_client()
    video_client.memory = memory
    with DOSVideoTools(gdb=video_client) as tools:
        from_video = tools.screen_dump()

    session_client = fake_client()
    session_client.memory = memory
    session = DosboxSession()
    session.gdb = session_client

    assert from_video == session.screen_lines()


def test_screen_dump_with_ticks_uses_the_same_decode():
    client = fake_client()
    client.memory = _screen_bytes("TICKS")
    with DOSVideoTools(gdb=client) as tools:
        lines, _ticks = tools.screen_dump_with_ticks()
        assert lines == tools.screen_dump()


# --------------------------------------------------------------------------
# CLI groups: borrowed vs owned
# --------------------------------------------------------------------------


def test_cpu_regs_borrows_a_client_from_the_context_object():
    client = fake_client()
    result = CliRunner().invoke(main, ["cpu", "regs"], obj={GDB_CLIENT_KEY: client})
    assert result.exit_code == 0, result.output
    assert "EIP=00001234" in result.output


def test_cpu_regs_does_not_close_the_client_it_borrowed():
    client = fake_client()
    CliRunner().invoke(main, ["cpu", "regs"], obj={GDB_CLIENT_KEY: client})
    assert client.closed is False


def test_mem_read_borrows_a_client_from_the_context_object():
    client = fake_client()
    client.memory = b"\xde\xad\xbe\xef" + bytes(3996)
    result = CliRunner().invoke(
        main, ["mem", "read", "0xb8000", "4", "--hex"], obj={GDB_CLIENT_KEY: client}
    )
    assert result.exit_code == 0, result.output
    assert "deadbeef" in result.output.lower()
    assert client.closed is False


def test_screen_show_borrows_a_client_from_the_context_object():
    client = fake_client()
    client.memory = _screen_bytes("BORROWED SCREEN")
    result = CliRunner().invoke(main, ["screen", "show"], obj={GDB_CLIENT_KEY: client})
    assert result.exit_code == 0, result.output
    assert "BORROWED SCREEN" in result.output
    assert client.closed is False


def test_screen_info_borrows_and_leaves_the_client_open():
    client = fake_client()
    result = CliRunner().invoke(main, ["screen", "info"], obj={GDB_CLIENT_KEY: client})
    assert result.exit_code == 0, result.output
    assert client.closed is False


def test_a_borrowing_cli_command_never_opens_a_socket(monkeypatch):
    """The hang in lokkju/dbxdebug#11 was a second connect. There must be none."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("a borrowing command must not open its own connection")

    monkeypatch.setattr("socket.socket", _boom)
    result = CliRunner().invoke(main, ["cpu", "regs"], obj={GDB_CLIENT_KEY: fake_client()})
    assert result.exit_code == 0, result.output


def test_without_a_borrowed_client_the_cli_still_builds_its_own(monkeypatch):
    """Standalone behaviour is unchanged: no client in `obj` means own one."""
    built: list[FakeGDBClient] = []

    def _factory(host: str, port: int) -> FakeGDBClient:
        client = FakeGDBClient(host, port)
        built.append(client)
        return client

    monkeypatch.setattr("dbxdebug.cli.GDBClient", _factory)
    result = CliRunner().invoke(main, ["cpu", "--port", "4321", "regs"])
    assert result.exit_code == 0, result.output
    assert [(c.host, c.port) for c in built] == [("localhost", 4321)]


def test_an_owned_cli_client_is_closed_when_the_command_ends(monkeypatch):
    built: list[FakeGDBClient] = []

    def _factory(host: str, port: int) -> FakeGDBClient:
        client = FakeGDBClient(host, port)
        built.append(client)
        return client

    monkeypatch.setattr("dbxdebug.cli.GDBClient", _factory)
    CliRunner().invoke(main, ["cpu", "regs"])
    assert [c.closed for c in built] == [True]
