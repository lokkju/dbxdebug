"""What this library does against a REAL DOSBox-X, not a fake.

The rest of the suite proves framing and arithmetic against fakes. These
tests prove the library actually drives an emulator: that the stub advertises
what `GDBClient` demands of it, that `eip` really is an offset, that a
breakpoint above 64 KB really fires, that `memdump` really refuses while the
CPU runs, and that `frames.steps_out` really stops after a 16-bit epilogue's
`ret` rather than on it.

Four of them are about the wire itself. The wire is not a strict
request/response alternation -- an unrequested stop reply or an abandoned
reply breaks it -- so the last four tests pin down the control case where the
alternation does hold, the two known ways it breaks, and the read timeout
that bounds a request the stub will never answer. Two of them asserted the
old, broken behaviour until the framing rework landed; their docstrings say
what changed. Fake-socket coverage of the same paths, including the ones a
live emulator will not produce on demand, is in `tests/test_gdb_framing.py`.

Every test gets its OWN emulator, launched and torn down by the fixtures in
`conftest.py`. They are marked `integration` and are excluded from the
default `pytest` run (see `addopts` in `pyproject.toml`), because CI has no
emulator to run them against. Opt in with:

    uv run pytest -m integration tests/integration -v

The values asserted here were observed live against the `remotedebug` build
of DOSBox-X; where a value could be read from the guest instead of hardcoded
(the INT 08h vector, the COM program's load segment) it is read, so only
genuinely fixed things -- the reset vector's contents, the stub's capability
tokens -- are written down as constants.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from dbxdebug.addressing import linear
from dbxdebug.frames import steps_out, walk_frames
from dbxdebug.gdb import GDBClient
from dbxdebug.qmp import QMPClient, QMPError
from dbxdebug.session import DosboxSession

pytestmark = pytest.mark.integration

# The 8086 reset vector and the 16 bytes that follow it: a far JMP into the
# BIOS, then the BIOS date string "01/01/92". ROM, so it is stable across the
# whole life of a session and identical in every session -- which is what
# makes it usable as a "did this reply belong to this request" probe.
BIOS_RESET_VECTOR = 0xFFFF0
BIOS_RESET_BYTES = bytes.fromhex("ea5be000f030312f30312f393200fc55")

# IVT slot for INT 08h, the timer tick. Its handler lives in the F000 ROM
# segment, so its LINEAR address is above 64 KB, and the tick fires ~18.2
# times a second, so a breakpoint on it is hit almost immediately.
INT08_VECTOR_SLOT = 0x08 * 4

# Highest linear address reachable by a 16-bit offset alone. A breakpoint
# above this is the one a stub without `dosbox-x-linear-bp+` gets wrong.
FIRST_64K = 0x10000

# The two vendor capabilities this package's correctness depends on.
LINEAR_BP_CAPABILITY = "dosbox-x-linear-bp+"
EIP_OFFSET_CAPABILITY = "dosbox-x-eip-offset+"

# What a booted guest shows once autoexec has switched to the mounted drive.
DOS_PROMPT = "C:\\>"

# A hand-assembled .COM that calls a textbook 16-bit routine forever. Written
# out as bytes rather than assembled, so the test needs no assembler and the
# offsets below are exact.
#
#   0100  31 C0        xor ax,ax
#   0102  8E C0        mov es,ax
#   0104  8C C8        mov ax,cs
#   0106  26 A3 00 05  mov es:[0500],ax   ; stamp CS where the host can see it
#   010A  E8 03 00     call 0110
#   010D  EB FB        jmp 010A           ; <- where a real `ret` lands
#   010F  90           nop                ; padding, never executed
#   0110  55           push bp            ; prologue
#   0111  89 E5        mov bp,sp
#   0113  83 EC 04     sub sp,4           ; four bytes of locals
#   0116  90           nop                ; <- breakpoint: prologue is done
#   0117  89 EC        mov sp,bp          ; epilogue
#   0119  5D           pop bp             ; SP lands on BP+2 here
#   011A  C3           ret                ; only THIS carries SP past BP+2
FRAME_COM = bytes(
    [
        0x31, 0xC0,
        0x8E, 0xC0,
        0x8C, 0xC8,
        0x26, 0xA3, 0x00, 0x05,
        0xE8, 0x03, 0x00,
        0xEB, 0xFB,
        0x90,
        0x55,
        0x89, 0xE5,
        0x83, 0xEC, 0x04,
        0x90,
        0x89, 0xEC,
        0x5D,
        0xC3,
    ]
)  # fmt: skip

# Guest name of the staged program, and the offsets inside it that matter.
FRAME_COM_NAME = "FRAME.COM"
FRAME_STAMP_ADDRESS = 0x500
FRAME_BODY_OFFSET = 0x116
FRAME_AFTER_CALL_OFFSET = 0x10D

# Wall seconds to wait for the guest to boot far enough to have run the
# staged program and stamped its CS.
FRAME_STAMP_TIMEOUT = 40.0

# Where a .COM image is loaded within its segment, and enough of FRAME.COM's
# opening bytes to identify it there. The CS stamp alone does not prove the
# segment it names holds FRAME.COM -- see the check that uses these.
COM_LOAD_OFFSET = 0x100
FRAME_COM_SIGNATURE = FRAME_COM[:10]

# A .COM that terminates the instant it starts: at the .COM entry offset a
# lone `ret` returns to PSP:0000, which holds the INT 20h that DOS puts
# there. Driven by the batch file below, the guest performs a DOS program
# launch several times a second with no keystroke from the host -- which is
# what an armed break-on-exec needs in order to fire.
EXIT_COM = bytes([0xC3])
EXIT_COM_NAME = "EXITNOW.COM"
EXEC_LOOP_NAME = "EXECLOOP.BAT"
EXEC_LOOP_BAT = ":top\r\nEXITNOW\r\ngoto top\r\n"

# Wall seconds to wait for an armed break-on-exec to actually stop the CPU.
BREAK_ON_EXEC_TIMEOUT = 30.0

# Wall seconds allowed for the prompt to appear, and the bound the observed
# time is checked against.
PROMPT_TIMEOUT = 40.0

# A second, differently sized read used as the "did this reply belong to
# this request" probe for the stream-shift test: the BIOS data area, whose
# first eight bytes are the four COM port base addresses.
BDA_ADDRESS = 0x400
BDA_LENGTH = 8

# A socket timeout no localhost round-trip can meet, used to abandon a reply
# on purpose. Not zero -- zero would put the socket in non-blocking mode.
IMPOSSIBLE_SOCKET_TIMEOUT = 0.0005

# Wall seconds allowed for a request the stub genuinely will not answer. Long
# enough that a slow-but-alive stub is not mistaken for a silent one, short
# enough that the test does not sit on the package's 30s default.
UNANSWERED_REQUEST_TIMEOUT = 3.0

# `DosboxSession.boot_settle` defaults to 2.5s so that a caller reading the
# guest's SCREEN right after `start()` cannot capture the DOSBox-X boot
# banner. A test asserting only on ROM, registers, capabilities, QMP status
# or host process state depends on nothing the guest's boot progress can
# change, so it skips that wait. Every test that does need a booted DOS --
# a `C:\>` prompt, a staged program, a DOS EXEC, or the BIOS-populated IVT --
# keeps the default deliberately: shaving the wait there would turn a slow
# boot into a flaky assertion, or into a 30s socket timeout on a breakpoint
# set from a vector that was not written yet.
NO_BOOT_SETTLE = 0.0


def clients(session: DosboxSession) -> tuple[GDBClient, QMPClient]:
    """Return a started session's two debug clients, asserting both connected.

    Args:
        session: A started session.

    Returns:
        Its `(gdb, qmp)` clients.
    """
    assert session.gdb is not None, "no GDB client connected"
    assert session.qmp is not None, "no QMP client connected"
    return session.gdb, session.qmp


def int08_handler(gdb: GDBClient) -> tuple[int, int]:
    """Read the INT 08h handler's `seg:off` out of the guest's IVT.

    Args:
        gdb: A connected GDB client.

    Returns:
        `(seg, off)` as stored in the vector table.
    """
    off, seg = struct.unpack("<HH", gdb.read_memory(INT08_VECTOR_SLOT, 4))
    return seg, off


def test_session_starts_and_both_clients_connect(
    make_session: Callable[..., DosboxSession],
) -> None:
    """A session comes up with a live process and both debug clients attached."""
    session = make_session(boot_settle=NO_BOOT_SETTLE)
    gdb, qmp = clients(session)

    assert session.running
    assert session.pid is not None
    assert session.gdb_port != session.qmp_port
    assert qmp.query_status()["running"] is True
    assert gdb.read_memory(BIOS_RESET_VECTOR, len(BIOS_RESET_BYTES)) == BIOS_RESET_BYTES


def test_stub_advertises_both_vendor_capabilities(
    make_session: Callable[..., DosboxSession],
) -> None:
    """The stub advertises linear breakpoints AND offset-style `eip`."""
    gdb, _ = clients(make_session(boot_settle=NO_BOOT_SETTLE))

    assert LINEAR_BP_CAPABILITY in gdb.capabilities
    assert EIP_OFFSET_CAPABILITY in gdb.capabilities


def test_eip_is_an_offset_within_cs_not_a_linear_address(
    make_session: Callable[..., DosboxSession],
) -> None:
    """`registers["eip"]` is an offset; `linear_pc()` is `cs * 16 + eip`.

    The Stage 1 register-semantics fix, checked against the machine: an
    older stub returned `SegPhys(cs) + reg_eip` from `g`, which would make
    `eip` alone exceed a 16-bit offset and make the identity below fail.
    """
    gdb, _ = clients(make_session(boot_settle=NO_BOOT_SETTLE))
    # Halt first: a free-running CPU would move between the two reads and
    # the comparison would be meaningless (or flaky, which is worse).
    gdb.halt()

    registers = gdb.read_registers()
    eip, cs = registers["eip"], registers["cs"]

    assert eip < 0x10000, f"eip={eip:#x} is not a 16-bit offset"
    assert gdb.linear_pc() == cs * 16 + eip


def test_breakpoint_above_64k_fires_where_it_was_set(
    make_session: Callable[..., DosboxSession],
) -> None:
    """A `seg:off` breakpoint whose LINEAR address exceeds 64 KB actually fires.

    This is the whole point of `dosbox-x-linear-bp+`: a stub that decoded
    `Z0`'s address as a packed far pointer would answer OK here and never
    stop.
    """
    session = make_session()
    gdb, _ = clients(session)
    seg, off = int08_handler(gdb)
    target = linear(seg, off)
    assert target > FIRST_64K, f"INT 08h handler at {seg:04X}:{off:04X} is not above 64 KB"

    gdb.halt()
    assert session.set_breakpoint(seg, off)
    reply = gdb.continue_execution()

    assert reply == b"S05", f"unexpected stop reply {reply!r}"
    assert gdb.linear_pc() == target
    assert session.remove_breakpoint(seg, off)


def test_memdump_matches_gdb_read_byte_for_byte(
    make_session: Callable[..., DosboxSession],
) -> None:
    """The bulk QMP read and the GDB `m` read return the same bytes."""
    session = make_session(boot_settle=NO_BOOT_SETTLE)
    gdb, qmp = clients(session)
    gdb.halt()

    dumped = qmp.memdump(BIOS_RESET_VECTOR - 0x100, 0x110)
    read = gdb.read_memory(BIOS_RESET_VECTOR - 0x100, 0x110)

    assert isinstance(dumped, bytes)
    assert dumped == read
    assert dumped.endswith(BIOS_RESET_BYTES)


def test_memdump_refuses_while_the_cpu_is_running(
    make_session: Callable[..., DosboxSession],
) -> None:
    """`memdump` on a running CPU is refused with an error, not a torn read.

    Stage 1 hardening: the dump runs off the socket thread, so DOSBox-X
    requires the CPU stopped -- GDB-halted or QMP-stopped -- rather than
    racing the emulation thread.
    """
    session = make_session(boot_settle=NO_BOOT_SETTLE)
    _, qmp = clients(session)
    assert qmp.query_status()["running"] is True

    with pytest.raises(QMPError) as caught:
        qmp.memdump(BIOS_RESET_VECTOR, 16)

    assert "stopped" in str(caught.value)


def test_wait_for_text_observes_the_prompt(make_session: Callable[..., DosboxSession]) -> None:
    """`wait_for_text` returns an observed time, not None."""
    session = make_session()

    observed = session.wait_for_text(DOS_PROMPT, timeout=PROMPT_TIMEOUT)

    assert observed is not None, "the guest never reached a C:\\> prompt"
    # A real bound, unlike `observed >= 0.0`, which a rounded elapsed time
    # cannot fail: `wait_for_text` only returns a time from inside its own
    # deadline, so anything above the timeout means it measured wrongly.
    assert observed <= PROMPT_TIMEOUT


def test_context_exit_kills_the_process_and_removes_the_workdir(
    session_builder: Callable[..., DosboxSession],
) -> None:
    """Leaving the `with` block leaves no process and no scratch directory."""
    session = session_builder(boot_settle=NO_BOOT_SETTLE)
    with session:
        assert session.running
        assert session.pid is not None
        assert session.workdir is not None
        assert session.workdir.is_dir()

    # `stop()` leaves `pid` and `workdir` on the handle, so what they named
    # can still be checked after the block that destroyed both.
    pid, workdir = session.pid, session.workdir
    assert pid is not None
    assert workdir is not None
    assert not session.running
    assert not Path(f"/proc/{pid}").exists()
    assert not workdir.exists()


def test_strictly_serialised_requests_each_get_their_own_reply(
    make_session: Callable[..., DosboxSession],
) -> None:
    """Every reply matches its request while nothing else touches the stream.

    WHAT THIS COVERS: bursts of `m`/`g`/`p` packets against a free-running
    CPU, and continue/stop cycles with register and memory reads after each
    stop -- all issued strictly one at a time, each reply read before the
    next request is sent, with no breakpoint able to fire unrequested. Every
    probe reads the ROM reset vector, whose bytes are fixed, so a stale or
    shifted payload is DETECTABLE rather than merely inconsistent.

    WHAT THIS DOES NOT COVER, despite an earlier version of this test
    claiming it did: the historically recorded symptom that `frames.py`
    describes, where "a single `m` right after a run of packets came back
    with the previous response's payload". Producing that needs something to
    put an extra packet on the wire, or to abandon a reply that has already
    been requested; strict serialisation does neither, so passing here is no
    evidence at all that the symptom is gone. The two known ways to produce
    it each have their own test below. This one is the control: it shows the
    framing is correct when nothing disturbs the stream, which is what makes
    those two tests interpretable.
    """
    session = make_session()
    gdb, _ = clients(session)
    baseline = gdb.read_memory(BIOS_RESET_VECTOR, len(BIOS_RESET_BYTES))
    assert baseline == BIOS_RESET_BYTES
    mismatches: list[tuple[str, int, str]] = []

    # Shape 1: bursts against a free-running CPU.
    for iteration in range(40):
        gdb.read_registers()
        gdb.read_memory(0xB8000, 160)
        gdb.read_memory(0x400, 64)
        gdb.read_memory(0x0, 32)
        gdb.read_register(8)
        gdb.read_memory(0xF0000, 128)
        probe = gdb.read_memory(BIOS_RESET_VECTOR, len(BIOS_RESET_BYTES))
        if probe != BIOS_RESET_BYTES:
            mismatches.append(("running", iteration, probe.hex()))

    # Shape 2: the probe's own loop -- stop on a breakpoint, then read.
    seg, off = int08_handler(gdb)
    gdb.halt()
    assert gdb.set_breakpoint(linear(seg, off))
    for iteration in range(20):
        reply = gdb.continue_execution()
        assert reply == b"S05", f"unexpected stop reply {reply!r}"
        registers = gdb.read_registers()
        ss, bp = registers["ss"] & 0xFFFF, registers["ebp"] & 0xFFFF
        gdb.read_memory(linear(ss, (bp - 0xC) & 0xFFFF), 2)
        gdb.read_memory(linear(ss, bp), 6)
        gdb.read_register(8)
        gdb.read_memory(0xB8000, 320)
        assert gdb.set_breakpoint(BIOS_RESET_VECTOR)
        assert gdb.remove_breakpoint(BIOS_RESET_VECTOR)
        probe = gdb.read_memory(BIOS_RESET_VECTOR, len(BIOS_RESET_BYTES))
        if probe != BIOS_RESET_BYTES:
            mismatches.append(("stopped", iteration, probe.hex()))
    assert gdb.remove_breakpoint(linear(seg, off))

    assert not mismatches, f"desynced replies: {mismatches}"


def test_steps_out_returns_past_a_real_16_bit_ret(
    make_session: Callable[..., DosboxSession], tmp_path: Path
) -> None:
    """`steps_out` stops AFTER the `ret`, not on it, in real guest code.

    The `SP > BP + 2` rule exists because `pop bp` alone lands SP exactly on
    `BP + 2`, with the CPU still sitting on the `ret`. The staged .COM has
    exactly that epilogue, so the older `SP > BP` rule would stop inside the
    callee at `0x011A`; only the fixed rule reaches `0x010D`, the
    instruction after the `call`.
    """
    drive = tmp_path / "c"
    drive.mkdir()
    (drive / FRAME_COM_NAME).write_bytes(FRAME_COM)
    session = make_session(
        mounts={"c": drive},
        autoexec=[f"mount c {drive}", "c:", FRAME_COM_NAME],
    )
    gdb, _ = clients(session)

    # The program stamps its own CS into low memory, so the host learns the
    # load segment from the guest instead of guessing at it.
    deadline = time.time() + FRAME_STAMP_TIMEOUT
    cs = 0
    while not cs and time.time() < deadline:
        cs = struct.unpack("<H", gdb.read_memory(FRAME_STAMP_ADDRESS, 2))[0]
        if not cs:
            time.sleep(0.2)
    assert cs, f"{FRAME_COM_NAME} never ran: no CS stamp at {FRAME_STAMP_ADDRESS:#x}"
    # The stamp says only "some word at 0x500 is non-zero". Confirm the
    # segment it names really holds FRAME.COM: without this, anything that
    # ever leaves a stray word there would send the breakpoint below to a
    # garbage address, and the wrong value would surface as a 30s socket
    # timeout instead of as a failed assertion.
    loaded = gdb.read_memory(linear(cs, COM_LOAD_OFFSET), len(FRAME_COM_SIGNATURE))
    assert loaded == FRAME_COM_SIGNATURE, (
        f"the word at {FRAME_STAMP_ADDRESS:#x} is {cs:#06x}, but "
        f"{cs:04X}:{COM_LOAD_OFFSET:04X} does not hold {FRAME_COM_NAME}"
    )

    gdb.halt()
    body = linear(cs, FRAME_BODY_OFFSET)
    assert gdb.set_breakpoint(body)
    assert gdb.continue_execution() == b"S05"
    assert gdb.linear_pc() == body
    # The breakpoint must go before stepping: a breakpoint hit during a
    # single step would push an extra stop reply onto the wire.
    assert gdb.remove_breakpoint(body)

    entry = gdb.read_registers()
    frame_bp = entry["ebp"] & 0xFFFF
    assert (entry["esp"] & 0xFFFF) == frame_bp - 4, "the prologue's locals are not on the stack"
    walked = walk_frames(gdb, max_depth=1)
    assert walked and walked[0].bp == frame_bp
    assert walked[0].return_off == FRAME_AFTER_CALL_OFFSET

    steps_out(gdb, timeout=20.0)

    after = gdb.read_registers()
    assert gdb.linear_pc() == linear(cs, FRAME_AFTER_CALL_OFFSET)
    assert (after["esp"] & 0xFFFF) == frame_bp + 4, "SP did not clear the return-address slot"


def test_unsolicited_stop_reply_from_break_on_exec_is_queued_not_read_as_a_reply(
    make_session: Callable[..., DosboxSession], tmp_path: Path
) -> None:
    """An unrequested `S05` is queued, and the next read still gets its own bytes.

    `DEBUG_CheckExecuteBreakpoint` arms AND immediately activates a
    breakpoint at the next program's entry point, regardless of whether the
    GDB client asked to continue. When it hits, the stub sends a `$S05#b8`
    that nothing requested, and it lands exactly where the client's next
    packet expects its `+` ACK.

    THIS ASSERTED A DEFECT AND NOW ASSERTS THE CONTRACT. It used to require
    `ConnectionError: Failed to receive ACK. Got: b'$'` -- after which every
    later call chewed one more byte off the stream -- because that was what
    the client did. What it requires now: the stop reply is diverted to
    `pending_stops`, the `m` that follows is answered with the ROM bytes it
    actually asked for, the connection stays usable for the read after that,
    and `take_pending_stops` hands the stop to the caller exactly once.
    """
    drive = tmp_path / "c"
    drive.mkdir()
    (drive / EXIT_COM_NAME).write_bytes(EXIT_COM)
    (drive / EXEC_LOOP_NAME).write_text(EXEC_LOOP_BAT, newline="")
    session = make_session(
        mounts={"c": drive},
        autoexec=[f"mount c {drive}", "c:", EXEC_LOOP_NAME],
    )
    gdb, qmp = clients(session)
    assert qmp.query_status()["running"] is True
    assert gdb.read_memory(BIOS_RESET_VECTOR, len(BIOS_RESET_BYTES)) == BIOS_RESET_BYTES

    qmp.debug_break_on_exec(True)

    # Wait for the break to actually fire before touching GDB at all, so the
    # unsolicited reply is already buffered when the client next speaks --
    # rather than racing it mid-exchange, where it lands where a PAYLOAD was
    # expected instead of where an ACK was. QMP is a separate connection, so
    # polling it does not disturb the GDB stream.
    deadline = time.time() + BREAK_ON_EXEC_TIMEOUT
    while time.time() < deadline and qmp.query_status()["running"]:
        time.sleep(0.2)
    assert qmp.query_status()["running"] is False, (
        f"break-on-exec never stopped the CPU within {BREAK_ON_EXEC_TIMEOUT}s; "
        f"the guest's {EXEC_LOOP_NAME} loop may not be running"
    )

    assert gdb.read_memory(BIOS_RESET_VECTOR, len(BIOS_RESET_BYTES)) == BIOS_RESET_BYTES
    assert gdb.read_memory(BDA_ADDRESS, BDA_LENGTH) != BIOS_RESET_BYTES
    assert gdb.read_memory(BIOS_RESET_VECTOR, len(BIOS_RESET_BYTES)) == BIOS_RESET_BYTES

    # The stop really happened, so it must reach the caller rather than being
    # silently dropped -- and exactly once.
    stops = gdb.take_pending_stops()
    assert stops, "the unsolicited stop reply was swallowed instead of queued"
    assert all(stop.startswith(b"S") or stop.startswith(b"T") for stop in stops), stops
    assert gdb.take_pending_stops() == []


def test_a_timed_out_request_does_not_shift_every_later_reply(
    make_session: Callable[..., DosboxSession],
) -> None:
    """A request that times out must not leave every later reply one packet late.

    This is the second, quieter way to reproduce the symptom `frames.py`
    records. The socket timeout is squeezed below any possible round-trip,
    so the reply to the 16-byte ROM read is abandoned unread. The client
    used to send its next packet, consume the ABANDONED request's ACK, and
    read the ABANDONED request's payload: 16 bytes of ROM where 8 bytes of
    BIOS data area were asked for, with nothing putting the stream back and
    nothing reporting it.

    It now drains exactly what the abandoned exchange still owes before
    sending anything else, so the next request is answered with its own
    bytes. This test was `xfail(strict=True)` for as long as that was not
    true; the marker is gone because it genuinely passes.
    """
    gdb, _ = clients(make_session(boot_settle=NO_BOOT_SETTLE))
    assert gdb.sock is not None
    original_timeout = gdb.sock.gettimeout()

    baseline = gdb.read_memory(BDA_ADDRESS, BDA_LENGTH)
    assert len(baseline) == BDA_LENGTH
    assert gdb.read_memory(BIOS_RESET_VECTOR, len(BIOS_RESET_BYTES)) == BIOS_RESET_BYTES

    gdb.sock.settimeout(IMPOSSIBLE_SOCKET_TIMEOUT)
    with pytest.raises(TimeoutError):
        gdb.read_memory(BIOS_RESET_VECTOR, len(BIOS_RESET_BYTES))
    gdb.sock.settimeout(original_timeout)

    assert gdb.read_memory(BDA_ADDRESS, BDA_LENGTH) == baseline
    # Not just the first read after the timeout: the stream is back in step
    # for good, and both lengths still come back where they belong.
    assert gdb.read_memory(BIOS_RESET_VECTOR, len(BIOS_RESET_BYTES)) == BIOS_RESET_BYTES
    assert gdb.read_memory(BDA_ADDRESS, BDA_LENGTH) == baseline


def test_the_default_timeout_bounds_a_request_the_stub_will_not_answer(
    make_session: Callable[..., DosboxSession],
) -> None:
    """`qmp.stop()` then a GDB request fails with a diagnostic, not a deadlock.

    While the emulator is QMP-stopped the GDB stub is not serviced at all --
    it is polled from the emulation thread -- so this exact pair used to hang
    the caller forever with nothing to read. The client now carries a read
    timeout of its own, so it raises and names the packet that went
    unanswered. A short explicit timeout keeps the test quick; the point
    being proven is that the bound exists and is honoured, not its default.
    """
    session = make_session(boot_settle=NO_BOOT_SETTLE)
    gdb, qmp = clients(session)
    assert gdb.sock is not None
    assert gdb.read_memory(BIOS_RESET_VECTOR, len(BIOS_RESET_BYTES)) == BIOS_RESET_BYTES

    qmp.stop()
    gdb.sock.settimeout(UNANSWERED_REQUEST_TIMEOUT)
    try:
        with pytest.raises(TimeoutError) as caught:
            gdb.read_memory(BIOS_RESET_VECTOR, len(BIOS_RESET_BYTES))
    finally:
        qmp.cont()

    assert "mffff0" in str(caught.value), caught.value
