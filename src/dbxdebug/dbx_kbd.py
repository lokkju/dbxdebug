"""
DOSBox-X keyboard key codes and QMP QKeyCode mapping.

This module provides:
- DBX_KEY: Enum of DOSBox-X internal key codes
- DBX_KEY_TO_QCODE: Mapping from DBX_KEY to QMP qcode strings
- QCODE_TO_DBX_KEY: Reverse mapping from qcode strings to DBX_KEY
"""

from enum import IntEnum


class DBX_KEY(IntEnum):
    """DOSBox-X internal keyboard key codes."""

    KBD_NONE = 0x00
    KBD_1 = 0x01
    KBD_2 = 0x02
    KBD_3 = 0x03
    KBD_4 = 0x04
    KBD_5 = 0x05
    KBD_6 = 0x06
    KBD_7 = 0x07
    KBD_8 = 0x08
    KBD_9 = 0x09
    KBD_0 = 0x0A
    KBD_q = 0x0B
    KBD_w = 0x0C
    KBD_e = 0x0D
    KBD_r = 0x0E
    KBD_t = 0x0F
    KBD_y = 0x10
    KBD_u = 0x11
    KBD_i = 0x12
    KBD_o = 0x13
    KBD_p = 0x14
    KBD_a = 0x15
    KBD_s = 0x16
    KBD_d = 0x17
    KBD_f = 0x18
    KBD_g = 0x19
    KBD_h = 0x1A
    KBD_j = 0x1B
    KBD_k = 0x1C
    KBD_l = 0x1D
    KBD_z = 0x1E
    KBD_x = 0x1F
    KBD_c = 0x20
    KBD_v = 0x21
    KBD_b = 0x22
    KBD_n = 0x23
    KBD_m = 0x24
    KBD_f1 = 0x25
    KBD_f2 = 0x26
    KBD_f3 = 0x27
    KBD_f4 = 0x28
    KBD_f5 = 0x29
    KBD_f6 = 0x2A
    KBD_f7 = 0x2B
    KBD_f8 = 0x2C
    KBD_f9 = 0x2D
    KBD_f10 = 0x2E
    KBD_f11 = 0x2F
    KBD_f12 = 0x30
    KBD_esc = 0x31
    KBD_tab = 0x32
    KBD_backspace = 0x33
    KBD_enter = 0x34
    KBD_space = 0x35
    KBD_leftalt = 0x36
    KBD_rightalt = 0x37
    KBD_leftctrl = 0x38
    KBD_rightctrl = 0x39
    KBD_leftshift = 0x3A
    KBD_rightshift = 0x3B
    KBD_capslock = 0x3C
    KBD_scrolllock = 0x3D
    KBD_numlock = 0x3E
    KBD_grave = 0x3F
    KBD_minus = 0x40
    KBD_equals = 0x41
    KBD_backslash = 0x42
    KBD_leftbracket = 0x43
    KBD_rightbracket = 0x44
    KBD_semicolon = 0x45
    KBD_quote = 0x46
    KBD_period = 0x47
    KBD_comma = 0x48
    KBD_slash = 0x49
    KBD_extra_lt_gt = 0x4A
    KBD_printscreen = 0x4B
    KBD_pause = 0x4C
    KBD_insert = 0x4D
    KBD_home = 0x4E
    KBD_pageup = 0x4F
    KBD_delete = 0x50
    KBD_end = 0x51
    KBD_pagedown = 0x52
    KBD_left = 0x53
    KBD_up = 0x54
    KBD_down = 0x55
    KBD_right = 0x56
    KBD_kp1 = 0x57
    KBD_kp2 = 0x58
    KBD_kp3 = 0x59
    KBD_kp4 = 0x5A
    KBD_kp5 = 0x5B
    KBD_kp6 = 0x5C
    KBD_kp7 = 0x5D
    KBD_kp8 = 0x5E
    KBD_kp9 = 0x5F
    KBD_kp0 = 0x60
    KBD_kpdivide = 0x61
    KBD_kpmultiply = 0x62
    KBD_kpminus = 0x63
    KBD_kpplus = 0x64
    KBD_kpenter = 0x65
    KBD_kpperiod = 0x66
    KBD_lwindows = 0x67
    KBD_rwindows = 0x68
    KBD_rwinmenu = 0x69
    KBD_kpequals = 0x6A
    KBD_f13 = 0x6B
    KBD_f14 = 0x6C
    KBD_f15 = 0x6D
    KBD_f16 = 0x6E
    KBD_f17 = 0x6F
    KBD_f18 = 0x70
    KBD_f19 = 0x71
    KBD_f20 = 0x72
    KBD_f21 = 0x73
    KBD_f22 = 0x74
    KBD_f23 = 0x75
    KBD_f24 = 0x76
    KBD_jp_hankaku = 0x77
    KBD_jp_muhenkan = 0x78
    KBD_jp_henkan = 0x79
    KBD_jp_hiragana = 0x7A
    KBD_yen = 0x7B
    KBD_underscore = 0x7C
    KBD_ax = 0x7D
    KBD_conv = 0x7E
    KBD_nconv = 0x7F
    KBD_kor_hancha = 0x80
    KBD_kor_hanyong = 0x81
    KBD_jp_yen = 0x82
    KBD_jp_backslash = 0x83
    KBD_colon = 0x84
    KBD_caret = 0x85
    KBD_atsign = 0x86
    KBD_jp_ro = 0x87
    KBD_help = 0x88
    KBD_kpcomma = 0x89
    KBD_stop = 0x8A
    KBD_copy = 0x8B
    KBD_vf1 = 0x8C
    KBD_vf2 = 0x8D
    KBD_vf3 = 0x8E
    KBD_vf4 = 0x8F
    KBD_vf5 = 0x90
    KBD_kana = 0x91
    KBD_nfer = 0x92
    KBD_xfer = 0x93
    KBD_LAST = 0x94


