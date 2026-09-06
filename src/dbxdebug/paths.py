"""Single source of truth for locating the DOSBox-X emulator binary.

WHY THIS EXISTS. `session.py` and `doctor.py` both need to answer "where is
`dosbox-x`", and before this module existed they answered it two different
ways: `session.py` resolved `DBXDEBUG_DOSBOX` or a hardcoded conventional
path with no existence check and no `PATH` fallback, while `doctor.py`
additionally checked existence and fell back to `shutil.which`. On a host
where `dosbox-x` lived on `PATH` but not at the conventional checkout
location, `doctor` reported the emulator healthy while every real
`DosboxSession` launch failed outright -- a doctor that reports a healthy
state the library cannot actually use is worse than one that reports
nothing. Both callers now resolve through the exact same order defined
here, so "doctor says found" and "a launch can actually find it" can never
disagree again.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

__all__ = [
    "DEFAULT_DOSBOX_X_PATH",
    "DOSBOX_X_ENV_VAR",
    "configured_dosbox_x_path",
    "find_dosbox_x",
]

# The only definition of the conventional checkout path in this package.
# Used when DBXDEBUG_DOSBOX is unset and nothing is found on PATH either.
DEFAULT_DOSBOX_X_PATH = str(Path.home() / "projects/eesystem/dosbox-x/src/dosbox-x")

# Named once so a typo in the variable name can never make the two callers
# read different environment variables.
DOSBOX_X_ENV_VAR = "DBXDEBUG_DOSBOX"


def configured_dosbox_x_path() -> Path:
    """Resolve the emulator path a launch should attempt, read fresh each call.

    This is the "path to try" half of resolution -- see `find_dosbox_x` for
    the "does a usable binary actually exist" half. Checked in order: the
    `DBXDEBUG_DOSBOX` environment variable, the conventional checkout path,
    then `PATH`.

    Returns:
        The path a launch should attempt. When `DBXDEBUG_DOSBOX` is set,
        that value is returned exactly as given, whether or not it exists
        -- an explicit override the user typed is trusted and surfaced as a
        clear failure when a launch is attempted against it, never
        silently swapped out for a different binary found on `PATH`.
        Otherwise: the conventional path if a file exists there; failing
        that, a `PATH` binary if `shutil.which` finds one; failing that,
        the conventional path anyway, so a caller always has some path to
        attempt and a clear "does not exist" error to raise on failure.
    """
    env_path = os.environ.get(DOSBOX_X_ENV_VAR)
    if env_path:
        return Path(env_path)
    conventional = Path(DEFAULT_DOSBOX_X_PATH)
    if conventional.is_file():
        return conventional
    found = shutil.which("dosbox-x")
    return Path(found) if found else conventional


def find_dosbox_x() -> Path | None:
    """Locate a `dosbox-x` binary that actually exists, in the same order.

    Checked in order: the `DBXDEBUG_DOSBOX` environment variable, the
    conventional checkout path, then `PATH`.

    Returns:
        The resolved path if a candidate file exists, else None. When
        `DBXDEBUG_DOSBOX` is set but names a file that does not exist, that
        explicit override is trusted and the search stops there rather
        than silently falling through to a different binary.
    """
    env_path = os.environ.get(DOSBOX_X_ENV_VAR)
    if env_path:
        candidate = Path(env_path)
        return candidate if candidate.is_file() else None
    conventional = Path(DEFAULT_DOSBOX_X_PATH)
    if conventional.is_file():
        return conventional
    found = shutil.which("dosbox-x")
    return Path(found) if found else None
