"""Fast, read-only readiness checks for the DOSBox-X remote-debug host.

`doctor` answers "can a session actually start here" without starting one:
finding the emulator binary, checking whether it looks like a fork build
with remote debugging compiled in, counting CPUs as a rough concurrency
ceiling, checking the registry directory is writable, and flagging any
session the registry already thinks is orphaned. It never launches
DOSBox-X -- `session.py` measures whether a launch actually WORKS; this
only checks whether attempting one is likely to succeed.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .registry import RegisteredSession, list_sessions, registry_dir

# Overridable via DBXDEBUG_DOSBOX, the same variable session.py reads --
# kept in sync here so `doctor` reports on the exact binary a real launch
# would use.
_DEFAULT_DOSBOX_X = str(Path.home() / "projects/eesystem/dosbox-x/src/dosbox-x")

# Strings this project's DOSBox-X fork embeds for its added gdbserver and
# qmpserver remote-debug servers (see `dosbox.cpp`'s `[dosbox]` section
# properties). Their presence in the binary is a cheap, static proxy for
# "built from the fork with remote debugging compiled in" -- checking for
# them avoids ever having to launch the emulator just to find out.
_REMOTE_DEBUG_MARKERS = (b"gdbserver port", b"qmpserver port")

# Name a probe file so a writability check never collides with a real
# registry entry (those are all named after a session's launch details).
_WRITE_PROBE_NAME = ".dbxdebug-doctor-write-probe"


def find_dosbox_x() -> Path | None:
    """Locate a `dosbox-x` binary the way `session.py` would launch it.

    Checked in order: the `DBXDEBUG_DOSBOX` environment variable, the
    conventional checkout path `~/projects/eesystem/dosbox-x/src/dosbox-x`,
    then `PATH`.

    Returns:
        The resolved path if a candidate file exists, else None. When
        `DBXDEBUG_DOSBOX` is set but names a file that does not exist, that
        explicit override is trusted and the search stops there rather
        than silently falling through to a different binary.
    """
    env_path = os.environ.get("DBXDEBUG_DOSBOX")
    if env_path:
        candidate = Path(env_path)
        return candidate if candidate.is_file() else None
    conventional = Path(_DEFAULT_DOSBOX_X)
    if conventional.is_file():
        return conventional
    found = shutil.which("dosbox-x")
    return Path(found) if found else None


def has_remote_debug(binary: Path) -> bool | None:
    """Check whether `binary` appears to have remote debugging compiled in.

    A static, read-only check: it scans the executable's bytes for strings
    the fork's gdbserver/qmpserver code embeds, and never runs the binary.
    "Appears to" is deliberate -- this is a heuristic proxy for a compiled-in
    feature, not proof the servers actually work.

    Args:
        binary: Path to a `dosbox-x` executable.

    Returns:
        True if every marker string was found, False if the file was read
        but no marker was found (likely a stock, non-forked build), or None
        if the file could not be read at all.
    """
    try:
        data = binary.read_bytes()
    except OSError:
        return None
    return all(marker in data for marker in _REMOTE_DEBUG_MARKERS)


def registry_is_writable(directory: Path) -> bool:
    """Probe whether `directory` accepts a file write, leaving nothing behind.

    Args:
        directory: Directory to probe.

    Returns:
        True if a temporary file could be created and removed inside
        `directory`.
    """
    probe = directory / _WRITE_PROBE_NAME
    try:
        probe.write_text("")
        probe.unlink()
        return True
    except OSError:
        return False


@dataclass
class DoctorReport:
    """The collected results of one `doctor.run()` pass."""

    dosbox_x: Path | None
    remote_debug: bool | None
    cpu_count: int | None
    registry_path: Path
    registry_writable: bool
    orphans: list[RegisteredSession] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether every check found a fully ready host.

        A caller that only wants a yes/no verdict can use this; `render()`
        below always prints the detail regardless of it.
        """
        return bool(
            self.dosbox_x and self.remote_debug and self.registry_writable and not self.orphans
        )

    def render(self) -> str:
        """Format the report as human-readable, aligned status lines.

        Returns:
            A multi-line string suitable for printing directly to a
            terminal.
        """
        lines: list[str] = []
        if self.dosbox_x is not None:
            lines.append(f"[ok]   dosbox-x binary found: {self.dosbox_x}")
            if self.remote_debug is True:
                lines.append(
                    "[ok]   remote debugging: appears compiled in "
                    "(gdbserver/qmpserver strings found)"
                )
            elif self.remote_debug is False:
                lines.append(
                    "[fail] remote debugging: no gdbserver/qmpserver strings found "
                    "-- this looks like a stock (non-forked) build"
                )
            else:
                lines.append("[warn] remote debugging: could not read the binary to check")
        else:
            lines.append(
                "[fail] dosbox-x binary: not found (checked $DBXDEBUG_DOSBOX, "
                f"{_DEFAULT_DOSBOX_X}, and $PATH)"
            )

        if self.cpu_count:
            lines.append(f"[info] host CPUs: {self.cpu_count} (rough concurrency ceiling)")
        else:
            lines.append("[warn] host CPUs: could not be determined")

        if self.registry_writable:
            lines.append(f"[ok]   registry dir writable: {self.registry_path}")
        else:
            lines.append(f"[fail] registry dir NOT writable: {self.registry_path}")

        if self.orphans:
            lines.append(f"[warn] orphaned sessions: {len(self.orphans)}")
            for orphan in self.orphans:
                lines.append(f"         pid={orphan.pid} workdir={orphan.workdir}")
        else:
            lines.append("[ok]   orphaned sessions: none")

        return "\n".join(lines)


def run(registry: Path | None = None) -> DoctorReport:
    """Run every readiness check and collect the results.

    Never starts an emulator: every check here is a filesystem probe, a
    static read of the binary's bytes, or a read of the existing session
    registry.

    Args:
        registry: Registry directory to check, or None for the default
            (`DBXDEBUG_REGISTRY`, or `~/.cache/dbxdebug-sessions`).

    Returns:
        The completed `DoctorReport`.
    """
    binary = find_dosbox_x()
    remote_debug = has_remote_debug(binary) if binary is not None else None
    registry_path = registry_dir(registry)
    orphans = [sess for sess in list_sessions(registry) if sess.orphaned]
    return DoctorReport(
        dosbox_x=binary,
        remote_debug=remote_debug,
        cpu_count=os.cpu_count(),
        registry_path=registry_path,
        registry_writable=registry_is_writable(registry_path),
        orphans=orphans,
    )
