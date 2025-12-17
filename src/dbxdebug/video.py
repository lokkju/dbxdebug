"""
DOS video memory access utilities.

Provides screen capture and video memory inspection for DOS text mode.
"""

from loguru import logger

from .gdb import GDBClient

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


class DOSVideoTools:
    """Tools for analyzing DOS program screen output."""

    def __init__(self, host: str = "localhost", port: int = GDBClient.DEFAULT_PORT):
        """
        Initialize video tools with GDB connection.

        Args:
            host: GDB server hostname
            port: GDB server port
        """
        self.gdb = GDBClient(host, port)

    def close(self) -> None:
        """Close the GDB connection."""
        self.gdb.close()

    def __enter__(self) -> "DOSVideoTools":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
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

            lines = []
            for row in range(25):
                line_text = ""
                for col in range(80):
                    char_index = (row * 80 + col) * 2
                    if char_index < len(memory):
                        char = memory[char_index]
                        if char == 0:
                            line_text += " "
                        elif 32 <= char <= 126:
                            line_text += chr(char)
                        else:
                            line_text += chr(char)
                    else:
                        line_text += " "
                lines.append(line_text)
            return lines
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

            lines = []
            for row in range(25):
                line_text = ""
                for col in range(80):
                    char_index = (row * 80 + col) * 2
                    if char_index < len(memory):
                        char = memory[char_index]
                        if char == 0 or char == 32:
                            line_text += " "
                        elif 32 <= char <= 126:
                            line_text += chr(char)
                        else:
                            line_text += chr(char)
                    else:
                        line_text += " "
                lines.append(line_text)
            return (lines, ticks)
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
