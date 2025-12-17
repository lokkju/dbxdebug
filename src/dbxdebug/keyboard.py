"""
Keyboard input helpers for QMP protocol.

Provides convenient functions for building key sequences and common
keyboard operations using the QMP protocol.
"""

from .dbx_kbd import DBX_KEY, DBX_KEY_TO_QCODE


def get_qcode(key: DBX_KEY) -> str:
    """
    Get QMP qcode for a DBX_KEY.

    Args:
        key: DOSBox-X key code

    Returns:
        QMP qcode string

    Raises:
        ValueError: If key has no QMP mapping
    """
    qcode = DBX_KEY_TO_QCODE.get(key)
    if qcode is None:
        raise ValueError(f"No QMP mapping for key: {key.name}")
    return qcode


def key_list(*keys: DBX_KEY) -> list[str]:
    """
    Convert DBX_KEY values to QMP qcode list.

    Args:
        *keys: DBX_KEY values

    Returns:
        List of QMP qcode strings for use with QMPClient.send_key()

    Example:
        >>> key_list(DBX_KEY.KBD_leftctrl, DBX_KEY.KBD_c)
        ['ctrl', 'c']
    """
    return [get_qcode(k) for k in keys]


def ctrl_key(key: DBX_KEY) -> list[str]:
    """
    Build Ctrl+key combination.

    Args:
        key: The key to combine with Ctrl

    Returns:
        List of qcodes for QMPClient.send_key()

    Example:
        >>> ctrl_key(DBX_KEY.KBD_c)
        ['ctrl', 'c']
    """
    return ["ctrl", get_qcode(key)]


def alt_key(key: DBX_KEY) -> list[str]:
    """
    Build Alt+key combination.

    Args:
        key: The key to combine with Alt

    Returns:
        List of qcodes for QMPClient.send_key()

    Example:
        >>> alt_key(DBX_KEY.KBD_f4)
        ['alt', 'f4']
    """
    return ["alt", get_qcode(key)]


def shift_key(key: DBX_KEY) -> list[str]:
    """
    Build Shift+key combination.

    Args:
        key: The key to combine with Shift

    Returns:
        List of qcodes for QMPClient.send_key()
    """
    return ["shift", get_qcode(key)]


def ctrl_alt_key(key: DBX_KEY) -> list[str]:
    """
    Build Ctrl+Alt+key combination.

    Args:
        key: The key to combine with Ctrl+Alt

    Returns:
        List of qcodes for QMPClient.send_key()

    Example:
        >>> ctrl_alt_key(DBX_KEY.KBD_delete)
        ['ctrl', 'alt', 'delete']
    """
    return ["ctrl", "alt", get_qcode(key)]


def ctrl_shift_key(key: DBX_KEY) -> list[str]:
    """
    Build Ctrl+Shift+key combination.

    Args:
        key: The key to combine with Ctrl+Shift

    Returns:
        List of qcodes for QMPClient.send_key()
    """
    return ["ctrl", "shift", get_qcode(key)]


# Convenience constants for common keys
ENTER = ["ret"]
ESCAPE = ["esc"]
TAB = ["tab"]
BACKSPACE = ["backspace"]
SPACE = ["spc"]
DELETE = ["delete"]
INSERT = ["insert"]
HOME = ["home"]
END = ["end"]
PAGE_UP = ["pgup"]
PAGE_DOWN = ["pgdn"]
UP = ["up"]
DOWN = ["down"]
LEFT = ["left"]
RIGHT = ["right"]

# Common key combinations
CTRL_C = ["ctrl", "c"]
CTRL_V = ["ctrl", "v"]
CTRL_X = ["ctrl", "x"]
CTRL_Z = ["ctrl", "z"]
CTRL_A = ["ctrl", "a"]
CTRL_S = ["ctrl", "s"]
CTRL_ALT_DEL = ["ctrl", "alt", "delete"]
ALT_F4 = ["alt", "f4"]
ALT_TAB = ["alt", "tab"]


def function_key(n: int) -> list[str]:
    """
    Get function key qcode.

    Args:
        n: Function key number (1-24)

    Returns:
        List with single qcode for QMPClient.send_key()

    Example:
        >>> function_key(1)
        ['f1']
    """
    if not 1 <= n <= 24:
        raise ValueError(f"Function key must be 1-24, got {n}")
    return [f"f{n}"]


def digit_key(n: int) -> list[str]:
    """
    Get digit key qcode.

    Args:
        n: Digit (0-9)

    Returns:
        List with single qcode for QMPClient.send_key()

    Example:
        >>> digit_key(5)
        ['5']
    """
    if not 0 <= n <= 9:
        raise ValueError(f"Digit must be 0-9, got {n}")
    return [str(n)]


def number_keys(num: int) -> list[list[str]]:
    """
    Get key sequences to type a number.

    Args:
        num: Number to type

    Returns:
        List of key lists, each to be passed to QMPClient.send_key()

    Example:
        >>> number_keys(42)
        [['4'], ['2']]
    """
    return [[d] for d in str(num)]
