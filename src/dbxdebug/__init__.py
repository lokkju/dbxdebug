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
from .video import (
    DOSVideoTools,
    decode_vga_attribute,
    format_attribute_info,
    DOS_VIDEO_PAGE_ONE,
    DOS_VIDEO_PAGE_TWO,
    DOS_VIDEO_MEMORY_SIZE,
    BDA_TIMER_TICK,
    TIMER_FREQUENCY,
)
from .dbx_kbd import (
    DBX_KEY,
    DBX_KEY_TO_QCODE,
    QCODE_TO_DBX_KEY,
    dbx_key_to_qcode,
    qcode_to_dbx_key,
    char_to_qcode,
    char_needs_shift,
)
from .utils import parse_x86_address, hexdump
from .keyboard import (
    key_list,
    ctrl_key,
    alt_key,
    shift_key,
    ctrl_alt_key,
    ctrl_shift_key,
    function_key,
    digit_key,
    number_keys,
    ENTER,
    ESCAPE,
    TAB,
    BACKSPACE,
    SPACE,
    DELETE,
    CTRL_C,
    CTRL_V,
    CTRL_X,
    CTRL_Z,
    CTRL_A,
    CTRL_S,
    CTRL_ALT_DEL,
    ALT_F4,
    ALT_TAB,
)
from .html import (
    dos_video_to_html,
    save_dos_video_html,
    analyze_dos_video_colors,
    VGA_COLORS,
    VGA_COLOR_NAMES,
)
from .capture_io import ScreenRecorder, load_capture, save_capture, get_capture_path

__version__ = "0.1.0"

__all__ = [
    # Clients
    "GDBClient",
    "QMPClient",
    "QMPError",
    # Video tools
    "DOSVideoTools",
    "decode_vga_attribute",
    "format_attribute_info",
    "dos_video_to_html",
    "save_dos_video_html",
    "analyze_dos_video_colors",
    # Video constants
    "DOS_VIDEO_PAGE_ONE",
    "DOS_VIDEO_PAGE_TWO",
    "DOS_VIDEO_MEMORY_SIZE",
    "BDA_TIMER_TICK",
    "TIMER_FREQUENCY",
    "VGA_COLORS",
    "VGA_COLOR_NAMES",
    # Key codes
    "DBX_KEY",
    "DBX_KEY_TO_QCODE",
    "QCODE_TO_DBX_KEY",
    "dbx_key_to_qcode",
    "qcode_to_dbx_key",
    "char_to_qcode",
    "char_needs_shift",
    # Keyboard helpers
    "key_list",
    "ctrl_key",
    "alt_key",
    "shift_key",
    "ctrl_alt_key",
    "ctrl_shift_key",
    "function_key",
    "digit_key",
    "number_keys",
    # Common key constants
    "ENTER",
    "ESCAPE",
    "TAB",
    "BACKSPACE",
    "SPACE",
    "DELETE",
    "CTRL_C",
    "CTRL_V",
    "CTRL_X",
    "CTRL_Z",
    "CTRL_A",
    "CTRL_S",
    "CTRL_ALT_DEL",
    "ALT_F4",
    "ALT_TAB",
    # Capture I/O
    "ScreenRecorder",
    "load_capture",
    "save_capture",
    "get_capture_path",
    # Utilities
    "parse_x86_address",
    "hexdump",
]
