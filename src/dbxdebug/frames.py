"""Walking the real-mode BP chain, and stepping out of the current frame.

A 16-bit procedure that opens with the standard `push bp / mov bp,sp`
prologue leaves a three-word record reachable from BP, all of it read
through SS:

    [BP+0]  the caller's saved BP -- the next link in the chain
    [BP+2]  the return offset
    [BP+4]  the return segment, but ONLY if the call was FAR

Walking that chain is pure memory reading; stepping out of a frame is pure
single-stepping. Neither needs a breakpoint, and this module sets none.

Addresses. Every read here goes through `addressing.linear(ss, off)`. This
package computes linear addresses and the hardened DOSBox-X stub takes them
directly; `addressing.bp_addr` deliberately raises rather than encoding a
packed `(seg << 16) | off` far pointer. The probe scripts this module's
domain knowledge came from (`probe_frame_unwind.py` in the
powerbasic-decompile tree) predate that and argue at length about whether a
linear `cs*16 + off` "also fires" for `Z0`. That argument is dead here: no
breakpoints, no packed pointers.

The `word()` retry, deliberately not reproduced. `probe_frame_unwind.py`
carries a helper that reads the same two bytes up to four times "until two
consecutive reads agree", with the comment that "a single `m` right after a
run of packets has been seen to come back with the previous response's
payload". That is a real observation someone made against a live stub, and
it is recorded here so it is not lost. We do not replicate the workaround:
it doubles the round-trips on every single word, and, worse, it would
silently paper over a genuine protocol desync instead of surfacing it. Note
carefully what is and is not being claimed -- the observation predates the
hardening pass on both the stub and `GDBClient` (which now negotiates no-ack
mode), but nobody has demonstrated that the desync was fixed, only that we
decline to work around it blind. So: `walk_frames` reads each frame exactly
once via `GDBClient.read_memory`. If a live integration test ever reproduces
a stale-payload read, that is a bug in the client or the stub and must be
fixed there, not compensated for in this module.
"""

import struct
import time
from dataclasses import dataclass
from typing import Protocol

from . import addressing

# Words read from the frame record at [BP]: saved BP, return offset,
# return segment.
FRAME_RECORD_SIZE = 6


class FrameWalkError(RuntimeError):
    """Raised when a frame operation cannot complete within its bounds."""


class GDBLike(Protocol):
    """The slice of `gdb.GDBClient` this module needs.

    Declared structurally so callers (and tests) can pass anything with
    these three methods, and so this module never imports the socket layer.
    """

    def read_registers(self) -> dict[str, int]:
        """Return register values keyed by the lowercase names in `gdb.REGISTER_NAMES`."""
        ...

    def read_memory(self, address: str | int, length: int) -> bytes:
        """Read `length` bytes from a linear `address`."""
        ...

    def step(self) -> bytes:
        """Single-step one instruction and return the stop reply."""
        ...


@dataclass(frozen=True)
class Frame:
    """One link in the real-mode BP chain.

    Attributes:
        bp: The frame's BP value, an offset within SS.
        return_off: The word at `[BP+2]`, the return offset.
        return_seg: The word at `[BP+4]`. Meaningful ONLY if the call that
            created this frame was FAR. Whether it was is not decidable
            from the frame itself, so this module does not guess: for a
            NEAR call the same word is whatever the caller happened to
            leave there (typically the first pushed argument), and it is
            reported unchanged. The caller must know which calling
            convention its target uses.
        depth: 0 for the innermost frame, incrementing outward.
    """

    bp: int
    return_off: int
    return_seg: int | None
    depth: int


