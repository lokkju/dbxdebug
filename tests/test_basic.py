"""Basic tests for dbxdebug library."""

import pytest

from dbxdebug import (
    ALT_F4,
    CTRL_C,
    # Key helpers
    DBX_KEY,
    ENTER,
    # Video tools
    ScreenRecorder,
    # Version
    __version__,
    alt_key,
    ctrl_key,
    decode_vga_attribute,
    hexdump,
    # Utils
    parse_x86_address,
    shift_key,
)


class TestVersion:
    """Test version is accessible."""

    def test_version_exists(self):
        """Version string should be defined."""
        assert __version__ is not None
        assert isinstance(__version__, str)
        assert len(__version__) > 0


class TestAddressParsing:
    """Test x86 address parsing."""

    def test_segment_offset(self):
        """Parse segment:offset format."""
        assert parse_x86_address("b800:0000") == 0xB8000
        assert parse_x86_address("0000:0000") == 0x0
        assert parse_x86_address("0040:006C") == 0x46C

    def test_hex_address(self):
        """Parse hex address."""
        assert parse_x86_address("0x1000") == 0x1000
        assert parse_x86_address("0xB8000") == 0xB8000

    def test_decimal_address(self):
        """Parse decimal address."""
        assert parse_x86_address("4096") == 4096

    def test_invalid_address(self):
        """Invalid address raises ValueError."""
        with pytest.raises(ValueError):
            parse_x86_address("invalid")


class TestVGAAttribute:
    """Test VGA attribute decoding."""

    def test_decode_basic(self):
        """Decode basic VGA attribute byte."""
        # 0x07 = light gray on black
        attr = decode_vga_attribute(0x07)
        assert attr["foreground"] == 7
        assert attr["background"] == 0
        assert attr["blink"] is False

    def test_decode_with_blink(self):
        """Decode attribute with blink bit."""
        # 0x87 = light gray on black, blinking
        attr = decode_vga_attribute(0x87)
        assert attr["foreground"] == 7
        assert attr["background"] == 0
        assert attr["blink"] is True

    def test_decode_colors(self):
        """Decode various color combinations."""
        # 0x1F = white on blue
        attr = decode_vga_attribute(0x1F)
        assert attr["foreground"] == 15
        assert attr["background"] == 1

        # 0x4E = yellow on red
        attr = decode_vga_attribute(0x4E)
        assert attr["foreground"] == 14
        assert attr["background"] == 4


class TestKeyboardHelpers:
    """Test keyboard helper functions."""

    def test_ctrl_key(self):
        """Create ctrl+key combination."""
        keys = ctrl_key(DBX_KEY.KBD_c)
        assert "ctrl" in keys
        assert "c" in keys

    def test_alt_key(self):
        """Create alt+key combination."""
        keys = alt_key(DBX_KEY.KBD_f4)
        assert "alt" in keys
        assert "f4" in keys

    def test_shift_key(self):
        """Create shift+key combination."""
        keys = shift_key(DBX_KEY.KBD_a)
        assert "shift" in keys
        assert "a" in keys

    def test_predefined_constants(self):
        """Predefined key constants exist."""
        assert CTRL_C is not None
        assert ALT_F4 is not None
        assert ENTER is not None


class TestDBXKey:
    """Test DBX_KEY enum."""

    def test_key_enum_values(self):
        """DBX_KEY enum has expected values."""
        # Values are DOSBox-X internal codes, not scan codes
        assert DBX_KEY.KBD_a.value == 0x15
        assert DBX_KEY.KBD_enter.value == 0x34
        assert DBX_KEY.KBD_esc.value == 0x31


class TestHexdump:
    """Test hexdump utility."""

    def test_basic_hexdump(self):
        """Generate hexdump of bytes."""
        data = b"Hello World!"
        result = hexdump(data, bytes_per_line=16)
        # hexdump returns list of strings
        assert isinstance(result, list)
        assert len(result) > 0
        joined = "\n".join(result).lower()
        assert "48656c6c" in joined  # "Hell" in hex (grouped)
        assert "hello" in joined  # ASCII representation

    def test_empty_data(self):
        """Hexdump of empty data."""
        result = hexdump(b"")
        assert result == []


class TestScreenRecorder:
    """Test ScreenRecorder without connection."""

    def test_init(self):
        """Initialize recorder."""
        recorder = ScreenRecorder()
        assert len(recorder) == 0
        assert recorder.timestamps == []

    def test_metadata(self):
        """Recorder accepts metadata."""
        recorder = ScreenRecorder(metadata={"test": "value"})
        assert recorder.metadata["test"] == "value"

    def test_clear(self):
        """Clear recorder data."""
        recorder = ScreenRecorder()
        recorder.screens[12345] = ["test"]
        assert len(recorder) == 1
        recorder.clear()
        assert len(recorder) == 0
