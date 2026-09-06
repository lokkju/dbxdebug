"""The package root's export surface, pinned so a refactor cannot move it.

The rule these tests enforce is stated once, in `dbxdebug/__init__.py`:

    every module declares its supported surface in `__all__`, and the
    package root re-exports the union of the LIBRARY modules' `__all__`.
    `cli`, `registry` and `doctor` -- the `dbxdebug` command's own
    machinery -- are not re-exported.

Two failures these tests exist to catch, both of which the surface had
before the rule was written (lokkju/dbxdebug#7):

  * a name added to a module and forgotten at the root, which is how the
    root came to export `GDBClient` but not the `GDBTimeoutError` you catch
    from it, and `ENTER` but not `UP`;
  * a documented entry point quietly moved or renamed, which breaks every
    consumer that followed the README rather than the source.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

import dbxdebug

# Re-exported at the package root. Adding a module here is a deliberate act:
# it puts every name in that module's `__all__` into the package namespace.
LIBRARY_MODULES = [
    "addressing",
    "capture_io",
    "dbx_kbd",
    "frames",
    "gdb",
    "html",
    "keyboard",
    "paths",
    "qmp",
    "session",
    "utils",
    "video",
]

# NOT re-exported: the `dbxdebug` command's machinery. `cli` additionally
# must stay off the library import path because it is the only module that
# imports `click`.
COMMAND_MODULES = ["cli", "doctor", "registry"]

# The surface the README, docs/migration.md and the skill tell a caller to
# import. Spelled out rather than derived, so that dropping a module from
# LIBRARY_MODULES still fails loudly here instead of silently shrinking the
# package.
DOCUMENTED_ENTRY_POINTS = [
    "DosboxSession",
    "DosboxLaunchError",
    "GDBClient",
    "GDBTimeoutError",
    "GDBDesyncError",
    "IncompatibleStubError",
    "REGISTER_NAMES",
    "QMPClient",
    "QMPError",
    "CpuNotStoppedError",
    "DOSVideoTools",
    "linear",
    "linear_pc",
    "parse_address",
    "bp_addr",
    "PackedAddressError",
    "walk_frames",
    "steps_out",
    "Frame",
    "FrameWalkError",
    "find_dosbox_x",
    "configured_dosbox_x_path",
    "ScreenRecorder",
    "CTRL_C",
]


def _module_all(name: str) -> list[str]:
    module = importlib.import_module(f"dbxdebug.{name}")
    assert hasattr(module, "__all__"), f"dbxdebug.{name} declares no __all__"
    return list(module.__all__)


@pytest.mark.parametrize("name", LIBRARY_MODULES + COMMAND_MODULES)
def test_every_module_but_the_cli_declares_its_own_surface(name):
    """`__all__` is where the export decision is made, next to the code."""
    if name == "cli":
        pytest.skip("cli is an entry point, not an importable surface")
    for exported in _module_all(name):
        module = importlib.import_module(f"dbxdebug.{name}")
        assert hasattr(module, exported), f"dbxdebug.{name}.__all__ names missing {exported}"


def test_root_all_is_exactly_the_union_of_the_library_modules():
    """The root is derived, not curated.

    A name added to a library module's `__all__` and forgotten here fails
    this test rather than becoming a gap someone notices years later.
    """
    union = {name for module in LIBRARY_MODULES for name in _module_all(module)}
    root = set(dbxdebug.__all__)
    assert root - union == set(), "at the root but in no library module's __all__"
    assert union - root == set(), "in a library module's __all__ but not at the root"


def test_root_all_has_no_duplicates():
    assert len(dbxdebug.__all__) == len(set(dbxdebug.__all__))


def test_every_name_in_root_all_is_actually_importable_and_is_the_module_object():
    """Identity, not just presence: a shadowing copy is still a bug."""
    for name in dbxdebug.__all__:
        assert hasattr(dbxdebug, name), f"dbxdebug.__all__ names missing {name}"
    for module in LIBRARY_MODULES:
        source = importlib.import_module(f"dbxdebug.{module}")
        for name in _module_all(module):
            assert getattr(dbxdebug, name) is getattr(source, name), (
                f"dbxdebug.{name} is not dbxdebug.{module}.{name}"
            )


@pytest.mark.parametrize("name", DOCUMENTED_ENTRY_POINTS)
def test_documented_entry_point_imports_from_the_package_root(name):
    """What the docs tell a caller to write must keep working."""
    assert hasattr(dbxdebug, name)
    assert name in dbxdebug.__all__


def test_command_module_names_are_not_at_the_root():
    """`run`, `reap` and `list_sessions` mean nothing unqualified.

    They stay reachable as `dbxdebug.registry.reap` and `dbxdebug.doctor.run`.

    Only names these modules OWN are checked. `doctor.__all__` re-exports
    `paths.find_dosbox_x`, which is at the root because `paths` is a library
    module -- that is the rule working, not a leak.
    """
    library = {name for module in LIBRARY_MODULES for name in _module_all(module)}
    for module in ["doctor", "registry"]:
        owned = [name for name in _module_all(module) if name not in library]
        assert owned, f"dbxdebug.{module} owns no names of its own"
        for name in owned:
            assert not hasattr(dbxdebug, name), (
                f"dbxdebug.{module}.{name} leaked to the package root"
            )


def test_command_modules_are_still_reachable_by_their_module_path():
    from dbxdebug import doctor, registry

    assert callable(doctor.run)
    assert callable(registry.list_sessions)


def test_importing_the_package_does_not_import_click():
    """`click` is a CLI dependency; the library path must stay free of it.

    Run in a subprocess: by the time this file executes, another test in the
    same session has usually imported `dbxdebug.cli` already, so checking
    `sys.modules` in-process would pass no matter what the root imports.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, dbxdebug; print('click' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False", (
        f"importing dbxdebug pulled in click: {result.stdout!r} {result.stderr!r}"
    )


def test_module_level_imports_still_work():
    """This change is additive. Nothing written against the old docs breaks."""
    from dbxdebug.addressing import linear
    from dbxdebug.frames import walk_frames
    from dbxdebug.paths import find_dosbox_x
    from dbxdebug.session import DosboxSession

    assert (linear, walk_frames, find_dosbox_x, DosboxSession) == (
        dbxdebug.linear,
        dbxdebug.walk_frames,
        dbxdebug.find_dosbox_x,
        dbxdebug.DosboxSession,
    )
