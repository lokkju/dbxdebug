"""
Utility functions for address parsing and hex dumps.
"""

import re


def parse_x86_address(address_str: str | int) -> int:
    """
    Parse an x86 address and convert to linear address.

    Supports:
    - Segmented addresses: "XXXX:YYYY" or "0xXXXX:YYYY"
    - Linear hex: "0x12345" or "12345"
    - Integer passthrough

    Args:
        address_str: Address as string or int

    Returns:
        Linear address as integer

    Raises:
        ValueError: If address format is invalid
    """
    if isinstance(address_str, int):
        return address_str

    # Check if it's a segmented address
    segment_match = re.match(r"(?:0x)?([0-9a-fA-F]+):([0-9a-fA-F]+)", address_str)
    if segment_match:
        segment = int(segment_match.group(1), 16)
        offset = int(segment_match.group(2), 16)
        # Linear address: segment * 16 + offset
        return (segment << 4) + offset

    # Try to interpret as a normal hex or decimal number
    try:
        return int(address_str, 0)
    except ValueError:
        raise ValueError(
            f"Invalid address format: {address_str}. "
            "Use segment:offset (e.g., b800:0000) or linear address."
        )


def hexdump(
    src: bytes,
    bytes_per_line: int = 32,
    bytes_per_group: int = 4,
    sep: str = ".",
    start_addr: int = 0,
) -> list[str]:
    """
    Generate a hex dump of binary data.

    Args:
        src: Binary data to dump
        bytes_per_line: Number of bytes per line
        bytes_per_group: Group bytes with spaces
        sep: Character for non-printable bytes
        start_addr: Starting address for display

    Returns:
        List of formatted hex dump lines
    """
    FILTER = "".join([(len(repr(chr(x))) == 3) and chr(x) or sep for x in range(256)])
    lines = []
    max_addr_len = max(8, len(hex(start_addr + len(src))))

    for offset in range(0, len(src), bytes_per_line):
        addr = start_addr + offset
        chars = src[offset : offset + bytes_per_line]

        # Create hex string
        hex_parts = []
        for i in range(0, len(chars), bytes_per_group):
            group = chars[i : i + bytes_per_group]
            hex_parts.append("".join(f"{b:02X}" for b in group))
        hex_string = " ".join(hex_parts)

        # Pad to align with full lines
        full_groups = bytes_per_line // bytes_per_group
        expected_len = full_groups * (bytes_per_group * 2) + (full_groups - 1)
        hex_string = hex_string.ljust(expected_len)

        # Create printable string
        printable = "".join((x <= 127 and FILTER[x]) or sep for x in chars)

        lines.append(f"{addr:0{max_addr_len}X}  {hex_string}  |{printable}|")

    return lines
