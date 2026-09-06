"""Real-mode stack frame walking, tested against a fake GDB client.

Frame walking is pure arithmetic over memory reads, so these tests supply a
fake that implements only the three methods `frames.GDBLike` requires. The
fake's method signatures match `dbxdebug.gdb.GDBClient` exactly, including
that `read_memory` takes a LINEAR address and that `registers["eip"]` is an
offset within CS rather than a linear address.
"""

import pytest

from dbxdebug.addressing import linear
from dbxdebug.frames import Frame, FrameWalkError, steps_out, walk_frames

# The lowercase register names DOSBox-X's stub reports, in `gdb.REGISTER_NAMES`
# order. Duplicated here on purpose: these tests pin the key names `frames`
# relies on, so they must fail if the client's names drift.
REGISTER_NAMES = [
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
]


class FakeGDB:
    """A minimal stand-in for `GDBClient` covering only what `frames` uses.

    Args:
        registers: Register values to report; missing names default to 0.
        memory: Linear address -> bytes, consulted byte by byte.
        fail_reads_after: Number of successful `read_memory` calls before
            every later call raises `OSError`. `None` means never fail.
        step_script: Register dicts applied in turn by `step`; the last one
            repeats forever once exhausted.
    """

    def __init__(
        self,
        registers: dict[str, int] | None = None,
        memory: dict[int, bytes] | None = None,
        fail_reads_after: int | None = None,
        step_script: list[dict[str, int]] | None = None,
    ) -> None:
        self._registers = dict(registers or {})
        self._memory: dict[int, int] = {}
        for base, blob in (memory or {}).items():
            for i, byte in enumerate(blob):
                self._memory[base + i] = byte
        self._fail_reads_after = fail_reads_after
        self._reads = 0
        self._step_script = list(step_script or [])
        self.steps = 0

    def read_registers(self) -> dict[str, int]:
        return {name: self._registers.get(name, 0) for name in REGISTER_NAMES}

    def read_memory(self, address: int | str, length: int) -> bytes:
        if isinstance(address, str):
            raise AssertionError("frames must pass linear int addresses")
        if self._fail_reads_after is not None and self._reads >= self._fail_reads_after:
            raise OSError("simulated read failure")
        self._reads += 1
        return bytes(self._memory.get(address + i, 0) for i in range(length))

    def step(self) -> bytes:
        self.steps += 1
        if self._step_script:
            self._registers.update(self._step_script.pop(0))
        return b"S05"


def frame_bytes(saved_bp: int, return_off: int, return_seg: int) -> bytes:
    """Encode the three words a real-mode frame exposes at `[BP]`.

    Args:
        saved_bp: Caller's BP, stored at `[BP]`.
        return_off: Return offset, stored at `[BP+2]`.
        return_seg: Return segment (far calls only), stored at `[BP+4]`.

    Returns:
        The six little-endian bytes.
    """
    return (
        saved_bp.to_bytes(2, "little")
        + return_off.to_bytes(2, "little")
        + return_seg.to_bytes(2, "little")
    )


SS = 0x0824


def test_walk_frames_returns_the_whole_chain_up_to_the_zero_terminator():
    memory = {
        linear(SS, 0x1000): frame_bytes(0x1200, 0xAAAA, 0x1111),
        linear(SS, 0x1200): frame_bytes(0x1400, 0xBBBB, 0x2222),
        linear(SS, 0x1400): frame_bytes(0x0000, 0xCCCC, 0x3333),
    }
    gdb = FakeGDB({"ebp": 0x1000, "ss": SS}, memory)

    assert walk_frames(gdb) == [
        Frame(bp=0x1000, return_off=0xAAAA, return_seg=0x1111, depth=0),
        Frame(bp=0x1200, return_off=0xBBBB, return_seg=0x2222, depth=1),
        Frame(bp=0x1400, return_off=0xCCCC, return_seg=0x3333, depth=2),
    ]


