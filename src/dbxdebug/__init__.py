"""dbxdebug - client library and CLI for DOSBox-X remote debug protocols.

THE EXPORT RULE, in one sentence: every module declares its own supported
surface in ``__all__``, and this package root re-exports the union of the
LIBRARY modules' ``__all__`` -- ``addressing``, ``capture_io``, ``dbx_kbd``,
``frames``, ``gdb``, ``html``, ``keyboard``, ``paths``, ``qmp``, ``session``,
``utils`` and ``video``.

The three modules deliberately NOT re-exported are ``cli``, ``registry`` and
``doctor``: the ``dbxdebug`` command's own machinery. They act on the host's
whole set of sessions rather than on the one you launched, and their names
mean nothing unqualified -- ``run``, ``reap``, ``list_sessions``,
``free_port`` and ``main`` are all names a package root should not own.
Reach them as ``dbxdebug.registry.list_sessions`` and ``dbxdebug.doctor.run``.
Leaving ``cli`` out is also what keeps ``import dbxdebug`` free of ``click``:
the CLI dependency is never pulled in on the library path.

A derived root beats a curated one. Before this rule the root exported
``GDBClient`` but not the ``GDBTimeoutError`` you catch from it, and ``ENTER``
but not ``UP`` -- gaps nobody chose, which is what a re-export list
maintained by hand at a distance from the code it names decays into. The
choice is now made once per module, beside the definition, and
``tests/test_exports.py`` asserts this root is exactly that union.

Every ``from dbxdebug.<module> import ...`` keeps working; this is purely
additive.

The entry points, in the order a caller meets them:

- ``DosboxSession`` -- launch an emulator, connect to it, tear it down.
- ``GDBClient`` -- memory, registers, breakpoints, stepping.
- ``QMPClient`` -- keys, mouse, screendumps, save states, run control.
- ``DOSVideoTools`` and the ``html`` helpers -- read and render the screen.
- ``linear`` / ``parse_address`` and ``walk_frames`` -- make sense of what
  the two clients hand back.
"""

from importlib.metadata import version

from .addressing import (
    CS_INDEX,
    EIP_INDEX,
    REAL_MODE_CEILING,
    PackedAddressError,
    bp_addr,
    linear,
    linear_pc,
    parse_address,
)
from .capture_io import ScreenRecorder, get_capture_path, load_capture, save_capture
from .dbx_kbd import (
    DBX_KEY,
    DBX_KEY_TO_QCODE,
    QCODE_TO_DBX_KEY,
    char_needs_shift,
    char_to_qcode,
    dbx_key_to_qcode,
    qcode_to_dbx_key,
)
from .frames import (
    FRAME_RECORD_SIZE,
    MAX_STEPPABLE_FRAME_BP,
    SEGMENT_SIZE,
    Frame,
    FrameWalkError,
    GDBLike,
    read_frame_record,
    steps_out,
    walk_frames,
)
from .gdb import (
    DEFAULT_TIMEOUT,
    LINEAR_BREAKPOINTS_CAPABILITY,
    REGISTER_NAMES,
    GDBClient,
    GDBDesyncError,
    GDBTimeoutError,
    IncompatibleStubError,
    looks_like_stop_reply,
)
from .html import (
    CP437_MAP,
    VGA_COLOR_NAMES,
    VGA_COLORS,
    analyze_dos_video_colors,
    char_to_html,
    dos_video_to_html,
    save_dos_video_html,
)
from .keyboard import (
    ALT_F4,
    ALT_TAB,
    BACKSPACE,
    CTRL_A,
    CTRL_ALT_DEL,
    CTRL_C,
    CTRL_S,
    CTRL_V,
    CTRL_X,
    CTRL_Z,
    DELETE,
    DOWN,
    END,
    ENTER,
    ESCAPE,
    HOME,
    INSERT,
    LEFT,
    PAGE_DOWN,
    PAGE_UP,
    RIGHT,
    SPACE,
    TAB,
    UP,
    alt_key,
    ctrl_alt_key,
    ctrl_key,
    ctrl_shift_key,
    digit_key,
    function_key,
    get_qcode,
    key_list,
    number_keys,
    shift_key,
)
from .paths import (
    DEFAULT_DOSBOX_X_PATH,
    DOSBOX_X_ENV_VAR,
    configured_dosbox_x_path,
    find_dosbox_x,
)
from .qmp import MOUSE_BUTTONS, CpuNotStoppedError, QMPClient, QMPError
from .session import (
    DEFAULT_CONF,
    DEFAULT_SDL_OUTPUT,
    HEADLESS_ENV,
    DosboxLaunchError,
    DosboxSession,
    render_conf,
)
from .utils import hexdump, parse_x86_address
from .video import (
    BDA_COLUMN_COUNT,
    BDA_MODE,
    BDA_ROW_COUNT,
    BDA_TIMER_TICK,
    DEFAULT_SCREEN_HEIGHT,
    DEFAULT_SCREEN_WIDTH,
    DOS_VIDEO_MEMORY_SIZE,
    DOS_VIDEO_PAGE_ONE,
    DOS_VIDEO_PAGE_TWO,
    TIMER_FREQUENCY,
    DOSVideoTools,
    decode_text_screen,
    decode_vga_attribute,
    format_attribute_info,
)

