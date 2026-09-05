"""The one place that knows how an address is encoded.

DOSBox-X's GDB stub takes a **linear** address for `Z0`/`z0` (set/remove
breakpoint) -- the same encoding used by `m`/`M` (read/write memory). Older
builds got this wrong: they split the argument as a packed far pointer,
recovering a segment with `seg = addr >> 16`. A breakpoint set anywhere above
`0x10000` was therefore stored at a garbage location and never fired, while
the stub still answered `OK`. Below `0x10000` the two interpretations happen
to coincide, which is exactly why the bug looked like it worked for years.
Builds that advertise `dosbox-x-linear-bp+` in `qSupported` take linear
addresses, as this module assumes throughout.

There is a second, related trap this module exists to close off. A caller
holding a `seg`/`off` pair and packing it as `(seg << 16) | off` was CORRECT
against old builds and is WRONG against current ones -- and the stub answers
`OK` in both cases, so a silently-wrong call cannot be told apart from a
correct one by its response. `bp_addr` exists to be that old, tempting
helper, and it RAISES instead of packing anything: some downstream code
still calls it, and a loud failure here is the entire point.
"""

from collections.abc import Sequence

# Index of the CS register within a GDB register list.
CS_INDEX = 10

# Index of the EIP register within a GDB register list. EIP is an *offset
# within CS*, not a linear address on its own -- combining it with CS is
# what `linear_pc` is for.
EIP_INDEX = 8

# One megabyte plus 64 KB. This admits the entire real-mode address range
# including the High Memory Area: the top reachable real-mode address is
# `0xFFFF0 + 0xFFFF = 0x10FFEF`, comfortably under this ceiling. Anything at
# or above it cannot be a real linear address produced by real-mode
# segment:offset arithmetic, so `parse_address` treats it as a packed far
# pointer instead.
#
# This guard has a known, honestly-documented blind spot: a packed pair
# with a small segment is indistinguishable from a legitimate linear
# address. For example `0010:0000` packs to
# `(0x0010 << 16) | 0x0000 == 0x00100000`, which is below the ceiling and
# passes -- silently meaning linear address 0x100000 rather than the
# caller's intended offset 0x100 in segment 0x10. Real DOS programs load
# well above segment 0, so the realistic case of an accidentally packed
# pointer is caught; a pointer built from a near-zero segment is not.
REAL_MODE_CEILING = 0x110000


class PackedAddressError(ValueError):
    """Raised when an address looks like a packed far pointer, not a linear address."""


def linear(seg: int, off: int) -> int:
    """Convert a real-mode segment:offset pair to a linear address.

    Args:
        seg: Segment value.
        off: Offset within the segment.

    Returns:
        The linear address `seg * 16 + off`.
    """
    return seg * 16 + off


def parse_address(addr: int | str) -> int:
    """Parse a linear address given as an int or a `"seg:off"` hex string.

    Args:
        addr: Either an integer linear address, or a string of the form
            `"seg:off"` with both parts in hexadecimal.

    Returns:
        The linear address.

    Raises:
        ValueError: If `addr` is a string with no `:` separator, or if
            either the `seg` or `off` component of a `"seg:off"` string
            does not fit in 16 bits.
        PackedAddressError: If `addr` is an integer at or above
            `REAL_MODE_CEILING`, indicating it is almost certainly a packed
            far pointer (`(seg << 16) | off`) rather than a real linear
            address. See `REAL_MODE_CEILING` for the residual blind spot
            this check cannot catch.
    """
    if isinstance(addr, str):
        seg_str, sep, off_str = addr.partition(":")
        if not sep:
            raise ValueError(f'expected an int or a "seg:off" hex string, got {addr!r}')
        seg = int(seg_str, 16)
        off = int(off_str, 16)
        if not 0 <= seg <= 0xFFFF:
            raise ValueError(f"segment out of range (0x0000-0xFFFF): {seg:#x}")
        if not 0 <= off <= 0xFFFF:
            raise ValueError(f"offset out of range (0x0000-0xFFFF): {off:#x}")
        return linear(seg, off)

    if addr >= REAL_MODE_CEILING:
        raise PackedAddressError(
            f"address {addr:#x} looks like a packed far pointer "
            f"(seg={addr >> 16:#x}, off={addr & 0xFFFF:#x}); "
            "DOSBox-X's GDB stub expects a linear address"
        )
    return addr


def linear_pc(registers: Sequence[int]) -> int:
    """Compute the linear program counter from a GDB register list.

    Args:
        registers: A GDB-style register list, indexed as DOSBox-X's stub
            reports them.

    Returns:
        The linear address of the next instruction: `CS * 16 + EIP`, since
        `registers[EIP_INDEX]` is an offset within `registers[CS_INDEX]`,
        not a linear address by itself.
    """
    return registers[CS_INDEX] * 16 + registers[EIP_INDEX]


def bp_addr(seg: int, off: int) -> int:
    """Refuse to pack a segment:offset pair into a breakpoint address.

    This shim exists only to fail loudly. The historical helper it replaces
    packed its arguments as `(seg << 16) | off`, which was correct against
    pre-fix DOSBox-X builds that split `Z0`/`z0` arguments as far pointers,
    and is wrong against current builds that take a linear address. The GDB
    stub answers `OK` either way, so a caller cannot tell success from a
    silently misplaced breakpoint. Use `linear` (or `linear_pc`) instead.

    Args:
        seg: Segment value the caller was about to pack.
        off: Offset value the caller was about to pack.

    Raises:
        PackedAddressError: Always.
    """
    raise PackedAddressError(
        f"bp_addr({seg:#x}, {off:#x}) refused: breakpoints take a linear "
        "address, not a packed seg:off pair; use linear(seg, off) instead"
    )
