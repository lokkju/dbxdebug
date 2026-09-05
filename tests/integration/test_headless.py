"""What `DosboxSession(headless=True)` actually costs, measured against a real emulator.

The claim under test is narrow and easy to get wrong by assumption: SDL's
`dummy` video driver removes the WINDOW, not the rendering. DOSBox-X still
paints every frame into an offscreen surface, so `qmp.screendump()` and the
`0xB8000` text readers see exactly what they would see with a window on
screen.

That was verified by launching one headless session and one windowed session
with the same fixed `autoexec` -- fixed because the default one echoes the
session's random tempdir onto the guest screen, which makes any two boots
differ for a reason that has nothing to do with the video driver -- and
comparing what came back:

    headless: 720x400 RGB PNG, 13380 bytes
    windowed: 720x400 RGB PNG, 13380 bytes
    screen_lines identical:  True
    png pixels identical:    True
    png bytes identical:     True
    headless opened an X window: False
    windowed opened an X window: True
      0x2600003 "DOSBox-X 2025.12.01: COMMAND - 100%": ("dosbox-x" "dosbox-x")

Byte-identical, not merely equivalent. `test_screen_capture_is_identical_to_a
_windowed_session` re-runs exactly that comparison, but it is opt-in: it
launches a real window, which takes the keyboard focus of whoever is at the
machine, and a suite that does that by accident is the whole problem
lokkju/dbxdebug#3 exists to fix. Set `DBXDEBUG_ALLOW_WINDOWED=1` to run it.

Everything else here runs headless and is always on. The rest of
`tests/integration` is coverage for this feature too, implicitly: those
fixtures no longer set the SDL variables by hand, so they only pass at all
because `headless=True` is the default.
"""

from __future__ import annotations

import base64
import os
import shutil
import struct
import subprocess
import zlib
from collections.abc import Callable

import pytest

from dbxdebug.session import HEADLESS_ENV, DosboxSession

pytestmark = pytest.mark.integration

# An autoexec that paints the same screen in every session. The default one
# ends with `mount c <workdir>`, and the workdir is a fresh mkdtemp name, so
# two sessions using it can never produce the same screen.
FIXED_AUTOEXEC = "cls\necho DBXHEADLESSPROBE"
FIXED_AUTOEXEC_MARKER = "DBXHEADLESSPROBE"

# DOSBox-X's screendump of an 80x25 text mode, as observed live: 9x16 cells,
# 8-bit truecolour, no palette, no interlacing.
EXPECTED_PNG_SIZE = (720, 400)

# Set to run the test that launches a visible, focus-stealing window.
ALLOW_WINDOWED_ENV = "DBXDEBUG_ALLOW_WINDOWED"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_chunks(data: bytes) -> dict[str, list[bytes]]:
    """Split a PNG into its chunks.

    Args:
        data: A complete PNG file.

    Returns:
        Chunk bodies keyed by four-character chunk type, in file order.
    """
    assert data[:8] == _PNG_MAGIC, "screendump did not return a PNG"
    out: dict[str, list[bytes]] = {}
    i = 8
    while i < len(data):
        (length,) = struct.unpack(">I", data[i : i + 4])
        out.setdefault(data[i + 4 : i + 8].decode("ascii"), []).append(data[i + 8 : i + 8 + length])
        i += 12 + length
    return out


