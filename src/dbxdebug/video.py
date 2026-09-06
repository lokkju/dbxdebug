"""
DOS video memory access utilities.

Provides screen capture and video memory inspection for DOS text mode.

`DOSVideoTools` either OWNS a `GDBClient` it built itself or BORROWS one the
caller passed in, and it closes only a client it owns. Borrowing is the
important case: the DOSBox-X stub serves ONE GDB client at a time
(lokkju/dbxdebug#8), so a second connection completes the TCP handshake and
then blocks forever in the `qSupported` exchange, with no read timeout to end
it (lokkju/dbxdebug#4). Anything that already holds a client -- a
`DosboxSession`, an embedding CLI -- must hand that client over rather than
let these tools open a competing one (lokkju/dbxdebug#11).
"""

from loguru import logger

from .gdb import GDBClient

__all__ = [
    "BDA_COLUMN_COUNT",
    "BDA_MODE",
    "BDA_ROW_COUNT",
    "BDA_TIMER_TICK",
    "DEFAULT_SCREEN_HEIGHT",
    "DEFAULT_SCREEN_WIDTH",
    "DOSVideoTools",
    "DOS_VIDEO_MEMORY_SIZE",
    "DOS_VIDEO_PAGE_ONE",
    "DOS_VIDEO_PAGE_TWO",
    "TIMER_FREQUENCY",
    "decode_text_screen",
    "decode_vga_attribute",
    "format_attribute_info",
]

# DOS video memory addresses
DOS_VIDEO_PAGE_ONE = "0xB800:0000"
DOS_VIDEO_PAGE_TWO = "0xB800:1000"
DOS_VIDEO_MEMORY_SIZE = 0xFA0  # 4000 bytes (80 * 25 * 2)

# BIOS Data Area addresses
BDA_MODE = "0x0040:0049"
BDA_COLUMN_COUNT = "0x0040:004A"
BDA_ROW_COUNT = "0x0040:0084"
BDA_TIMER_TICK = "0x0040:006C"  # 4-byte tick counter, 18.2065 Hz

# Timer constants
TIMER_FREQUENCY = 18.2065  # Hz

# Default VGA text-mode geometry.
DEFAULT_SCREEN_WIDTH = 80
DEFAULT_SCREEN_HEIGHT = 25


def decode_text_screen(
    memory: bytes,
    width: int = DEFAULT_SCREEN_WIDTH,
    height: int = DEFAULT_SCREEN_HEIGHT,
) -> list[str]:
    """Decode a VGA text-mode framebuffer into one string per row.

    THE single decode path for text-mode video memory. `screen_dump`,
    `screen_dump_with_ticks` and `DosboxSession.screen_lines` all call this
    rather than unpacking cells themselves; three copies of the same loop was
    the drift risk recorded in lokkju/dbxdebug#7.

    Video memory holds two bytes per cell -- the character first, then the
    attribute byte, which is discarded here. A cell holding 0x00 renders as a
    space; every other byte becomes `chr(byte)`, so the guest's code page 437
    bytes come back as their Latin-1 code points, unmapped. Cells past the end
    of `memory` render as spaces rather than raising, so a short read still
    yields a full-size screen.

    Args:
        memory: Raw video memory, character/attribute interleaved.
        width: Screen columns.
        height: Screen rows.

    Returns:
        `height` strings of `width` characters each.
    """
    lines = []
    for row in range(height):
        chars = []
        for col in range(width):
            index = (row * width + col) * 2
            byte = memory[index] if index < len(memory) else 0
            chars.append(" " if byte == 0 else chr(byte))
        lines.append("".join(chars))
    return lines