def test_walk_frames_masks_registers_to_sixteen_bits():
    """Real-mode BP and SS are the low words of the 32-bit values the stub reports."""
    memory = {linear(SS, 0x1000): frame_bytes(0x0000, 0xAAAA, 0x1111)}
    gdb = FakeGDB({"ebp": 0xDEAD1000, "ss": 0xBEEF0000 | SS}, memory)

    assert walk_frames(gdb) == [Frame(bp=0x1000, return_off=0xAAAA, return_seg=0x1111, depth=0)]


def test_walk_frames_terminates_on_a_self_referential_bp():
    """A one-frame cycle ends at the strict-monotonicity rule, not a cycle guard.

    `walk_frames` keeps no set of visited BPs. A saved BP pointing back at
    the frame that holds it is not strictly greater than the current BP, so
    `saved_bp <= bp` ends the walk with the single frame already collected.
    """
    memory = {linear(SS, 0x1000): frame_bytes(0x1000, 0xAAAA, 0x1111)}
    gdb = FakeGDB({"ebp": 0x1000, "ss": SS}, memory)

    walked = walk_frames(gdb)

    assert len(walked) == 1
    assert walked == [Frame(bp=0x1000, return_off=0xAAAA, return_seg=0x1111, depth=0)]


def test_walk_frames_terminates_on_an_a_to_b_to_a_cycle():
    """A two-frame cycle ends at the strict-monotonicity rule, not a cycle guard.

    A->B->A is a genuine cycle in guest memory, and the requirement that it
    terminate is real. Nothing detects the repeat as such: B's saved BP is
    below B, so `saved_bp <= bp` stops the walk after the two frames
    collected on the way in.
    """
    memory = {
        linear(SS, 0x1000): frame_bytes(0x1200, 0xAAAA, 0x1111),
        linear(SS, 0x1200): frame_bytes(0x1000, 0xBBBB, 0x2222),
    }
    gdb = FakeGDB({"ebp": 0x1000, "ss": SS}, memory)

    walked = walk_frames(gdb)

    assert len(walked) == 2
    assert walked == [
        Frame(bp=0x1000, return_off=0xAAAA, return_seg=0x1111, depth=0),
        Frame(bp=0x1200, return_off=0xBBBB, return_seg=0x2222, depth=1),
    ]


def test_walk_frames_stops_on_a_read_failure_and_keeps_what_it_has():
    memory = {
        linear(SS, 0x1000): frame_bytes(0x1200, 0xAAAA, 0x1111),
        linear(SS, 0x1200): frame_bytes(0x1400, 0xBBBB, 0x2222),
    }
    gdb = FakeGDB({"ebp": 0x1000, "ss": SS}, memory, fail_reads_after=1)

    assert walk_frames(gdb) == [Frame(bp=0x1000, return_off=0xAAAA, return_seg=0x1111, depth=0)]


def test_walk_frames_returns_nothing_when_the_registers_cannot_be_read():
    class Broken(FakeGDB):
        def read_registers(self) -> dict[str, int]:
            raise OSError("simulated register failure")

    assert walk_frames(Broken()) == []


def test_walk_frames_honours_max_depth():
    memory = {
        linear(SS, 0x1000 + 0x100 * i): frame_bytes(0x1000 + 0x100 * (i + 1), i, SS)
        for i in range(20)
    }
    gdb = FakeGDB({"ebp": 0x1000, "ss": SS}, memory)

    walked = walk_frames(gdb, max_depth=4)

    assert len(walked) == 4
    assert [f.depth for f in walked] == [0, 1, 2, 3]
    assert [f.bp for f in walked] == [0x1000, 0x1100, 0x1200, 0x1300]