# Mapping from DBX_KEY to QMP qcode strings
# Based on DOSBox-X qmp.cpp qkeyname_to_keycode()
DBX_KEY_TO_QCODE: dict[DBX_KEY, str] = {
    # Letters
    DBX_KEY.KBD_a: "a",
    DBX_KEY.KBD_b: "b",
    DBX_KEY.KBD_c: "c",
    DBX_KEY.KBD_d: "d",
    DBX_KEY.KBD_e: "e",
    DBX_KEY.KBD_f: "f",
    DBX_KEY.KBD_g: "g",
    DBX_KEY.KBD_h: "h",
    DBX_KEY.KBD_i: "i",
    DBX_KEY.KBD_j: "j",
    DBX_KEY.KBD_k: "k",
    DBX_KEY.KBD_l: "l",
    DBX_KEY.KBD_m: "m",
    DBX_KEY.KBD_n: "n",
    DBX_KEY.KBD_o: "o",
    DBX_KEY.KBD_p: "p",
    DBX_KEY.KBD_q: "q",
    DBX_KEY.KBD_r: "r",
    DBX_KEY.KBD_s: "s",
    DBX_KEY.KBD_t: "t",
    DBX_KEY.KBD_u: "u",
    DBX_KEY.KBD_v: "v",
    DBX_KEY.KBD_w: "w",
    DBX_KEY.KBD_x: "x",
    DBX_KEY.KBD_y: "y",
    DBX_KEY.KBD_z: "z",
    # Numbers
    DBX_KEY.KBD_0: "0",
    DBX_KEY.KBD_1: "1",
    DBX_KEY.KBD_2: "2",
    DBX_KEY.KBD_3: "3",
    DBX_KEY.KBD_4: "4",
    DBX_KEY.KBD_5: "5",
    DBX_KEY.KBD_6: "6",
    DBX_KEY.KBD_7: "7",
    DBX_KEY.KBD_8: "8",
    DBX_KEY.KBD_9: "9",
    # Function keys
    DBX_KEY.KBD_f1: "f1",
    DBX_KEY.KBD_f2: "f2",
    DBX_KEY.KBD_f3: "f3",
    DBX_KEY.KBD_f4: "f4",
    DBX_KEY.KBD_f5: "f5",
    DBX_KEY.KBD_f6: "f6",
    DBX_KEY.KBD_f7: "f7",
    DBX_KEY.KBD_f8: "f8",
    DBX_KEY.KBD_f9: "f9",
    DBX_KEY.KBD_f10: "f10",
    DBX_KEY.KBD_f11: "f11",
    DBX_KEY.KBD_f12: "f12",
    DBX_KEY.KBD_f13: "f13",
    DBX_KEY.KBD_f14: "f14",
    DBX_KEY.KBD_f15: "f15",
    DBX_KEY.KBD_f16: "f16",
    DBX_KEY.KBD_f17: "f17",
    DBX_KEY.KBD_f18: "f18",
    DBX_KEY.KBD_f19: "f19",
    DBX_KEY.KBD_f20: "f20",
    DBX_KEY.KBD_f21: "f21",
    DBX_KEY.KBD_f22: "f22",
    DBX_KEY.KBD_f23: "f23",
    DBX_KEY.KBD_f24: "f24",
    # Modifiers
    DBX_KEY.KBD_leftshift: "shift",
    DBX_KEY.KBD_rightshift: "shift_r",
    DBX_KEY.KBD_leftctrl: "ctrl",
    DBX_KEY.KBD_rightctrl: "ctrl_r",
    DBX_KEY.KBD_leftalt: "alt",
    DBX_KEY.KBD_rightalt: "alt_r",
    DBX_KEY.KBD_lwindows: "meta_l",
    DBX_KEY.KBD_rwindows: "meta_r",
    DBX_KEY.KBD_rwinmenu: "menu",
    # Special keys
    DBX_KEY.KBD_esc: "esc",
    DBX_KEY.KBD_tab: "tab",
    DBX_KEY.KBD_backspace: "backspace",
    DBX_KEY.KBD_enter: "ret",
    DBX_KEY.KBD_space: "spc",
    DBX_KEY.KBD_capslock: "caps_lock",
    DBX_KEY.KBD_numlock: "num_lock",
    DBX_KEY.KBD_scrolllock: "scroll_lock",
    # Navigation
    DBX_KEY.KBD_insert: "insert",
    DBX_KEY.KBD_delete: "delete",
    DBX_KEY.KBD_home: "home",
    DBX_KEY.KBD_end: "end",
    DBX_KEY.KBD_pageup: "pgup",
    DBX_KEY.KBD_pagedown: "pgdn",
    DBX_KEY.KBD_left: "left",
    DBX_KEY.KBD_right: "right",
    DBX_KEY.KBD_up: "up",
    DBX_KEY.KBD_down: "down",
    # Punctuation
    DBX_KEY.KBD_grave: "grave_accent",
    DBX_KEY.KBD_minus: "minus",
    DBX_KEY.KBD_equals: "equal",
    DBX_KEY.KBD_backslash: "backslash",
    DBX_KEY.KBD_leftbracket: "bracket_left",
    DBX_KEY.KBD_rightbracket: "bracket_right",
    DBX_KEY.KBD_semicolon: "semicolon",
    DBX_KEY.KBD_quote: "apostrophe",
    DBX_KEY.KBD_comma: "comma",
    DBX_KEY.KBD_period: "dot",
    DBX_KEY.KBD_slash: "slash",
    DBX_KEY.KBD_extra_lt_gt: "less",
    # System keys
    DBX_KEY.KBD_printscreen: "print",
    DBX_KEY.KBD_pause: "pause",
    # Keypad
    DBX_KEY.KBD_kp0: "kp_0",
    DBX_KEY.KBD_kp1: "kp_1",
    DBX_KEY.KBD_kp2: "kp_2",
    DBX_KEY.KBD_kp3: "kp_3",
    DBX_KEY.KBD_kp4: "kp_4",
    DBX_KEY.KBD_kp5: "kp_5",
    DBX_KEY.KBD_kp6: "kp_6",
    DBX_KEY.KBD_kp7: "kp_7",
    DBX_KEY.KBD_kp8: "kp_8",
    DBX_KEY.KBD_kp9: "kp_9",
    DBX_KEY.KBD_kpdivide: "kp_divide",
    DBX_KEY.KBD_kpmultiply: "kp_multiply",
    DBX_KEY.KBD_kpminus: "kp_subtract",
    DBX_KEY.KBD_kpplus: "kp_add",
    DBX_KEY.KBD_kpenter: "kp_enter",
    DBX_KEY.KBD_kpperiod: "kp_decimal",
    DBX_KEY.KBD_kpequals: "kp_equals",
    DBX_KEY.KBD_kpcomma: "kp_comma",
    # Japanese keys
    DBX_KEY.KBD_jp_henkan: "henkan",
    DBX_KEY.KBD_jp_muhenkan: "muhenkan",
    DBX_KEY.KBD_jp_hiragana: "hiragana",
    DBX_KEY.KBD_yen: "yen",
    DBX_KEY.KBD_jp_ro: "ro",
}