class DOSVideoTools:
    """Tools for analyzing DOS program screen output.

    Wraps a `GDBClient` that this object either owns or borrows. See
    `__init__` for how to choose, and `close` for what each choice means at
    teardown.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        *,
        gdb: GDBClient | None = None,
    ):
        """
        Initialize video tools over an owned or a borrowed GDB connection.

        Pass `gdb` to BORROW a client the caller already has -- typically
        `DOSVideoTools(gdb=session.gdb)`. This object then never closes it;
        the caller stays its owner. Pass `host`/`port` (or neither, for the
        defaults) to have this object OWN a client it builds, which `close`
        then closes.

        Borrowing is not a nicety. The stub serves one GDB client at a time,
        so opening a second one against a session that already has one hangs
        rather than fails (lokkju/dbxdebug#11).

        Args:
            host: GDB server hostname. Defaults to localhost. Only valid
                when building an owned client.
            port: GDB server port. Defaults to `GDBClient.DEFAULT_PORT`.
                Only valid when building an owned client.
            gdb: An already-connected client to borrow. Mutually exclusive
                with `host`/`port`.

        Raises:
            ValueError: If `gdb` is combined with `host` or `port`. Silently
                ignoring an explicitly passed port would connect somewhere
                the caller did not ask for.
        """
        if gdb is not None:
            if host is not None or port is not None:
                raise ValueError(
                    "pass either gdb= (to borrow a connected client) or host/port "
                    "(to build one), not both"
                )
            self.gdb = gdb
            self._owns_gdb = False
        else:
            self.gdb = GDBClient(
                "localhost" if host is None else host,
                GDBClient.DEFAULT_PORT if port is None else port,
            )
            self._owns_gdb = True

    @property
    def owns_client(self) -> bool:
        """Whether this object built its own client and must close it.

        Returns:
            True if the client was built here, False if it was borrowed.
        """
        return self._owns_gdb

    def close(self) -> None:
        """Close the GDB connection, but only if this object owns it.

        A BORROWED client is left open: its owner closes it. Closing here
        would leave the owner holding a dead socket, and a double close on
        the way out is exactly the failure that resurfaces later as an
        unrelated hang.
        """
        if self._owns_gdb:
            self.gdb.close()

    def __enter__(self) -> "DOSVideoTools":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close an owned client. Leaves a borrowed one open -- see `close`."""
        self.close()

    def read_timer_ticks(self) -> int | None:
        """
        Read the BIOS timer tick counter (18.2065 Hz).

        Returns:
            Tick count or None on error
        """
        try:
            data = self.gdb.read_memory(BDA_TIMER_TICK, 4)
            return int.from_bytes(data, "little")
        except Exception as e:
            logger.exception(e)
            return None

    def read_video_mode(self) -> int | None:
        """
        Read the current video mode.

        Returns:
            Video mode number or None on error
        """
        try:
            data = self.gdb.read_memory(BDA_MODE, 1)
            return data[0]
        except Exception as e:
            logger.exception(e)
            return None

    def screen_dump(self, page: int = 1) -> list[str] | None:
        """
        Dump the DOS text screen as a list of strings.

        Args:
            page: Video page number (1 or 2)

        Returns:
            List of 25 strings (80 chars each) or None on error
        """
        try:
            addr = DOS_VIDEO_PAGE_ONE if page == 1 else DOS_VIDEO_PAGE_TWO
            memory = self.gdb.read_memory(addr, DOS_VIDEO_MEMORY_SIZE)
            return decode_text_screen(memory)
        except Exception as e:
            logger.exception(e)
            return None

    def screen_dump_with_ticks(self) -> tuple[list[str] | None, int | None]:
        """
        Dump screen and timer ticks together for timing correlation.

        Returns:
            Tuple of (lines, ticks) or (None, None) on error
        """
        try:
            # Read timer first (small read, fast)
            tick_data = self.gdb.read_memory(BDA_TIMER_TICK, 4)
            ticks = int.from_bytes(tick_data, "little")

            # Then read screen
            memory = self.gdb.read_memory(DOS_VIDEO_PAGE_ONE, DOS_VIDEO_MEMORY_SIZE)
            return (decode_text_screen(memory), ticks)
        except Exception as e:
            logger.exception(e)
            return (None, None)

    def screen_raw(self, page: int = 1) -> bytes | None:
        """
        Read raw video memory (characters and attributes).

        Args:
            page: Video page number (1 or 2)

        Returns:
            Raw bytes or None on error
        """
        try:
            addr = DOS_VIDEO_PAGE_ONE if page == 1 else DOS_VIDEO_PAGE_TWO
            return self.gdb.read_memory(addr, DOS_VIDEO_MEMORY_SIZE)
        except Exception as e:
            logger.exception(e)
            return None

    def screen_debug(self) -> list[bytes] | None:
        """
        Read raw video memory from both pages.

        Returns:
            List of [page1_bytes, page2_bytes] or None on error
        """
        try:
            return [
                self.gdb.read_memory(DOS_VIDEO_PAGE_ONE, DOS_VIDEO_MEMORY_SIZE),
                self.gdb.read_memory(DOS_VIDEO_PAGE_TWO, DOS_VIDEO_MEMORY_SIZE),
            ]
        except Exception as e:
            logger.exception(e)
            return None


def decode_vga_attribute(attr_byte: int) -> dict:
    """
    Decode a VGA text mode attribute byte.

    Attribute format: IRGB irgb
    - Upper 4 bits: background color (IRGB)
    - Lower 4 bits: foreground color (irgb)
    - Bit 7: blink flag

    Args:
        attr_byte: Attribute byte value

    Returns:
        Dict with foreground, background, colors, and blink flag
    """
    color_names = [
        "Black",
        "Blue",
        "Green",
        "Cyan",
        "Red",
        "Magenta",
        "Brown",
        "Light Gray",
        "Dark Gray",
        "Light Blue",
        "Light Green",
        "Light Cyan",
        "Light Red",
        "Light Magenta",
        "Yellow",
        "White",
    ]

    foreground = attr_byte & 0x0F
    background = (attr_byte & 0x70) >> 4
    blink = (attr_byte & 0x80) != 0

    return {
        "foreground": foreground,
        "background": background,
        "fg_color": color_names[foreground],
        "bg_color": color_names[background],
        "blink": blink,
        "raw_value": attr_byte,
    }


def format_attribute_info(attr_byte: int) -> str:
    """Format attribute information as a readable string."""
    info = decode_vga_attribute(attr_byte)

    result = f"Attribute 0x{attr_byte:02X}:\n"
    result += f"  Foreground: {info['fg_color']} (0x{info['foreground']:X})\n"
    result += f"  Background: {info['bg_color']} (0x{info['background']:X})\n"
    result += f"  Blinking: {'Yes' if info['blink'] else 'No'}\n"

    return result
