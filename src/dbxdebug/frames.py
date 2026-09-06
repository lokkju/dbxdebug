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
packed `(seg << 16) | off` far pointer. The probe script this module's
domain knowledge came from -- an earlier consumer of this protocol, in a
downstream project, written against an older stub -- predates that and
argues at length about whether a linear `cs*16 + off` "also fires" for
`Z0`. That argument is dead here: no breakpoints, no packed pointers.

The `word()` retry, deliberately not reproduced. That earlier probe carries
a helper that reads the same two bytes up to four times "until two
consecutive reads agree", with the comment that "a single `m` right after a
run of packets has been seen to come back with the previous response's
payload". That is a real observation someone made against a live stub, and
it is recorded here so it is not lost. We do not replicate the workaround:
it doubles the round-trips on every single word and, worse, it would
silently paper over a genuine protocol desync instead of surfacing it --
note that two identical consecutive requests mask a one-packet lag
perfectly, so "until two consecutive reads agree" would have looked like it
worked whether or not the stream had shifted. Both ways of producing that
shift -- an unsolicited stop reply, and a timed-out request whose abandoned
reply is read as the next answer -- were reproduced live and then fixed in
`GDBClient` itself, which is the right layer: it bounds every read, drains
exactly what an abandoned exchange still owes before sending anything else,
diverts stop replies nobody asked for into `GDBClient.pending_stops`, and
marks itself unusable rather than answering when a drain cannot complete.
Live tests for both are in `tests/integration/test_live_session.py`, framing
tests in `tests/test_gdb_framing.py`. Nothing here changed as a result:
`walk_frames` still reads each frame exactly once via
`GDBClient.read_memory`. A stale-payload read is a bug in the client or the
stub and must be fixed there, not compensated for here.
"""

import struct
import time
from dataclasses import dataclass
from typing import Protocol

from . import addressing

__all__ = [
    "FRAME_RECORD_SIZE",
    "Frame",
    "FrameWalkError",
    "GDBLike",
    "steps_out",
    "walk_frames",
]

# Words read from the frame record at [BP]: saved BP, return offset,
# return segment.
FRAME_RECORD_SIZE = 6


class FrameWalkError(RuntimeError):
    """Raised when a frame operation cannot complete within its bounds."""


class GDBLike(Protocol):
    """The slice of `gdb.GDBClient` this module needs.

    Declared structurally so callers (and tests) can pass anything with
    these three methods. Note what that does and does not buy: the
    `Protocol` earns its keep for testability and type-checking, NOT for
    import isolation. This module does not import `gdb` itself, but
    `dbxdebug/__init__.py` does (`from .gdb import GDBClient`), so
    `import dbxdebug.frames` pulls the socket layer in anyway.
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
    return_seg: int
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
    * a memory or register read raises, or comes back short;
    * `max_depth` frames have been collected.

    Every one of those inputs is guest-controlled memory, which is why the
    loop is bounded several different ways. A cyclic BP chain terminates on
    the second rule -- see the comment on the loop for why that subsumes
    cycle detection entirely, and why no separate `seen` set is kept.

    Known limitation, documented rather than fixed: a frame at `BP > 0xFFFA`
    has its six-byte record straddle the end of SS, where a real CPU wraps
    the tail of the read around to `SS:0000`. This reads the linear bytes
    that follow the segment instead.

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
        ss = registers["ss"] & 0xFFFF
        bp = registers["ebp"] & 0xFFFF
    except Exception:
        return frames

    # No cycle guard here, and none is needed. `bp` is only ever reassigned
    # after the `saved_bp <= bp` break below, so every accepted frame has a
    # strictly greater BP than the one before it: the sequence of visited BPs
    # is strictly increasing and therefore cannot repeat a value. A cyclic BP
    # chain in guest memory closes back onto a BP that is not above the
    # current one, so it ends at the monotonicity rule instead of looping --
    # which is the whole of what a `seen` set would have done. That strictness
    # is load-bearing: weakening the comparison to `saved_bp < bp` would admit
    # `saved_bp == bp` and reintroduce the possibility of a cycle.
    for depth in range(max_depth):
        if bp == 0:
            break
        try:
            record = gdb.read_memory(addressing.linear(ss, bp), FRAME_RECORD_SIZE)
            # A short reply is an expected end of the walk, not a failure. The
            # `struct.unpack` below is inside this `try` as well, so a truncated
            # record cannot escape even without this guard -- the guard states
            # the intent, and keeps the unpack provably total.
            if len(record) < FRAME_RECORD_SIZE:
                break
            saved_bp, return_off, return_seg = struct.unpack("<HHH", record[:FRAME_RECORD_SIZE])
        except Exception:
            break
        frames.append(Frame(bp=bp, return_off=return_off, return_seg=return_seg, depth=depth))
        if saved_bp == 0 or saved_bp <= bp:
            break
        bp = saved_bp

    return frames


