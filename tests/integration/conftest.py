"""Fixtures that launch, and always tear down, a real DOSBox-X emulator.

Everything in `tests/integration` needs an actual emulator, so the binary is
resolved through the package's own `paths.find_dosbox_x` -- never a hardcoded
path -- and every test in this directory skips when that returns None. A
machine with no emulator still runs the rest of the suite.

The sessions built here are the only ones these tests may launch: the factory
records each one before it is started and stops all of them in a `finally`,
so a test that fails, errors, or is interrupted mid-assertion still leaves no
emulator process and no scratch workdir behind.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from dbxdebug.paths import find_dosbox_x
from dbxdebug.session import DosboxSession

# `DosboxSession.label` prefixes the scratch workdir name, so every workdir
# these tests create is identifiable as theirs at a glance.
SESSION_LABEL = "dbxdebug_it"

# Wall seconds any single GDB packet exchange may take before the socket
# raises. `GDBClient` arms `gdb.DEFAULT_TIMEOUT` itself now, so this is an
# OVERRIDE these tests state explicitly rather than their only protection
# from a stub that accepts a command and never answers -- `c` with a
# breakpoint that never fires, say. Written out here so that changing the
# package's default cannot quietly change how long a wedged run takes.
GDB_SOCKET_TIMEOUT = 30.0


@pytest.fixture(scope="session")
def dosbox_binary() -> Path:
    """Locate the emulator the same way a real `DosboxSession` launch does.

    Returns:
        Path to a `dosbox-x` binary that exists, resolved by
        `dbxdebug.paths.find_dosbox_x` (which honours `$DBXDEBUG_DOSBOX`
        and falls back to `PATH`).

    Raises:
        Skipped: Via `pytest.skip` when no binary is found, which skips
            every test in this directory rather than failing them.
    """
    found = find_dosbox_x()
    if found is None:
        pytest.skip(
            "no dosbox-x binary found; build one or point $DBXDEBUG_DOSBOX at it "
            "to run the live integration tests"
        )
    return found


def build_session(binary: Path, **kwargs: Any) -> DosboxSession:
    """Build an UNSTARTED headless session against `binary`.

    Headless comes from `DosboxSession(headless=True)`, which is the default:
    without it the emulator opens a real window and takes the developer's
    keyboard focus mid-test-run. These fixtures no longer set the SDL
    variables by hand, so a regression in that default fails the live suite
    by stealing focus -- and `test_headless.py` fails it on purpose.

    Args:
        binary: The emulator to launch.
        **kwargs: Passed straight through to `DosboxSession`.

    Returns:
        A session that has not been started yet. Callers that start it are
        responsible for stopping it -- use `with`, or the `make_session`
        fixture, which does that for them.
    """
    return DosboxSession(executable=binary, label=SESSION_LABEL, **kwargs)


@pytest.fixture
def make_session(dosbox_binary: Path) -> Iterator[Callable[..., DosboxSession]]:
    """Yield a factory for started sessions, all torn down after the test.

    Args:
        dosbox_binary: The resolved emulator path.

    Yields:
        A callable taking `DosboxSession` keyword arguments and returning a
        started session with `GDB_SOCKET_TIMEOUT` armed over whatever the
        client's own default would have been. Each
        call launches its own emulator, so a test that wants two sessions
        gets two independent ones.
    """
    started: list[DosboxSession] = []

    def factory(**kwargs: Any) -> DosboxSession:
        session = build_session(dosbox_binary, **kwargs)
        # Recorded BEFORE start(), so a launch that raises part-way through
        # still gets its process and workdir cleaned up below.
        started.append(session)
        session.start()
        gdb = session.gdb
        if gdb is not None and gdb.sock is not None:
            gdb.sock.settimeout(GDB_SOCKET_TIMEOUT)
        return session

    try:
        yield factory
    finally:
        for session in reversed(started):
            session.stop()


@pytest.fixture
def session_builder(dosbox_binary: Path) -> Callable[..., DosboxSession]:
    """Return a factory for UNSTARTED headless sessions.

    For the one test that has to drive `DosboxSession.__enter__` /
    `__exit__` itself, because what it asserts on is exactly what leaving
    the `with` block does. Every other test should use `make_session`.

    Args:
        dosbox_binary: The resolved emulator path.

    Returns:
        A callable taking `DosboxSession` keyword arguments and returning
        an unstarted session. The caller owns its teardown -- use `with`.
    """

    def builder(**kwargs: Any) -> DosboxSession:
        return build_session(dosbox_binary, **kwargs)

    return builder