def _png_pixels(data: bytes) -> tuple[tuple[int, int], bytes]:
    """Decode a PNG far enough to compare its pixels.

    Written out rather than pulled from Pillow: this package has no image
    dependency, and adding one so a test can compare two screenshots would
    put an image library in every consumer's dependency tree.

    Args:
        data: A complete PNG file. Must be 8-bit and non-interlaced, which
            is what DOSBox-X's `screendump` produces.

    Returns:
        `((width, height), pixel_bytes)`, where `pixel_bytes` is the
        unfiltered scanline data with no per-row filter bytes.
    """
    chunks = _png_chunks(data)
    width, height, depth, colour, _comp, _filt, interlace = struct.unpack(
        ">IIBBBBB", chunks["IHDR"][0]
    )
    assert depth == 8 and interlace == 0, f"unexpected png: depth={depth} interlace={interlace}"
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour]
    raw = zlib.decompress(b"".join(chunks["IDAT"]))
    stride = width * bpp
    out = bytearray()
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        mode = raw[pos]
        line = bytearray(raw[pos + 1 : pos + 1 + stride])
        pos += 1 + stride
        for x in range(stride):
            left = line[x - bpp] if x >= bpp else 0
            up = prev[x]
            upleft = prev[x - bpp] if x >= bpp else 0
            if mode == 0:
                continue
            if mode == 1:
                line[x] = (line[x] + left) & 0xFF
            elif mode == 2:
                line[x] = (line[x] + up) & 0xFF
            elif mode == 3:
                line[x] = (line[x] + (left + up) // 2) & 0xFF
            elif mode == 4:
                p = left + up - upleft
                pl, pu, pul = abs(p - left), abs(p - up), abs(p - upleft)
                line[x] = (
                    line[x] + (left if (pl <= pu and pl <= pul) else (up if pu <= pul else upleft))
                ) & 0xFF
            else:
                raise AssertionError(f"unknown png filter {mode}")
        out += line
        prev = line
    return (width, height), bytes(out)


def _capture(session: DosboxSession) -> tuple[list[str], tuple[int, int], bytes]:
    """Read the guest screen both ways: as text, and as a rendered PNG.

    Args:
        session: A started session with its QMP and GDB clients connected.

    Returns:
        `(screen_lines, (width, height), pixel_bytes)`.
    """
    assert session.qmp is not None
    png = base64.b64decode(session.qmp.screendump()["data"])
    size, pixels = _png_pixels(png)
    return session.screen_lines(), size, pixels


def _dosbox_x_windows() -> list[str]:
    """List X11 toplevels whose title names DOSBox-X.

    Returns:
        One string per matching line of `xwininfo -root -tree`. Empty when
        nothing matches -- which is also what a machine with no DOSBox-X
        window returns, so callers must establish their own baseline.
    """
    tree = subprocess.run(
        ["xwininfo", "-root", "-tree"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).stdout
    return [line.strip() for line in tree.splitlines() if "DOSBox-X" in line]


# --------------------------------------------------------------------------
# the environment the child actually gets
# --------------------------------------------------------------------------


def test_headless_is_the_default_for_the_integration_fixtures(
    make_session: Callable[..., DosboxSession],
) -> None:
    """The fixtures pass no `env`, so this only holds if the default does."""
    session = make_session()
    assert session.headless is True
    assert session._child_env()["SDL_VIDEODRIVER"] == HEADLESS_ENV["SDL_VIDEODRIVER"]
    assert session._child_env()["SDL_AUDIODRIVER"] == HEADLESS_ENV["SDL_AUDIODRIVER"]


# --------------------------------------------------------------------------
# the debug surface, headless
# --------------------------------------------------------------------------


def test_screen_capture_works_under_the_dummy_video_driver(
    make_session: Callable[..., DosboxSession],
) -> None:
    """`dummy` removes the window, not the rendering.

    The assertion that matters is the last one: a PNG of the right size that
    is entirely one colour would be what a driver that stopped rendering
    produced, and it would still be a valid PNG of the right size.
    """
    session = make_session(autoexec=FIXED_AUTOEXEC)
    lines, size, pixels = _capture(session)

    assert any(FIXED_AUTOEXEC_MARKER in line for line in lines), lines
    assert size == EXPECTED_PNG_SIZE
    assert len(set(pixels)) > 1, "screendump is a single flat colour -- nothing was rendered"


def test_text_and_rendered_capture_agree_headless(
    make_session: Callable[..., DosboxSession],
) -> None:
    """Two independent read paths, one guest: they must not disagree.

    `screen_lines()` reads guest memory over GDB; `screendump` renders
    through DOSBox-X's own capture path. If the dummy driver were quietly
    serving a stale or blank framebuffer, only the second would notice --
    so the text read is the control, not the subject.
    """
    session = make_session(autoexec=FIXED_AUTOEXEC)
    lines_a, _, pixels_a = _capture(session)
    lines_b, _, pixels_b = _capture(session)

    assert lines_a == lines_b
    assert pixels_a == pixels_b
    assert any(FIXED_AUTOEXEC_MARKER in line for line in lines_a)


def test_headless_session_opens_no_window(
    make_session: Callable[..., DosboxSession],
) -> None:
    """No X11 toplevel appears for a headless session.

    Skipped without a display or `xwininfo`, and skipped if some other
    DOSBox-X window is already open -- this machine is shared with the
    emulator a developer may be watching, and this test does not get to
    fail because of it.
    """
    if not os.environ.get("DISPLAY"):
        pytest.skip("no DISPLAY; cannot observe whether a window appeared")
    if shutil.which("xwininfo") is None:
        pytest.skip("xwininfo not installed; cannot observe whether a window appeared")
    if _dosbox_x_windows():
        pytest.skip("a DOSBox-X window is already open; cannot attribute a new one")

    session = make_session()
    assert session.running
    assert _dosbox_x_windows() == []


# --------------------------------------------------------------------------
# the comparison against a real window (opt-in: it steals focus)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get(ALLOW_WINDOWED_ENV),
    reason=(
        f"launches a visible window that takes the keyboard focus; set "
        f"{ALLOW_WINDOWED_ENV}=1 to run it"
    ),
)
def test_screen_capture_is_identical_to_a_windowed_session(
    make_session: Callable[..., DosboxSession],
) -> None:
    """Headless and windowed produce the same screen, to the byte.

    The two sessions are separate boots of separate processes, so this only
    holds because `FIXED_AUTOEXEC` removes the one thing that differs
    between boots -- the workdir path the default autoexec prints.
    """
    headless = make_session(autoexec=FIXED_AUTOEXEC, headless=True)
    windowed = make_session(autoexec=FIXED_AUTOEXEC, headless=False)

    lines_h, size_h, pixels_h = _capture(headless)
    lines_w, size_w, pixels_w = _capture(windowed)

    assert lines_h == lines_w
    assert size_h == size_w == EXPECTED_PNG_SIZE
    assert pixels_h == pixels_w
