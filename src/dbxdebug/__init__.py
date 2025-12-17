"""
dbxdebug - Client library for DOSBox-X remote debug protocols.

Provides:
- GDBClient: GDB remote serial protocol for debugging (memory, registers, breakpoints)
- QMPClient: QEMU Monitor Protocol for keyboard input
- Video/screen capture utilities
- Keyboard helpers for building key sequences
"""

from .gdb import GDBClient
from .qmp import QMPClient, QMPError
from .video import DOSVideoTools, decode_vga_attribute
from .dbx_kbd import DBX_KEY, DBX_KEY_TO_QCODE, QCODE_TO_DBX_KEY
from .utils import parse_x86_address, hexdump
from .keyboard import (
    ctrl_key,
    alt_key,
    shift_key,
    ctrl_alt_key,
    function_key,
    ENTER,
    ESCAPE,
    CTRL_C,
    CTRL_ALT_DEL,
)

__version__ = "0.1.0"

__all__ = [
    # Clients
    "GDBClient",
    "QMPClient",
    "QMPError",
    # Video
    "DOSVideoTools",
    "decode_vga_attribute",
    # Keys
    "DBX_KEY",
    "DBX_KEY_TO_QCODE",
    "QCODE_TO_DBX_KEY",
    # Keyboard helpers
    "ctrl_key",
    "alt_key",
    "shift_key",
    "ctrl_alt_key",
    "function_key",
    "ENTER",
    "ESCAPE",
    "CTRL_C",
    "CTRL_ALT_DEL",
    # Utilities
    "parse_x86_address",
    "hexdump",
]