def test_walk_frames_stops_on_a_decreasing_bp():
    """Stacks grow downward, so a caller's BP is always above its callee's."""
    memory = {
        linear(SS, 0x1200): frame_bytes(0x1000, 0xAAAA, 0x1111),
        linear(SS, 0x1000): frame_bytes(0x0000, 0xBBBB, 0x2222),
    }
    gdb = FakeGDB({"ebp": 0x1200, "ss": SS}, memory)

    assert walk_frames(gdb) == [Frame(bp=0x1200, return_off=0xAAAA, return_seg=0x1111, depth=0)]


def test_walk_frames_returns_nothing_when_a_register_is_missing():
    """A `GDBLike` whose register dict omits `ss`/`ebp` must not raise `KeyError`."""

    class Sparse(FakeGDB):
        def read_registers(self) -> dict[str, int]:
            return {"eax": 0}

    assert walk_frames(Sparse()) == []


def test_walk_frames_stops_on_a_short_memory_read():
    """A truncated `m` reply ends the walk cleanly, keeping what was already read.

    Which mechanism handles it is not observable: the explicit
    `len(record) < FRAME_RECORD_SIZE` guard and the `struct.unpack` that
    follows it now sit inside the same `try`, so a short record ends the
    walk either way. What this pins is the contract -- nothing escapes a
    function documented as never raising, and partial results survive.
    """

    class ShortSecondRead(FakeGDB):
        def read_memory(self, address: int | str, length: int) -> bytes:
            record = super().read_memory(address, length)
            return record if self._reads == 1 else record[:-1]

    memory = {
        linear(SS, 0x1000): frame_bytes(0x1200, 0xAAAA, 0x1111),
        linear(SS, 0x1200): frame_bytes(0x1400, 0xBBBB, 0x2222),
    }
    gdb = ShortSecondRead({"ebp": 0x1000, "ss": SS}, memory)

    assert walk_frames(gdb) == [Frame(bp=0x1000, return_off=0xAAAA, return_seg=0x1111, depth=0)]


def test_walk_frames_returns_nothing_for_a_zero_bp():
    gdb = FakeGDB({"ebp": 0x0000, "ss": SS}, {})

    assert walk_frames(gdb) == []


# The frame every `steps_out` test below steps out of: BP=0x1000, called
# NEAR from CS=0x1234 with the instruction after the `call` at 0x010D, and
# an argument word left at [BP+4] where a far call would have put its
# return segment. The entry CS matters now -- `steps_out` requires CS:IP to
# reach the recorded return address, not just SP to clear the return slot.
FRAME_BP = 0x1000
CALLER_CS = 0x1234
RETURN_OFF = 0x010D
NEAR_FRAME_MEMORY = {linear(SS, FRAME_BP): frame_bytes(0x1200, RETURN_OFF, 0x9999)}
INSIDE_CALLEE = {"ebp": FRAME_BP, "esp": 0x0FF0, "eip": 0x0117, "cs": CALLER_CS, "ss": SS}


def test_steps_out_waits_for_the_near_ret_not_the_pop_bp():
    """`mov sp,bp / pop bp / ret`: `pop bp` lands on BP+2 and must not stop the walk.

    SP goes BP-16 -> BP -> BP+2 -> BP+4. Stopping at BP+2 would return with
    the CPU still on the callee's `ret`.
    """
    gdb = FakeGDB(
        INSIDE_CALLEE,
        NEAR_FRAME_MEMORY,
        step_script=[
            {"esp": 0x1000, "eip": 0x0119},  # mov sp,bp
            {"esp": 0x1002, "eip": 0x011A},  # pop bp
            {"esp": 0x1004, "eip": RETURN_OFF},  # ret
        ],
    )

    reply = steps_out(gdb)

    assert reply == b"S05"
    assert gdb.steps == 3


def test_steps_out_waits_for_the_far_retf():
    """A far return pops offset and segment, leaving SP at BP+6 and CS at [BP+4]."""
    gdb = FakeGDB(
        INSIDE_CALLEE,
        NEAR_FRAME_MEMORY,
        step_script=[
            {"esp": 0x1000, "eip": 0x0119},  # mov sp,bp
            {"esp": 0x1002, "eip": 0x011A},  # pop bp
            {"esp": 0x1006, "eip": RETURN_OFF, "cs": 0x9999},  # retf
        ],
    )

    reply = steps_out(gdb)

    assert reply == b"S05"
    assert gdb.steps == 3