# Reverse mapping from qcode string to DBX_KEY
QCODE_TO_DBX_KEY: dict[str, DBX_KEY] = {v: k for k, v in DBX_KEY_TO_QCODE.items()}


def dbx_key_to_qcode(key: DBX_KEY) -> str | None:
    """
    Convert DBX_KEY to QMP qcode string.

    Args:
        key: DOSBox-X key code

    Returns:
        QMP qcode string or None if not mapped
    """
    return DBX_KEY_TO_QCODE.get(key)


def qcode_to_dbx_key(qcode: str) -> DBX_KEY | None:
    """
    Convert QMP qcode string to DBX_KEY.

    Args:
        qcode: QMP qcode string (e.g., "a", "ctrl", "ret")

    Returns:
        DBX_KEY or None if not mapped
    """
    return QCODE_TO_DBX_KEY.get(qcode)


def char_to_qcode(char: str) -> str | None:
    """
    Convert a single character to its QMP qcode.

    Args:
        char: Single character (a-z, 0-9, or punctuation)

    Returns:
        QMP qcode string or None if not mapped

    Example:
        >>> char_to_qcode('a')
        'a'
        >>> char_to_qcode('A')  # Same key, needs shift
        'a'
        >>> char_to_qcode(' ')
        'spc'
    """
    char = char.lower()
    if char == " ":
        return "spc"
    elif char == "\n" or char == "\r":
        return "ret"
    elif char == "\t":
        return "tab"
    elif char.isalnum():
        return char
    else:
        # Punctuation mapping
        punct_map = {
            "`": "grave_accent",
            "-": "minus",
            "=": "equal",
            "[": "bracket_left",
            "]": "bracket_right",
            "\\": "backslash",
            ";": "semicolon",
            "'": "apostrophe",
            ",": "comma",
            ".": "dot",
            "/": "slash",
        }
        return punct_map.get(char)


def char_needs_shift(char: str) -> bool:
    """
    Check if a character requires shift to type.

    Args:
        char: Single character

    Returns:
        True if shift is needed
    """
    shift_chars = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ~!@#$%^&*()_+{}|:"<>?')
    return char in shift_chars
