"""Tests for `dbxdebug.paths`: the single source of truth for locating dosbox-x.

These are the regression tests for the bug this module exists to fix:
`session.py` and `doctor.py` used to resolve the emulator binary two
different ways and could disagree about whether one existed. Every test
here runs with no real emulator and never touches the real `~/.cache` or
`~/projects/eesystem/dosbox-x` -- `shutil.which` and the conventional path
are both faked via `monkeypatch` and `tmp_path`.
"""

import dbxdebug.doctor as doctor_module
import dbxdebug.session as session_module
from dbxdebug.paths import configured_dosbox_x_path, find_dosbox_x


def _make_binary(path):
    path.write_bytes(b"not a real emulator, just needs to exist")
    path.chmod(0o755)
    return path


# --------------------------------------------------------------------------
# resolution order
# --------------------------------------------------------------------------


def test_env_var_wins_over_everything(tmp_path, monkeypatch):
    """DBXDEBUG_DOSBOX pointing at an existing file wins over the conventional
    path and PATH, even when both of those would also resolve to something.
    """
    env_binary = _make_binary(tmp_path / "env-dosbox-x")
    conventional_binary = _make_binary(tmp_path / "conventional-dosbox-x")
    monkeypatch.setattr("dbxdebug.paths.DEFAULT_DOSBOX_X_PATH", str(conventional_binary))
    monkeypatch.setattr("shutil.which", lambda _name: str(tmp_path / "path-dosbox-x"))
    monkeypatch.setenv("DBXDEBUG_DOSBOX", str(env_binary))

    assert find_dosbox_x() == env_binary
    assert configured_dosbox_x_path() == env_binary


def test_env_var_set_to_missing_file_is_not_found_and_not_replaced(tmp_path, monkeypatch):
    """An explicit DBXDEBUG_DOSBOX that does not exist is trusted as-is by
    `configured_dosbox_x_path` and reported absent by `find_dosbox_x` --
    neither silently substitutes a PATH binary the user did not ask for.
    """
    missing = tmp_path / "no-such-dosbox-x"
    path_binary = _make_binary(tmp_path / "path-dosbox-x")
    monkeypatch.setattr("shutil.which", lambda _name: str(path_binary))
    monkeypatch.setenv("DBXDEBUG_DOSBOX", str(missing))

    assert find_dosbox_x() is None
    assert configured_dosbox_x_path() == missing


def test_path_fallback_when_env_unset_and_conventional_absent(tmp_path, monkeypatch):
    """With no override and no conventional-path binary, a PATH binary is found."""
    path_binary = _make_binary(tmp_path / "path-dosbox-x")
    monkeypatch.delenv("DBXDEBUG_DOSBOX", raising=False)
    monkeypatch.setattr(
        "dbxdebug.paths.DEFAULT_DOSBOX_X_PATH", str(tmp_path / "no-conventional-binary")
    )
    monkeypatch.setattr("shutil.which", lambda _name: str(path_binary))

    assert find_dosbox_x() == path_binary
    assert configured_dosbox_x_path() == path_binary


def test_find_returns_none_when_nothing_found_anywhere(tmp_path, monkeypatch):
    """No env override, no conventional binary, nothing on PATH: None, no raise."""
    monkeypatch.delenv("DBXDEBUG_DOSBOX", raising=False)
    monkeypatch.setattr(
        "dbxdebug.paths.DEFAULT_DOSBOX_X_PATH", str(tmp_path / "no-conventional-binary")
    )
    monkeypatch.setattr("shutil.which", lambda _name: None)

    assert find_dosbox_x() is None
    # configured_dosbox_x_path always returns *something* to attempt.
    assert configured_dosbox_x_path() == tmp_path / "no-conventional-binary"


def test_env_var_is_read_fresh_on_every_call(tmp_path, monkeypatch):
    """Changing DBXDEBUG_DOSBOX between calls changes the result -- nothing caches
    the value at import time.
    """
    first = _make_binary(tmp_path / "first-dosbox-x")
    second = _make_binary(tmp_path / "second-dosbox-x")

    monkeypatch.setenv("DBXDEBUG_DOSBOX", str(first))
    assert find_dosbox_x() == first
    assert configured_dosbox_x_path() == first

    monkeypatch.setenv("DBXDEBUG_DOSBOX", str(second))
    assert find_dosbox_x() == second
    assert configured_dosbox_x_path() == second


# --------------------------------------------------------------------------
# the actual regression: session and doctor must agree
# --------------------------------------------------------------------------


def test_session_default_executable_and_doctor_agree_when_only_path_has_the_binary(
    tmp_path, monkeypatch
):
    """The bug this task fixes: `dosbox-x` on PATH but not at the conventional
    checkout location used to make `doctor` report the emulator healthy while
    `DosboxSession`'s default executable pointed at a path that did not exist.

    Both must now resolve to the exact same binary under the exact same
    environment.
    """
    path_binary = _make_binary(tmp_path / "path-dosbox-x")
    monkeypatch.delenv("DBXDEBUG_DOSBOX", raising=False)
    monkeypatch.setattr(
        "dbxdebug.paths.DEFAULT_DOSBOX_X_PATH", str(tmp_path / "no-conventional-binary")
    )
    monkeypatch.setattr("shutil.which", lambda _name: str(path_binary))

    session_default = session_module.DosboxSession().executable
    doctor_found = doctor_module.find_dosbox_x()

    assert session_default == path_binary
    assert doctor_found == path_binary
    assert session_default == doctor_found


def test_session_default_executable_and_doctor_agree_when_nothing_is_found(tmp_path, monkeypatch):
    """When neither an env override, a conventional binary, nor a PATH binary
    exists, `doctor` reports None; the session's default executable is a
    non-existent path, so a real launch fails loudly instead of silently
    picking something up. Both still agree on which path that is.
    """
    monkeypatch.delenv("DBXDEBUG_DOSBOX", raising=False)
    conventional = tmp_path / "no-conventional-binary"
    monkeypatch.setattr("dbxdebug.paths.DEFAULT_DOSBOX_X_PATH", str(conventional))
    monkeypatch.setattr("shutil.which", lambda _name: None)

    session_default = session_module.DosboxSession().executable
    doctor_found = doctor_module.find_dosbox_x()

    assert doctor_found is None
    assert session_default == conventional
