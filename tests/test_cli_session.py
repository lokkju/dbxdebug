"""Tests for the CLI's `session` group and `doctor` command.

These drive `cli.main` through click's `CliRunner`, never a real emulator.
Every test that touches the registry points `DBXDEBUG_REGISTRY` at a scratch
directory so it can never read or write the real one.
"""

import pytest
from click.testing import CliRunner

from dbxdebug.cli import main


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """Point every test at a scratch registry, never the real one."""
    monkeypatch.setenv("DBXDEBUG_REGISTRY", str(tmp_path / "registry"))
    return tmp_path


# --------------------------------------------------------------------------
# session list
# --------------------------------------------------------------------------


def test_session_list_on_empty_registry_exits_zero():
    runner = CliRunner()
    result = runner.invoke(main, ["session", "list"])
    assert result.exit_code == 0


def test_session_list_on_empty_registry_reports_no_sessions():
    runner = CliRunner()
    result = runner.invoke(main, ["session", "list"])
    assert "no registered" in result.output.lower()


def test_session_list_help_works():
    runner = CliRunner()
    result = runner.invoke(main, ["session", "list", "--help"])
    assert result.exit_code == 0


# --------------------------------------------------------------------------
# session reap
# --------------------------------------------------------------------------


def test_session_reap_help_works():
    runner = CliRunner()
    result = runner.invoke(main, ["session", "reap", "--help"])
    assert result.exit_code == 0
    assert "reap" in result.output.lower()


def test_session_reap_on_empty_registry_exits_zero():
    runner = CliRunner()
    result = runner.invoke(main, ["session", "reap"])
    assert result.exit_code == 0
    assert "nothing to reap" in result.output.lower()


def test_session_reap_dry_run_flag_accepted():
    runner = CliRunner()
    result = runner.invoke(main, ["session", "reap", "--dry-run"])
    assert result.exit_code == 0


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def test_doctor_runs_and_exits_zero_without_an_emulator(monkeypatch):
    # Guarantee "no emulator" regardless of what's on this host or its PATH.
    monkeypatch.setenv("DBXDEBUG_DOSBOX", "/nonexistent/dosbox-x")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0


def test_doctor_reports_missing_binary(monkeypatch):
    monkeypatch.setenv("DBXDEBUG_DOSBOX", "/nonexistent/dosbox-x")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])
    assert "not found" in result.output.lower()


def test_doctor_reports_cpu_count():
    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])
    assert "cpu" in result.output.lower()


def test_doctor_reports_registry_writable():
    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])
    assert "registry" in result.output.lower()


def test_doctor_never_starts_an_emulator(monkeypatch):
    """`doctor` must not launch DOSBox-X -- assert Popen is never called."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("doctor must not launch a subprocess")

    monkeypatch.setattr("subprocess.Popen", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0


def test_doctor_detects_remote_debug_marker_binary(monkeypatch, tmp_path):
    fake_binary = tmp_path / "dosbox-x"
    fake_binary.write_bytes(b"some header bytes gdbserver port qmpserver port trailing")
    fake_binary.chmod(0o755)
    monkeypatch.setenv("DBXDEBUG_DOSBOX", str(fake_binary))
    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "compiled in" in result.output.lower() or "remote debug" in result.output.lower()


def test_doctor_flags_binary_missing_remote_debug_markers(monkeypatch, tmp_path):
    fake_binary = tmp_path / "dosbox-x"
    fake_binary.write_bytes(b"a stock build with none of the markers")
    fake_binary.chmod(0o755)
    monkeypatch.setenv("DBXDEBUG_DOSBOX", str(fake_binary))
    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "stock" in result.output.lower() or "no gdbserver" in result.output.lower()


# --------------------------------------------------------------------------
# cpu regs: the PC= trap fix
# --------------------------------------------------------------------------


def _gdb_packet(data: bytes) -> bytes:
    checksum = sum(data) & 0xFF
    return b"$" + data + b"#" + f"{checksum:02x}".encode()


def _encode_g_reply(values: list[int]) -> bytes:
    return b"".join(value.to_bytes(4, "little").hex().encode() for value in values)


class _FakeSocket:
    """Minimal stand-in for `socket.socket` replaying canned reads."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self._timeout: float | None = None

    def connect(self, address):
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


_CURRENT_BUILD_REPLY = (
    b"PacketSize=3fff;swbreak+;hwbreak+;vContSupported+;QStartNoAckMode+;"
    b"dosbox-x-linear-bp+;dosbox-x-eip-offset+"
)

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
    0x1234,  # eip -- an offset within cs, not a linear address
    0x202,
    0xF000,  # cs
    0x10,
    0x20,
    0x30,
    0x40,
    0x50,
]


def test_cpu_regs_prints_pc_line_derived_from_cs_and_eip(monkeypatch):
    fake = _FakeSocket(
        [
            b"+",
            _gdb_packet(_CURRENT_BUILD_REPLY),
            b"+",
            _gdb_packet(_encode_g_reply(_SAMPLE_REGISTERS)),
        ]
    )
    monkeypatch.setattr("socket.socket", lambda *_args, **_kwargs: fake)

    runner = CliRunner()
    result = runner.invoke(main, ["cpu", "regs"])

    assert result.exit_code == 0, result.output
    expected_pc = 0xF000 * 16 + 0x1234
    assert f"PC={expected_pc:08X}" in result.output
    # The bare EIP line must still be present but must not be the only
    # program-counter-shaped value on screen.
    assert "EIP=00001234" in result.output