def steps_out(gdb: GDBLike, timeout: float = 10.0, max_steps: int = 100_000) -> bytes:
    """Single-step until the current frame has returned.

    The rule implemented, exactly: entry `BP` is recorded, then the target
    is single-stepped until `SP & 0xFFFF` is strictly greater than
    `BP + 2`. `[BP]` holds the saved BP and `[BP+2]` holds the return
    address, so SP reaching `BP+2` means the epilogue has popped BP and
    nothing more -- the CPU is still inside the callee, sitting on its
    `ret`. Only the return itself carries SP past the return-address slot:
    a near `ret` to `BP+4`, a far `retf` to `BP+6`, a `ret N` to `BP+4+N`.
    Each of those is strictly greater than `BP+2`; the bare `pop bp` that
    precedes them is not. `leave` is `mov sp,bp / pop bp` in a single
    instruction and so also lands exactly on `BP+2`, which is deliberately
    not enough to stop.

    Entry condition. `BP` must actually describe the current frame, so
    `SP > BP` on entry is rejected outright rather than satisfied on the
    first step: `BP == 0` (a routine with no frame pointer, a fresh
    real-mode entry, hand-written asm) or any stale `BP` left below the
    live `SP` would otherwise pass the comparison almost immediately and
    return having stepped one instruction and left nothing. `SP == BP` is
    legal and accepted -- that is a frame with no locals.

    Known bound of the heuristic, NOT fixed: a callee that pops BP and then
    jumps to a shared epilogue which pops further registers before its own
    `ret` raises SP past `BP+2` while still inside the callee, and this
    stops there, one or more instructions early. Distinguishing that from a
    real return needs instruction decoding or a breakpoint on the return
    address; this module does neither.

    Active breakpoints are a hazard, and this function does not guard
    against them: it removes none and expects none to be armed. `GDBClient`
    assumes strict request/response, so a breakpoint hit during one of these
    single steps makes the stub emit an extra, UNSOLICITED stop reply --
    which the client then reads where it expects the ACK or the reply to its
    next request, desyncing the connection permanently and silently
    (lokkju/dbxdebug#4). Clear every breakpoint before calling this.

    Two more consequences worth knowing. The check runs AFTER each step, so
    this always executes at least one instruction. And the rule assumes the
    prologue that established `BP` has already run -- called at a
    procedure's very first instruction, `BP` still belongs to the caller
    and sits ABOVE `SP`, so the entry check passes and the walk measures
    the caller's frame instead. SP is compared as a 16-bit value against an
    unmasked `BP+2`, so a frame at `BP >= 0xFFFD` (threshold at or above
    `0xFFFF`, which no 16-bit SP can exceed) can never satisfy the
    comparison and raises rather than returning a wrong answer; a stack
    that wraps the segment boundary mid-epilogue is not handled.

    Args:
        gdb: Anything satisfying `GDBLike`.
        timeout: Wall-clock ceiling, in seconds, on the whole operation.
            This is the bound that fires in practice: every iteration costs
            two GDB round-trips (`s` then `g`), so the clock runs out long
            before `max_steps` does.
        max_steps: Ceiling on the number of instructions stepped. A
            backstop for the case `timeout` cannot cover -- a caller
            passing `math.inf` or an effectively unbounded timeout -- which
            is why it is left large rather than tuned to what 10 seconds of
            round-trips can reach.

    Returns:
        The stop reply from the final `step`, as `bytes` -- matching
        `GDBClient.step`.

    Raises:
        FrameWalkError: If `SP > BP` on entry, or if the frame has not
            returned before either bound is reached. This never returns
            silently without having stepped out.
        Exception: Whatever `gdb.read_registers` or `gdb.step` raises is
            deliberately allowed to propagate unchanged -- unlike
            `walk_frames`, this function wraps no calls in `try`, because a
            transport failure mid-step-out leaves the target in an unknown
            place and must not be reported as a completed step-out.
    """
    registers = gdb.read_registers()
    frame_bp = registers["ebp"] & 0xFFFF
    entry_sp = registers["esp"] & 0xFFFF
    if entry_sp > frame_bp:
        raise FrameWalkError(
            f"SP={entry_sp:#06x} is above BP={frame_bp:#06x}: BP does not describe "
            "the current frame (no frame pointer established, or a stale BP). "
            "SP == BP is legal -- a frame with no locals -- but SP above BP is not"
        )

    # The frame is gone only once SP is strictly past the return-address slot
    # at [BP+2]. Stopping at BP+2 itself would stop on the bare `pop bp` (or
    # `leave`), with the callee's `ret` not yet executed.
    returned_sp = frame_bp + 2
    deadline = time.monotonic() + timeout

    for stepped in range(1, max_steps + 1):
        reply = gdb.step()
        if (gdb.read_registers()["esp"] & 0xFFFF) > returned_sp:
            return reply
        if time.monotonic() >= deadline:
            raise FrameWalkError(
                f"frame at BP={frame_bp:#06x} did not return within the "
                f"{timeout}s timeout ({stepped} instructions stepped)"
            )

    raise FrameWalkError(f"frame at BP={frame_bp:#06x} did not return within max_steps={max_steps}")
