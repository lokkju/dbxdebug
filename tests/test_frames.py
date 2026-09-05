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
    """A frame whose saved BP points at itself must not loop forever."""
    memory = {linear(SS, 0x1000): frame_bytes(0x1000, 0xAAAA, 0x1111)}
    gdb = FakeGDB({"ebp": 0x1000, "ss": SS}, memory)

    assert walk_frames(gdb) == [Frame(bp=0x1000, return_off=0xAAAA, return_seg=0x1111, depth=0)]


def test_walk_frames_terminates_on_an_a_to_b_to_a_cycle():
    """A two-frame cycle must terminate with the frames seen before the loop closes."""
    memory = {
        linear(SS, 0x1000): frame_bytes(0x1200, 0xAAAA, 0x1111),
        linear(SS, 0x1200): frame_bytes(0x1000, 0xBBBB, 0x2222),
    }
    gdb = FakeGDB({"ebp": 0x1000, "ss": SS}, memory)

    assert walk_frames(gdb) == [
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


def test_walk_frames_returns_nothing_for_a_zero_bp():
    gdb = FakeGDB({"ebp": 0x0000, "ss": SS}, {})

    assert walk_frames(gdb) == []


def test_steps_out_returns_the_stop_reply_once_sp_rises_past_the_saved_bp_slot():
    gdb = FakeGDB(
        {"ebp": 0x1000, "esp": 0x0FF0, "ss": SS},
        {},
        step_script=[{"esp": 0x0FF4}, {"esp": 0x1000}, {"esp": 0x1004}],
    )

    reply = steps_out(gdb)

    assert reply == b"S05"
    assert gdb.steps == 3


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
