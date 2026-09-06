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
`walk_frames` still reads each frame with a single `GDBClient.read_memory`
(two only for the rare record that wraps the end of SS, which is a property
of the CPU's addressing, not a retry). A stale-payload read is a bug in the
client or the stub and must be fixed there, not compensated for here.
"""

import struct
import time
from dataclasses import dataclass
from typing import Protocol

from . import addressing

# Words read from the frame record at [BP]: saved BP, return offset,
# return segment.
FRAME_RECORD_SIZE = 6

# Bytes in one real-mode segment. An offset is 16 bits, so `SS:0xFFFF + 1`
# is `SS:0000`, not the linear byte after the segment -- which is what
# `read_frame_record` exists to honour.
SEGMENT_SIZE = 0x10000

# The highest BP whose frame `steps_out` can recognise the return from. A
# near `ret` leaves SP at `BP+4`, so a frame at `BP > 0xFFFB` needs an SP no
# 16-bit register can hold and no amount of stepping would ever satisfy the
# test. Rejected up front rather than stepped at for the whole timeout.
MAX_STEPPABLE_FRAME_BP = 0xFFFB


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


def read_frame_record(gdb: GDBLike, ss: int, bp: int) -> tuple[int, int, int]:
    """Read the three words a real-mode frame exposes at `SS:BP`.

    The read follows the CPU's addressing rather than the linear address
    space: an offset is 16 bits, so a record starting above `0xFFFA` runs
    off the end of the segment and continues at `SS:0000`. This splits such
    a read in two and joins the halves. Every other record -- every record
    a real DOS stack actually produces -- costs exactly one `read_memory`,
    the same as before, because the split is taken only when the record
    genuinely straddles the boundary.

    Args:
        gdb: Anything satisfying `GDBLike`.
        ss: Stack segment.
        bp: Frame pointer, an offset within `ss`.

    Returns:
        `(saved_bp, return_off, return_seg)` -- the words at `[BP]`,
        `[BP+2]` and `[BP+4]`.

    Raises:
        ValueError: If the reads together come back shorter than
            `FRAME_RECORD_SIZE`.
        Exception: Whatever `gdb.read_memory` raises, unchanged.
    """
    head_length = min(FRAME_RECORD_SIZE, SEGMENT_SIZE - bp)
    record = gdb.read_memory(addressing.linear(ss, bp), head_length)
    if len(record) == head_length and head_length < FRAME_RECORD_SIZE:
        record += gdb.read_memory(addressing.linear(ss, 0), FRAME_RECORD_SIZE - head_length)
    if len(record) < FRAME_RECORD_SIZE:
        raise ValueError(
            f"short frame record at {ss:04x}:{bp:04x}: {len(record)} of {FRAME_RECORD_SIZE} bytes"
        )
    saved_bp, return_off, return_seg = struct.unpack("<HHH", record[:FRAME_RECORD_SIZE])
    return saved_bp, return_off, return_seg


def walk_frames(gdb: GDBLike, max_depth: int = 32) -> list[Frame]:
    """Walk the real-mode BP chain outward from the current frame.

    Starts at the current `BP`, reads the frame record at `SS:BP`, and
    follows the saved BP outward. Every read goes through
    `read_frame_record`, which is a single `read_memory` of six bytes at
    `addressing.linear(ss, bp)` except for a record above `BP = 0xFFFA`,
    which wraps to `SS:0000` the way the CPU addresses it and therefore
    costs two.

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
            # A short reply is an expected end of the walk, not a failure:
            # `read_frame_record` raises `ValueError` for one, and this
            # `except` treats it exactly like a transport failure -- stop,
            # keep what is already collected, never raise.
            saved_bp, return_off, return_seg = read_frame_record(gdb, ss, bp)
        except Exception:
            break
        frames.append(Frame(bp=bp, return_off=return_off, return_seg=return_seg, depth=depth))
        if saved_bp == 0 or saved_bp <= bp:
            break
        bp = saved_bp

    return frames


def steps_out(gdb: GDBLike, timeout: float = 10.0, max_steps: int = 250_000) -> bytes:
    """Single-step until the current frame has returned.

    The rule implemented, exactly. Entry `BP` and `CS` are recorded and the
    frame record at `SS:BP` is read once, giving the return address the
    frame itself names: `[BP+2]` is the return offset and `[BP+4]` is the
    return segment (meaningful only if the call was FAR -- see `Frame`).
    The target is then single-stepped until BOTH of these hold:

    * `IP` equals the recorded return offset; and
    * either `CS` is unchanged from entry and `SP >= BP+4` (a NEAR return:
      `ret` to `BP+4`, `ret N` to `BP+4+N`), or `CS` equals the recorded
      return segment and `SP >= BP+6` (a FAR return: `retf` to `BP+6`,
      `retf N` beyond it).

    Both halves are load-bearing. The SP half alone is what this used to
    be, and it stops early on a callee that raises SP past the
    return-address slot without returning -- the textbook shape being a
    variable-argument cleanup, `pop ax / add sp,N / jmp ax`, which crosses
    `BP+2` two instructions before control leaves the frame. The
    return-address half alone would accept a callee that merely branches
    back through the caller's code with its frame still live. Requiring
    both means the CPU has to be AT the caller's return address with the
    frame's return slot already popped. Nothing here decodes instructions:
    every value compared is one the register read at the end of each step
    already carries, so the added precision costs zero extra round-trips.

    Why not a breakpoint. Setting `Z0` at the return address and
    continuing would be far faster for a long frame, and was considered.
    It is rejected because it mutates the guest for the duration, because
    a `c` that hits the breakpoint mid-sequence is exactly the unsolicited
    stop reply this client is documented as fragile around (see below),
    and because a return address that is never reached would hang on `c`
    rather than being bounded by the step budget. A caller who wants that
    trade can set the breakpoint itself; this function stays a stepper.

    Entry conditions. `BP` must actually describe the current frame, so
    `SP > BP` on entry is rejected outright rather than satisfied on the
    first step: `BP == 0` (a routine with no frame pointer, a fresh
    real-mode entry, hand-written asm) or any stale `BP` left below the
    live `SP` would otherwise pass the comparison almost immediately and
    return having stepped one instruction and left nothing. `SP == BP` is
    legal and accepted -- that is a frame with no locals. `BP` above
    `MAX_STEPPABLE_FRAME_BP` is rejected too: a near return from such a
    frame needs an SP that does not fit in 16 bits, so no sequence of
    steps could ever satisfy the test and stepping at it would only run
    the guest for the whole timeout before failing.

    Active breakpoints are a hazard, and this function does not guard
    against them: it removes none and expects none to be armed. `GDBClient`
    assumes strict request/response, so a breakpoint hit during one of these
    single steps makes the stub emit an extra, UNSOLICITED stop reply --
    which the client then reads where it expects the ACK or the reply to its
    next request, desyncing the connection permanently and silently
    (lokkju/dbxdebug#4). Clear every breakpoint before calling this.

    Three more consequences worth knowing. The check runs AFTER each step,
    so this always executes at least one instruction. The rule assumes the
    prologue that established `BP` has already run -- called at a
    procedure's very first instruction, `BP` still belongs to the caller
    and sits ABOVE `SP`, so the entry check passes and the walk measures
    the caller's frame instead. And a guest that rewrites its own return
    slot after this has read it, or a frame at `BP == 0xFFFA` returned
    from by a FAR `retf` (whose `BP+6` is `0x10000`, out of a 16-bit SP's
    reach), never satisfies the test: those run to a bound and RAISE,
    naming the return address that never arrived, rather than stopping
    somewhere plausible. A stack that wraps the segment boundary
    mid-epilogue is still not handled.

    Args:
        gdb: Anything satisfying `GDBLike`.
        timeout: Wall-clock ceiling, in seconds, on the whole operation.
            This is the bound that fires first at the default `max_steps`,
            and it is meant to.
        max_steps: Ceiling on the number of instructions stepped. A
            backstop for the case `timeout` cannot cover -- a caller
            passing `math.inf` or an effectively unbounded timeout -- so it
            is set above what the default timeout can reach rather than
            below it, which would let the two race. An iteration is two GDB
            round-trips (`s` then `g`) and measured 0.114 ms against a live
            emulator over loopback with `TCP_NODELAY` on both ends, so the
            default 10 s reaches roughly 88,000 steps and this default is
            about 2.5x that -- clear of the timeout even on a host several
            times faster, and still under half a minute of stepping for the
            unbounded-timeout caller it exists for.

    Returns:
        The stop reply from the final `step`, as `bytes` -- matching
        `GDBClient.step`.

    Raises:
        FrameWalkError: If `SP > BP` on entry, if `BP` is above
            `MAX_STEPPABLE_FRAME_BP`, or if the frame has not returned
            before either bound is reached. This never returns silently
            without having stepped out.
        Exception: Whatever `gdb.read_registers`, `gdb.read_memory` or
            `gdb.step` raises is deliberately allowed to propagate
            unchanged -- unlike `walk_frames`, this function wraps no calls
            in `try`, because a transport failure mid-step-out leaves the
            target in an unknown place and must not be reported as a
            completed step-out.
    """
    registers = gdb.read_registers()
    frame_bp = registers["ebp"] & 0xFFFF
    entry_sp = registers["esp"] & 0xFFFF
    entry_cs = registers["cs"] & 0xFFFF
    ss = registers["ss"] & 0xFFFF
    if entry_sp > frame_bp:
        raise FrameWalkError(
            f"SP={entry_sp:#06x} is above BP={frame_bp:#06x}: BP does not describe "
            "the current frame (no frame pointer established, or a stale BP). "
            "SP == BP is legal -- a frame with no locals -- but SP above BP is not"
        )
    if frame_bp > MAX_STEPPABLE_FRAME_BP:
        raise FrameWalkError(
            f"BP={frame_bp:#06x} sits at the top of the stack segment: a return from "
            f"this frame would leave SP at {frame_bp + 4:#x} or above, which no 16-bit "
            "SP can hold, so no amount of stepping could recognise it"
        )

    _, return_off, return_seg = read_frame_record(gdb, ss, frame_bp)
    # Where a return lands SP, per calling convention. Anything below these
    # is still inside the callee, `pop bp` and `leave` included: both land
    # exactly on BP+2, with the `ret` not yet executed.
    near_sp = frame_bp + 4
    far_sp = frame_bp + 6
    # Purely diagnostic: the step at which SP first cleared the return slot
    # at [BP+2] without the return address having been reached. That is the
    # signature of a callee raising SP without returning, and saying so is
    # the difference between this failing loudly and the old rule's silent
    # early stop.
    passed_slot_at: int | None = None
    deadline = time.monotonic() + timeout

    for stepped in range(1, max_steps + 1):
        reply = gdb.step()
        registers = gdb.read_registers()
        sp = registers["esp"] & 0xFFFF
        ip = registers["eip"] & 0xFFFF
        cs = registers["cs"] & 0xFFFF
        if ip == return_off and (
            (cs == entry_cs and sp >= near_sp) or (cs == return_seg and sp >= far_sp)
        ):
            return reply
        if passed_slot_at is None and sp > frame_bp + 2:
            passed_slot_at = stepped
        if time.monotonic() >= deadline:
            raise FrameWalkError(
                f"frame at BP={frame_bp:#06x} did not return within the "
                f"{timeout}s timeout ({stepped} instructions stepped); "
                f"{_return_address_note(entry_cs, return_seg, return_off, passed_slot_at)}"
            )

    raise FrameWalkError(
        f"frame at BP={frame_bp:#06x} did not return within max_steps={max_steps}; "
        f"{_return_address_note(entry_cs, return_seg, return_off, passed_slot_at)}"
    )


def _return_address_note(
    entry_cs: int, return_seg: int, return_off: int, passed_slot_at: int | None
) -> str:
    """Describe the return address that never arrived, for a failure message.

    Args:
        entry_cs: CS at entry, which a near return preserves.
        return_seg: The word at `[BP+4]`, which a far return restores.
        return_off: The word at `[BP+2]`.
        passed_slot_at: The step at which SP first cleared `[BP+2]`, or
            `None` if it never did.

    Returns:
        A sentence naming the expected return address, and -- when SP did
        clear the return slot without the return happening -- when that
        occurred, which is the signature of a callee that raises SP without
        returning.
    """
    where = (
        f"CS:IP never reached {entry_cs:04x}:{return_off:04x} "
        f"(or {return_seg:04x}:{return_off:04x} if the call was far)"
    )
    if passed_slot_at is None:
        return f"{where}, and SP never cleared the return slot at [BP+2]"
    return (
        f"{where}, though SP cleared the return slot at [BP+2] on step "
        f"{passed_slot_at} -- a callee that raises SP without returning, or a "
        "rewritten return slot"
    )