def test_steps_out_waits_for_a_ret_n():
    """`ret N` also discards N bytes of arguments, leaving SP at BP+4+N."""
    gdb = FakeGDB(
        INSIDE_CALLEE,
        NEAR_FRAME_MEMORY,
        step_script=[
            {"esp": 0x1000, "eip": 0x0119},  # mov sp,bp
            {"esp": 0x1002, "eip": 0x011A},  # pop bp
            {"esp": 0x1008, "eip": RETURN_OFF},  # ret 4
        ],
    )

    reply = steps_out(gdb)

    assert reply == b"S05"
    assert gdb.steps == 3


def test_steps_out_waits_for_the_ret_after_a_leave():
    """`leave` is `mov sp,bp / pop bp` in one instruction, so it lands on BP+2."""
    gdb = FakeGDB(
        INSIDE_CALLEE,
        NEAR_FRAME_MEMORY,
        step_script=[
            {"esp": 0x1002, "eip": 0x011A},  # leave
            {"esp": 0x1004, "eip": RETURN_OFF},  # ret
        ],
    )

    reply = steps_out(gdb)

    assert reply == b"S05"
    assert gdb.steps == 2


def test_steps_out_does_not_stop_where_the_callee_merely_reaches_the_return_offset():
    """CS:IP at the return address with the frame still live is not a return.

    A callee whose own code passes through the caller's return offset --
    the same segment, an offset it happens to branch to -- has not
    returned, and SP still sitting inside the frame is what says so.
    """
    gdb = FakeGDB(
        INSIDE_CALLEE,
        NEAR_FRAME_MEMORY,
        step_script=[
            {"esp": 0x0FF0, "eip": RETURN_OFF},  # branched there, frame intact
            {"esp": 0x1002, "eip": 0x011A},  # leave
            {"esp": 0x1004, "eip": RETURN_OFF},  # ret -- this is the return
        ],
    )

    reply = steps_out(gdb)

    assert reply == b"S05"
    assert gdb.steps == 3


def test_steps_out_rejects_a_bp_that_does_not_describe_the_current_frame():
    """BP=0 -- no frame pointer, fresh real-mode entry, hand-written asm."""
    gdb = FakeGDB({"ebp": 0x0000, "esp": 0x0FFE, "ss": SS})

    with pytest.raises(FrameWalkError, match="does not describe the current frame"):
        steps_out(gdb)

    assert gdb.steps == 0


def test_steps_out_rejects_a_stale_bp_below_sp():
    """A BP left over from an already-returned frame sits below the live SP."""
    gdb = FakeGDB({"ebp": 0x1000, "esp": 0x1200, "ss": SS})

    with pytest.raises(FrameWalkError, match="does not describe the current frame"):
        steps_out(gdb)

    assert gdb.steps == 0


def test_steps_out_accepts_a_frame_with_no_locals():
    """`SP == BP` is legal: `push bp / mov bp,sp` with nothing pushed after it."""
    gdb = FakeGDB(
        {**INSIDE_CALLEE, "esp": FRAME_BP},
        NEAR_FRAME_MEMORY,
        step_script=[
            {"esp": 0x1002, "eip": 0x011A},  # pop bp
            {"esp": 0x1004, "eip": RETURN_OFF},  # ret
        ],
    )

    reply = steps_out(gdb)

    assert reply == b"S05"
    assert gdb.steps == 2


def test_steps_out_gives_up_after_max_steps():
    gdb = FakeGDB({"ebp": 0x1000, "esp": 0x0FF0, "ss": SS}, {})

    with pytest.raises(FrameWalkError, match="max_steps"):
        steps_out(gdb, max_steps=25)

    assert gdb.steps == 25