def walk_frames(gdb: GDBLike, max_depth: int = 32) -> list[Frame]:
    """Walk the real-mode BP chain outward from the current frame.

    Starts at the current `BP`, reads the frame record at `SS:BP`, and
    follows the saved BP outward. Every read is a single `read_memory` of
    six bytes at `addressing.linear(ss, bp)`.

    The walk stops -- returning whatever it has already collected, never
    raising -- on ANY of:

    * the saved BP is zero (the conventional chain terminator);
    * the saved BP is less than or equal to the current BP (real-mode
      stacks grow downward, so a caller's frame always sits above its
      callee's; anything else is corrupt or is not a BP chain at all);
    * the BP has already been visited (a cycle -- redundant given the
      monotonicity rule above, but kept as a second guard so that relaxing
      that rule can never turn this into an unbounded loop);
    * a memory or register read raises;
    * `max_depth` frames have been collected.

    Every one of those inputs is guest-controlled memory, which is why the
    loop is bounded several different ways.

    Args:
        gdb: Anything satisfying `GDBLike`.
        max_depth: Maximum number of frames to return.

    Returns:
        The frames from innermost outward. The zero terminator that ends a
        chain is not itself a frame and is not included.
    """
    frames: list[Frame] = []
    try:
        registers = gdb.read_registers()
    except Exception:
        return frames

    ss = registers["ss"] & 0xFFFF
    bp = registers["ebp"] & 0xFFFF
    seen: set[int] = set()

    for depth in range(max_depth):
        if bp == 0 or bp in seen:
            break
        seen.add(bp)
        try:
            record = gdb.read_memory(addressing.linear(ss, bp), FRAME_RECORD_SIZE)
        except Exception:
            break
        if len(record) < FRAME_RECORD_SIZE:
            break
        saved_bp, return_off, return_seg = struct.unpack("<HHH", record[:FRAME_RECORD_SIZE])
        frames.append(Frame(bp=bp, return_off=return_off, return_seg=return_seg, depth=depth))
        if saved_bp == 0 or saved_bp <= bp:
            break
        bp = saved_bp

    return frames


def steps_out(gdb: GDBLike, timeout: float = 10.0, max_steps: int = 100_000) -> bytes:
    """Single-step until the current frame has returned.

    The rule implemented, exactly: entry `BP` is recorded, then the target
    is single-stepped until `SP & 0xFFFF` is strictly greater than that
    entry `BP`. `[BP]` is the saved-BP slot, the lowest word of the frame
    record, so SP rising above it means the epilogue has popped BP and the
    return address (`mov sp,bp / pop bp / ret` leaves SP at `BP+4` for a
    near return, `BP+6` for a far one) -- the frame is gone.

    Two consequences worth knowing. The check runs AFTER each step, so this
    always executes at least one instruction. And the rule assumes the
    prologue that established `BP` has already run: called at a procedure's
    very first instruction, `BP` still belongs to the caller and SP is
    already below it by only the return address, so the walk would measure
    the caller's frame instead. SP is compared as a 16-bit value, so a
    stack that wraps the segment boundary mid-epilogue is not handled.

    Args:
        gdb: Anything satisfying `GDBLike`.
        timeout: Wall-clock ceiling, in seconds, on the whole operation.
        max_steps: Ceiling on the number of instructions stepped.

    Returns:
        The stop reply from the final `step`, as `bytes` -- matching
        `GDBClient.step`.

    Raises:
        FrameWalkError: If the frame has not returned before either bound
            is reached. This never returns silently without having stepped
            out.
    """
    registers = gdb.read_registers()
    frame_bp = registers["ebp"] & 0xFFFF
    deadline = time.monotonic() + timeout

    for stepped in range(1, max_steps + 1):
        reply = gdb.step()
        if (gdb.read_registers()["esp"] & 0xFFFF) > frame_bp:
            return reply
        if time.monotonic() >= deadline:
            raise FrameWalkError(
                f"frame at BP={frame_bp:#06x} did not return within the "
                f"{timeout}s timeout ({stepped} instructions stepped)"
            )

    raise FrameWalkError(f"frame at BP={frame_bp:#06x} did not return within max_steps={max_steps}")
