"""
HTML generation utilities for DOS video analysis.
"""

# VGA color palette (RGB values)
VGA_COLORS = [
    "#000000",  # 0: Black
    "#0000AA",  # 1: Blue
    "#00AA00",  # 2: Green
    "#00AAAA",  # 3: Cyan
    "#AA0000",  # 4: Red
    "#AA00AA",  # 5: Magenta
    "#AA5500",  # 6: Brown
    "#AAAAAA",  # 7: Light Gray
    "#555555",  # 8: Dark Gray
    "#5555FF",  # 9: Light Blue
    "#55FF55",  # 10: Light Green
    "#55FFFF",  # 11: Light Cyan
    "#FF5555",  # 12: Light Red
    "#FF55FF",  # 13: Light Magenta
    "#FFFF55",  # 14: Yellow
    "#FFFFFF",  # 15: White
]

VGA_COLOR_NAMES = [
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

# CP437 (DOS) to Unicode mapping for extended characters
CP437_MAP = {
    # Box drawing characters (single line)
    179: "│",
    180: "┤",
    191: "┐",
    192: "└",
    193: "┴",
    194: "┬",
    195: "├",
    196: "─",
    197: "┼",
    217: "┘",
    218: "┌",
    # Box drawing characters (double line)
    186: "║",
    187: "╗",
    188: "╝",
    200: "╚",
    201: "╔",
    202: "╩",
    203: "╦",
    204: "╠",
    205: "═",
    206: "╬",
    # Mixed single/double box drawing
    181: "╡",
    182: "╢",
    183: "╖",
    184: "╕",
    185: "╣",
    198: "╞",
    199: "╟",
    207: "╧",
    208: "╨",
    209: "╤",
    210: "╥",
    211: "╙",
    212: "╘",
    213: "╒",
    214: "╓",
    215: "╫",
    216: "╪",
    # Block elements
    176: "░",
    177: "▒",
    178: "▓",
    219: "█",
    220: "▄",
    221: "▌",
    222: "▐",
    223: "▀",
    # Special characters
    1: "☺",
    2: "☻",
    3: "♥",
    4: "♦",
    5: "♣",
    6: "♠",
    7: "•",
    8: "◘",
    9: "○",
    10: "◙",
    11: "♂",
    12: "♀",
    13: "♪",
    14: "♫",
    15: "☼",
    16: "►",
    17: "◄",
    18: "↕",
    19: "‼",
    20: "¶",
    21: "§",
    22: "▬",
    23: "↨",
    24: "↑",
    25: "↓",
    26: "→",
    27: "←",
    28: "∟",
    29: "↔",
    30: "▲",
    31: "▼",
    # Extended ASCII characters
    128: "Ç",
    129: "ü",
    130: "é",
    131: "â",
    132: "ä",
    133: "à",
    134: "å",
    135: "ç",
    136: "ê",
    137: "ë",
    138: "è",
    139: "ï",
    140: "î",
    141: "ì",
    142: "Ä",
    143: "Å",
    144: "É",
    145: "æ",
    146: "Æ",
    147: "ô",
    148: "ö",
    149: "ò",
    150: "û",
    151: "ù",
    152: "ÿ",
    153: "Ö",
    154: "Ü",
    155: "¢",
    156: "£",
    157: "¥",
    158: "₧",
    159: "ƒ",
    160: "á",
    161: "í",
    162: "ó",
    163: "ú",
    164: "ñ",
    165: "Ñ",
    166: "ª",
    167: "º",
    168: "¿",
    169: "⌐",
    170: "¬",
    171: "½",
    172: "¼",
    173: "¡",
    174: "«",
    175: "»",
    224: "α",
    225: "ß",
    226: "Γ",
    227: "π",
    228: "Σ",
    229: "σ",
    230: "µ",
    231: "τ",
    232: "Φ",
    233: "Θ",
    234: "Ω",
    235: "δ",
    236: "∞",
    237: "φ",
    238: "ε",
    239: "∩",
    240: "≡",
    241: "±",
    242: "≥",
    243: "≤",
    244: "⌠",
    245: "⌡",
    246: "÷",
    247: "≈",
    248: "°",
    249: "∙",
    250: "·",
    251: "√",
    252: "ⁿ",
    253: "²",
    254: "■",
    255: " ",
}


def char_to_html(char_code: int) -> str:
    """Convert DOS character code to HTML-safe string with CP437 mapping."""
    if char_code == 0:
        return "&nbsp;"
    elif char_code in CP437_MAP:
        return CP437_MAP[char_code]
    elif char_code == 32:
        return "&nbsp;"
    elif char_code < 32:
        return "□"
    elif char_code < 127:
        char = chr(char_code)
        if char in ["<", ">", "&", '"', "'"]:
            return {
                "<": "&lt;",
                ">": "&gt;",
                "&": "&amp;",
                '"': "&quot;",
                "'": "&#39;",
            }[char]
        return char
    else:
        return f"&#{char_code};"


def analyze_dos_video_colors(video_pages: list[bytes], cols: int = 80, rows: int = 25) -> dict:
    """
    Analyze all foreground and background colors used across multiple DOS video pages.

    Args:
        video_pages: list of bytes objects, each containing character/attribute pairs
        cols: number of columns per page (default 80)
        rows: number of rows per page (default 25)

    Returns:
        dict containing color analysis results
    """
    fg_colors: set[int] = set()
    bg_colors: set[int] = set()
    color_combinations: set[tuple[int, int]] = set()
    blink_used = False

    min_col = cols
    max_col = -1
    min_row = rows
    max_row = -1
    first_char_pos: tuple[int, int, int] | None = None
    last_char_pos: tuple[int, int, int] | None = None

    total_cells = 0
    content_cells = 0
    fg_color_counts = {i: 0 for i in range(16)}
    bg_color_counts = {i: 0 for i in range(16)}
    combination_counts: dict[str, int] = {}
    page_stats = []

    for page_idx, video_data in enumerate(video_pages):
        if not video_data:
            page_stats.append(
                {"page": page_idx, "cells": 0, "fg_colors": 0, "bg_colors": 0, "combinations": 0}
            )
            continue

        page_fg_colors: set[int] = set()
        page_bg_colors: set[int] = set()
        page_combinations: set[tuple[int, int]] = set()
        page_cells = 0
        page_content_cells = 0
        page_min_col = cols
        page_max_col = -1
        page_min_row = rows
        page_max_row = -1
        page_first_char: tuple[int, int] | None = None
        page_last_char: tuple[int, int] | None = None

        for row in range(rows):
            for col in range(cols):
                offset = (row * cols + col) * 2

                if offset + 1 < len(video_data):
                    char_code = video_data[offset]
                    attr_byte = video_data[offset + 1]
                else:
                    continue

                foreground = attr_byte & 0x0F
                background = (attr_byte & 0x70) >> 4
                blink = (attr_byte & 0x80) != 0

                is_content = char_code != 0 and char_code != 32

                if is_content:
                    content_cells += 1
                    page_content_cells += 1

                    min_col = min(min_col, col)
                    max_col = max(max_col, col)
                    min_row = min(min_row, row)
                    max_row = max(max_row, row)

                    page_min_col = min(page_min_col, col)
                    page_max_col = max(page_max_col, col)
                    page_min_row = min(page_min_row, row)
                    page_max_row = max(page_max_row, row)

                    char_pos = (page_idx, row, col)
                    if first_char_pos is None:
                        first_char_pos = char_pos
                    if page_first_char is None:
                        page_first_char = (row, col)

                    last_char_pos = char_pos
                    page_last_char = (row, col)

                fg_colors.add(foreground)
                bg_colors.add(background)
                color_combinations.add((foreground, background))

                page_fg_colors.add(foreground)
                page_bg_colors.add(background)
                page_combinations.add((foreground, background))

                fg_color_counts[foreground] += 1
                bg_color_counts[background] += 1

                combination_key = f"{foreground}:{background}"
                combination_counts[combination_key] = combination_counts.get(combination_key, 0) + 1

                if blink:
                    blink_used = True

                total_cells += 1
                page_cells += 1

        page_stats.append(
            {
                "page": page_idx,
                "cells": page_cells,
                "content_cells": page_content_cells,
                "fg_colors": len(page_fg_colors),
                "bg_colors": len(page_bg_colors),
                "combinations": len(page_combinations),
                "fg_color_list": sorted(page_fg_colors),
                "bg_color_list": sorted(page_bg_colors),
                "bounds": {
                    "min_col": page_min_col if page_content_cells > 0 else None,
                    "max_col": page_max_col if page_content_cells > 0 else None,
                    "min_row": page_min_row if page_content_cells > 0 else None,
                    "max_row": page_max_row if page_content_cells > 0 else None,
                    "width": (page_max_col - page_min_col + 1) if page_content_cells > 0 else 0,
                    "height": (page_max_row - page_min_row + 1) if page_content_cells > 0 else 0,
                    "first_char": page_first_char,
                    "last_char": page_last_char,
                },
            }
        )

    sorted_combinations = sorted(combination_counts.items(), key=lambda x: x[1], reverse=True)

    fg_color_info = []
    for color_id in sorted(fg_colors):
        fg_color_info.append(
            {
                "id": color_id,
                "name": VGA_COLOR_NAMES[color_id],
                "hex": VGA_COLORS[color_id],
                "count": fg_color_counts[color_id],
                "percentage": (fg_color_counts[color_id] / total_cells * 100)
                if total_cells > 0
                else 0,
            }
        )

    bg_color_info = []
    for color_id in sorted(bg_colors):
        bg_color_info.append(
            {
                "id": color_id,
                "name": VGA_COLOR_NAMES[color_id],
                "hex": VGA_COLORS[color_id],
                "count": bg_color_counts[color_id],
                "percentage": (bg_color_counts[color_id] / total_cells * 100)
                if total_cells > 0
                else 0,
            }
        )

    combination_info = []
    for combo_key, count in sorted_combinations:
        fg_id, bg_id = map(int, combo_key.split(":"))
        combination_info.append(
            {
                "foreground": {
                    "id": fg_id,
                    "name": VGA_COLOR_NAMES[fg_id],
                    "hex": VGA_COLORS[fg_id],
                },
                "background": {
                    "id": bg_id,
                    "name": VGA_COLOR_NAMES[bg_id],
                    "hex": VGA_COLORS[bg_id],
                },
                "count": count,
                "percentage": (count / total_cells * 100) if total_cells > 0 else 0,
            }
        )

    return {
        "summary": {
            "total_pages": len(video_pages),
            "total_cells": total_cells,
            "content_cells": content_cells,
            "unique_fg_colors": len(fg_colors),
            "unique_bg_colors": len(bg_colors),
            "unique_combinations": len(color_combinations),
            "blink_used": blink_used,
        },
        "content_bounds": {
            "global": {
                "min_col": min_col if content_cells > 0 else None,
                "max_col": max_col if content_cells > 0 else None,
                "min_row": min_row if content_cells > 0 else None,
                "max_row": max_row if content_cells > 0 else None,
                "width": (max_col - min_col + 1) if content_cells > 0 else 0,
                "height": (max_row - min_row + 1) if content_cells > 0 else 0,
                "first_char": first_char_pos,
                "last_char": last_char_pos,
            }
        },
        "foreground_colors": fg_color_info,
        "background_colors": bg_color_info,
        "color_combinations": combination_info,
        "page_statistics": page_stats,
        "raw_data": {
            "fg_color_ids": sorted(fg_colors),
            "bg_color_ids": sorted(bg_colors),
            "fg_counts": fg_color_counts,
            "bg_counts": bg_color_counts,
            "combination_counts": combination_counts,
        },
    }


def dos_video_to_html(video_data: bytes, cols: int = 80, rows: int = 25) -> str:
    """
    Convert DOS video page data to HTML with colors and interactive tooltips.

    Args:
        video_data: bytes object containing character/attribute pairs
        cols: number of columns (default 80)
        rows: number of rows (default 25)

    Returns:
        HTML string representing the video page
    """

    def decode_attribute(attr_byte: int) -> tuple[int, int, bool]:
        foreground = attr_byte & 0x0F
        background = (attr_byte & 0x70) >> 4
        blink = (attr_byte & 0x80) != 0
        return foreground, background, blink

    html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>DOS Video Page</title>
    <style>
        body {
            font-family: 'Consolas', 'Monaco', monospace;
            margin: 20px;
            background-color: #1a1a1a;
            color: #ccc;
        }
        .dos-screen {
            display: inline-block;
            background-color: #000;
            border: 3px solid #555;
            padding: 12px;
        }
        .dos-row {
            display: block;
            height: 16px;
            line-height: 16px;
            white-space: nowrap;
            font-size: 0;
        }
        .dos-char {
            display: inline-block;
            width: 9px;
            height: 16px;
            font-size: 14px;
            text-align: center;
        }
        .dos-char.blink {
            animation: blink 1s infinite;
        }
        @keyframes blink {
            0%, 50% { opacity: 1; }
            51%, 100% { opacity: 0; }
        }
    </style>
</head>
<body>
    <div class="dos-screen">
"""

    for row in range(rows):
        html += '        <div class="dos-row">'

        for col in range(cols):
            offset = (row * cols + col) * 2

            if offset + 1 < len(video_data):
                char_code = video_data[offset]
                attr_byte = video_data[offset + 1]
            else:
                char_code = 0
                attr_byte = 0x07

            fg, bg, blink = decode_attribute(attr_byte)
            fg_color = VGA_COLORS[fg]
            bg_color = VGA_COLORS[bg]
            char_html = char_to_html(char_code)

            css_class = "dos-char"
            if blink:
                css_class += " blink"

            style = f"color: {fg_color}; background-color: {bg_color};"
            html += f'<span class="{css_class}" style="{style}">{char_html}</span>'

        html += "</div>\n"

    html += """    </div>
</body>
</html>"""

    return html


def save_dos_video_html(
    video_data: bytes, filename: str = "dos_video.html", cols: int = 80, rows: int = 25
) -> None:
    """Save DOS video data as HTML file."""
    html = dos_video_to_html(video_data, cols, rows)
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
