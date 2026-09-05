"""The one place that knows how an address is encoded."""

import pytest

from dbxdebug.addressing import (
    CS_INDEX,
    EIP_INDEX,
    PackedAddressError,
    bp_addr,
    linear,
    linear_pc,
    parse_address,
)


def test_linear_is_seg_times_sixteen_plus_offset():
    assert linear(0x0824, 0x5A90) == 0x0824 * 16 + 0x5A90


def test_parse_address_accepts_an_int_unchanged():
    assert parse_address(0x30000) == 0x30000


def test_parse_address_accepts_a_seg_off_string():
    assert parse_address("0824:5a90") == 0x0824 * 16 + 0x5A90


def test_parse_address_rejects_a_packed_far_pointer():
    """0824:5A90 packed is 0x08245A90. Real-mode linear addresses stop just
    past 1 MB including the HMA, so nothing legitimate reaches here."""
    with pytest.raises(PackedAddressError, match="packed far pointer"):
        parse_address(0x08245A90)


def test_parse_address_allows_the_whole_real_mode_range():
    """The HMA tops out at 0xFFFF0 + 0xFFFF = 0x10FFEF, which must pass."""
    assert parse_address(0x10FFEF) == 0x10FFEF


def test_linear_pc_uses_cs_and_eip_indices():
    regs = [0] * 16
    regs[CS_INDEX] = 0x3000
    regs[EIP_INDEX] = 0x0100
    assert linear_pc(regs) == 0x30100


def test_bp_addr_raises_rather_than_packing():
    """The old helper packed (seg << 16) | off, correct against pre-fix
    builds and wrong against current ones. It must never quietly encode."""
    with pytest.raises(PackedAddressError, match="linear"):
        bp_addr(0x0824, 0x5A90)