__version__ = version("dbxdebug")

# Grouped by owning module, and in each group in the order a caller meets the
# names -- NOT alphabetically. This list is derived, not curated: it must
# equal the union of the library modules' `__all__`, which
# `tests/test_exports.py` checks. Add a name to its module's `__all__` first.
__all__ = [
    # -- Sessions: dbxdebug.session ------------------------------------
    "DosboxSession",
    "DosboxLaunchError",
    "render_conf",
    "DEFAULT_CONF",
    "DEFAULT_SDL_OUTPUT",
    "HEADLESS_ENV",
    # -- Clients: dbxdebug.gdb, dbxdebug.qmp ---------------------------
    "GDBClient",
    "GDBDesyncError",
    "GDBTimeoutError",
    "IncompatibleStubError",
    "looks_like_stop_reply",
    "REGISTER_NAMES",
    "DEFAULT_TIMEOUT",
    "LINEAR_BREAKPOINTS_CAPABILITY",
    "QMPClient",
    "QMPError",
    "CpuNotStoppedError",
    "MOUSE_BUTTONS",
    # -- Addresses and frames: dbxdebug.addressing, dbxdebug.frames ----
    "linear",
    "linear_pc",
    "parse_address",
    "bp_addr",
    "PackedAddressError",
    "CS_INDEX",
    "EIP_INDEX",
    "REAL_MODE_CEILING",
    "walk_frames",
    "steps_out",
    "Frame",
    "FrameWalkError",
    "GDBLike",
    "FRAME_RECORD_SIZE",
    "SEGMENT_SIZE",
    "MAX_STEPPABLE_FRAME_BP",
    "read_frame_record",
    # -- Locating the emulator: dbxdebug.paths -------------------------
    "find_dosbox_x",
    "configured_dosbox_x_path",
    "DOSBOX_X_ENV_VAR",
    "DEFAULT_DOSBOX_X_PATH",
    # -- Video: dbxdebug.video, dbxdebug.html --------------------------
    "DOSVideoTools",
    "decode_text_screen",
    "decode_vga_attribute",
    "format_attribute_info",
    "dos_video_to_html",
    "save_dos_video_html",
    "analyze_dos_video_colors",
    "char_to_html",
    "DOS_VIDEO_PAGE_ONE",
    "DOS_VIDEO_PAGE_TWO",
    "DOS_VIDEO_MEMORY_SIZE",
    "DEFAULT_SCREEN_WIDTH",
    "DEFAULT_SCREEN_HEIGHT",
    "BDA_MODE",
    "BDA_COLUMN_COUNT",
    "BDA_ROW_COUNT",
    "BDA_TIMER_TICK",
    "TIMER_FREQUENCY",
    "VGA_COLORS",
    "VGA_COLOR_NAMES",
    "CP437_MAP",
    # -- Key codes: dbxdebug.dbx_kbd -----------------------------------
    "DBX_KEY",
    "DBX_KEY_TO_QCODE",
    "QCODE_TO_DBX_KEY",
    "dbx_key_to_qcode",
    "qcode_to_dbx_key",
    "char_to_qcode",
    "char_needs_shift",
    # -- Keyboard helpers: dbxdebug.keyboard ---------------------------
    "get_qcode",
    "key_list",
    "ctrl_key",
    "alt_key",
    "shift_key",
    "ctrl_alt_key",
    "ctrl_shift_key",
    "function_key",
    "digit_key",
    "number_keys",
    "ENTER",
    "ESCAPE",
    "TAB",
    "BACKSPACE",
    "SPACE",
    "DELETE",
    "INSERT",
    "HOME",
    "END",
    "PAGE_UP",
    "PAGE_DOWN",
    "UP",
    "DOWN",
    "LEFT",
    "RIGHT",
    "CTRL_C",
    "CTRL_V",
    "CTRL_X",
    "CTRL_Z",
    "CTRL_A",
    "CTRL_S",
    "CTRL_ALT_DEL",
    "ALT_F4",
    "ALT_TAB",
    # -- Capture I/O: dbxdebug.capture_io ------------------------------
    "ScreenRecorder",
    "load_capture",
    "save_capture",
    "get_capture_path",
    # -- Utilities: dbxdebug.utils -------------------------------------
    "parse_x86_address",
    "hexdump",
]