def test_steps_out_gives_up_when_the_timeout_expires():
    gdb = FakeGDB({"ebp": 0x1000, "esp": 0x0FF0, "ss": SS}, {})

    with pytest.raises(FrameWalkError, match="timeout"):
        steps_out(gdb, timeout=0.0)

    assert gdb.steps == 1


def test_walk_frames_wraps_a_frame_record_around_the_end_of_the_segment():
    """A record straddling SS:FFFF continues at SS:0000, as a real CPU addresses it."""
    record = frame_bytes(0x0000, 0xAAAA, 0x1111)
    memory = {
        linear(SS, 0xFFFE): record[:2],
        linear(SS, 0x0000): record[2:],
        # What a non-wrapping read would pick up instead: the bytes that
        # simply follow the segment, which are NOT part of the frame.
        linear(SS, 0xFFFE) + 2: b"\xde\xad\xbe\xef",
    }
    gdb = FakeGDB({"ebp": 0xFFFE, "ss": SS}, memory)

    assert walk_frames(gdb) == [Frame(bp=0xFFFE, return_off=0xAAAA, return_seg=0x1111, depth=0)]


def test_steps_out_keeps_stepping_through_a_shared_epilogue_that_pops_past_the_slot():
    """SP past `BP+2` inside the callee is not a return; only the return address is.

    The shape: `pop bp` lands on `BP+2`, then the callee pops the return
    address into a register, discards its arguments, and jumps to it --
    `pop ax / add sp,6 / jmp ax`. SP crosses `BP+2` two instructions before
    control actually leaves the frame, and the old `SP > BP+2` rule stopped
    there, still inside the callee, reporting a return that had not
    happened.
    """
    memory = {linear(SS, 0x1000): frame_bytes(0x1200, 0x010D, 0x9999)}
    gdb = FakeGDB(
        {"ebp": 0x1000, "esp": 0x0FF0, "eip": 0x0117, "cs": 0x1234, "ss": SS},
        memory,
        step_script=[
            {"esp": 0x1000, "eip": 0x0119},  # mov sp,bp
            {"esp": 0x1002, "eip": 0x011A},  # pop bp      -> SP == BP+2
            {"esp": 0x1004, "eip": 0x011B},  # pop ax      -> SP past BP+2, still inside
            {"esp": 0x100A, "eip": 0x011E},  # add sp,6
            {"esp": 0x100A, "eip": 0x010D},  # jmp ax      -> now at the return address
        ],
    )

    reply = steps_out(gdb)

    assert reply == b"S05"
    assert gdb.steps == 5


def test_steps_out_raises_when_the_return_address_is_never_reached():
    """A frame whose recorded return address never arrives fails loudly.

    The old rule returned a plausible-looking stop reply the moment SP rose
    past `BP+2`. There is no correct answer to give here, so the only
    honest outcome is an error that names what was expected and what was
    seen.
    """
    memory = {linear(SS, 0x1000): frame_bytes(0x1200, 0x010D, 0x9999)}
    gdb = FakeGDB(
        {"ebp": 0x1000, "esp": 0x0FF0, "eip": 0x0117, "cs": 0x1234, "ss": SS},
        memory,
        step_script=[{"esp": 0x1010, "eip": 0x0200}],
    )

    with pytest.raises(FrameWalkError, match=r"1234:010d"):
        steps_out(gdb, max_steps=20)

    assert gdb.steps == 20


def test_steps_out_rejects_a_frame_too_close_to_the_top_of_the_segment():
    """`BP >= 0xFFFC` puts every return SP out of a 16-bit register's reach."""
    gdb = FakeGDB({"ebp": 0xFFFC, "esp": 0xFFF0, "ss": SS})

    with pytest.raises(FrameWalkError, match="top of the stack segment"):
        steps_out(gdb)

    assert gdb.steps == 0
