"""QMP commands the client did not previously expose.

`qmp.cpp` dispatches thirteen named commands (plus an unadvertised `quit` /
`system_powerdown` branch); `QMPClient` wrapped roughly half of them before
this file. These tests drive the rest -- `memdump`, `screendump`,
`savestate`, `loadstate`, `stop`, `cont`, `system_reset`, `query-status`,
`debug-break-on-exec`, and `quit` -- against a fake socket, with no emulator
involved. Each test asserts the exact JSON sent and how the reply is
decoded.

`TestMouse` covers the other half of `input-send-event`. The key branch was
already wrapped; the `rel` and `btn` branches were not, so `mouse_move`,
`mouse_button` and `mouse_click` are pinned here too.
"""

import base64
import json

import pytest

from dbxdebug.qmp import QMPClient, QMPError


class FakeSocket:
    """A minimal stand-in for `socket.socket` that replays canned replies.

    Each entry in `replies` is one already-encoded QMP response line (bytes,
    including the trailing `\\r\\n`); `recv` hands them out one per call,
    which matches how `QMPClient._read_message` consumes the stream.
    """

    def __init__(self, replies: list[bytes]) -> None:
        self._replies = list(replies)
        self.sent: list[dict] = []

    def connect(self, address: tuple[str, int]) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        # Client always sends one JSON object per line.
        line = data.decode().strip()
        self.sent.append(json.loads(line))

    def setsockopt(self, level: int, optname: int, value: int) -> None:
        """No-op: the real client sets TCP_NODELAY on connect."""

    def recv(self, _bufsize: int) -> bytes:
        if not self._replies:
            return b""
        return self._replies.pop(0)

    def close(self) -> None:
        pass


def _line(obj: dict) -> bytes:
    """Encode `obj` as a newline-delimited QMP response."""
    return (json.dumps(obj) + "\r\n").encode()


def _connect(monkeypatch: pytest.MonkeyPatch, *extra_replies: dict) -> tuple[QMPClient, FakeSocket]:
    """Connect a `QMPClient` against a fake socket.

    Queues the standard greeting and `qmp_capabilities` handshake ahead of
    any `extra_replies` the test wants to supply for its own commands.
    """
    replies = [
        _line({"QMP": {"version": {}, "capabilities": []}}),
        _line({"return": {}}),  # reply to qmp_capabilities
    ]
    replies.extend(_line(r) for r in extra_replies)
    fake = FakeSocket(replies)
    monkeypatch.setattr("socket.socket", lambda *_args, **_kwargs: fake)
    client = QMPClient()
    fake.sent.clear()  # drop the qmp_capabilities handshake from the log
    return client, fake


class TestMemdump:
    """The bulk-read path: base64 decode with no `file`, path with one."""

    def test_sends_address_and_size(self, monkeypatch: pytest.MonkeyPatch):
        payload = b"\x01\x02\x03\x04"
        client, fake = _connect(
            monkeypatch,
            {"return": {"data": base64.b64encode(payload).decode(), "size": 4}},
        )
        client.memdump(0xB8000, 4)
        assert fake.sent == [{"execute": "memdump", "arguments": {"address": 0xB8000, "size": 4}}]

    def test_decodes_base64_to_bytes_when_no_file_given(self, monkeypatch: pytest.MonkeyPatch):
        payload = b"\xde\xad\xbe\xef"
        client, _fake = _connect(
            monkeypatch,
            {"return": {"data": base64.b64encode(payload).decode(), "size": 4}},
        )
        result = client.memdump(0x1000, 4)
        assert result == payload

    def test_returns_path_when_file_given(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(
            monkeypatch,
            {"return": {"file": "/tmp/dump.bin", "size": 4096}},
        )
        result = client.memdump(0x1000, 4096, file="/tmp/dump.bin")
        assert fake.sent == [
            {
                "execute": "memdump",
                "arguments": {"address": 0x1000, "size": 4096, "file": "/tmp/dump.bin"},
            }
        ]
        assert result == "/tmp/dump.bin"

    def test_refusal_while_running_raises_qmp_error(self, monkeypatch: pytest.MonkeyPatch):
        client, _fake = _connect(
            monkeypatch,
            {
                "error": {
                    "class": "GenericError",
                    "desc": "memdump requires the CPU to be stopped for debugging; "
                    "halt via GDB or QMP stop first",
                }
            },
        )
        with pytest.raises(QMPError, match="stopped for debugging"):
            client.memdump(0x1000, 4)


class TestScreendump:
    def test_no_file_omits_arguments_entirely(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(
            monkeypatch,
            {"return": {"data": "AAAA", "size": 3, "format": "png", "file": "/tmp/shot.png"}},
        )
        result = client.screendump()
        assert fake.sent == [{"execute": "screendump"}]
        assert result == {"data": "AAAA", "size": 3, "format": "png", "file": "/tmp/shot.png"}

    def test_file_argument_is_sent(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(
            monkeypatch,
            {"return": {"file": "/tmp/shot.png", "size": 3, "format": "png"}},
        )
        result = client.screendump(file="/tmp/shot.png")
        assert fake.sent == [{"execute": "screendump", "arguments": {"file": "/tmp/shot.png"}}]
        assert result == {"file": "/tmp/shot.png", "size": 3, "format": "png"}


class TestSavestateLoadstate:
    def test_savestate_sends_file_and_returns_reply(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(monkeypatch, {"return": {"file": "/tmp/state.sav"}})
        result = client.savestate("/tmp/state.sav")
        assert fake.sent == [{"execute": "savestate", "arguments": {"file": "/tmp/state.sav"}}]
        assert result == {"file": "/tmp/state.sav"}

    def test_loadstate_sends_file_and_returns_reply(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(monkeypatch, {"return": {"file": "/tmp/state.sav"}})
        result = client.loadstate("/tmp/state.sav")
        assert fake.sent == [{"execute": "loadstate", "arguments": {"file": "/tmp/state.sav"}}]
        assert result == {"file": "/tmp/state.sav"}

    def test_loadstate_missing_file_raises_qmp_error(self, monkeypatch: pytest.MonkeyPatch):
        client, _fake = _connect(
            monkeypatch,
            {"error": {"class": "GenericError", "desc": "State file not found: /tmp/nope.sav"}},
        )
        with pytest.raises(QMPError, match="not found"):
            client.loadstate("/tmp/nope.sav")


class TestStopCont:
    def test_stop_sends_no_arguments(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(monkeypatch, {"return": {}})
        result = client.stop()
        assert fake.sent == [{"execute": "stop"}]
        assert result == {}

    def test_cont_sends_no_arguments(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(monkeypatch, {"return": {}})
        result = client.cont()
        assert fake.sent == [{"execute": "cont"}]
        assert result == {}


class TestSystemReset:
    def test_default_is_full_reset(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(monkeypatch, {"return": {}})
        client.system_reset()
        assert fake.sent == [{"execute": "system_reset", "arguments": {"dos_only": False}}]

    def test_dos_only_reset(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(monkeypatch, {"return": {}})
        client.system_reset(dos_only=True)
        assert fake.sent == [{"execute": "system_reset", "arguments": {"dos_only": True}}]

    def test_refused_while_gdb_halted_raises_qmp_error(self, monkeypatch: pytest.MonkeyPatch):
        client, _fake = _connect(
            monkeypatch,
            {
                "error": {
                    "class": "GenericError",
                    "desc": "system_reset refused: the CPU is halted for debugging; "
                    "continue or detach the debug client first",
                }
            },
        )
        with pytest.raises(QMPError, match="halted for debugging"):
            client.system_reset()


class TestQueryStatus:
    def test_exposes_flat_running_and_nested_debug(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(
            monkeypatch,
            {
                "return": {
                    "status": "paused",
                    "running": False,
                    "emulator-paused": False,
                    "debug": {"active": True, "paused": True, "reason": "breakpoint"},
                }
            },
        )
        result = client.query_status()
        assert fake.sent == [{"execute": "query-status"}]
        assert result["running"] is False
        assert result["debug"] == {"active": True, "paused": True, "reason": "breakpoint"}

    def test_debug_object_without_reason_is_preserved_as_is(self, monkeypatch: pytest.MonkeyPatch):
        client, _fake = _connect(
            monkeypatch,
            {
                "return": {
                    "status": "running",
                    "running": True,
                    "emulator-paused": False,
                    "debug": {"active": False, "paused": False},
                }
            },
        )
        result = client.query_status()
        assert result["debug"] == {"active": False, "paused": False}
        assert "reason" not in result["debug"]


class TestDebugBreakOnExec:
    def test_enable_sends_true(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(monkeypatch, {"return": {"enabled": True}})
        result = client.debug_break_on_exec(True)
        assert fake.sent == [{"execute": "debug-break-on-exec", "arguments": {"enabled": True}}]
        assert result == {"enabled": True}

    def test_disable_sends_false(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(monkeypatch, {"return": {"enabled": False}})
        result = client.debug_break_on_exec(False)
        assert fake.sent == [{"execute": "debug-break-on-exec", "arguments": {"enabled": False}}]
        assert result == {"enabled": False}


class TestQuit:
    def test_sends_no_arguments_and_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(monkeypatch, {"return": {}})
        result = client.quit()
        assert fake.sent == [{"execute": "quit"}]
        assert result is None


class TestMouse:
    """`input-send-event`'s `rel` and `btn` branches, on the wire.

    What the server accepts is narrower than the QEMU protocol these events
    are borrowed from: `qmp.cpp` handles `key`, `rel` and `btn` and nothing
    else, maps exactly three button names, and answers `{"return": {}}` even
    for an event it dropped. These tests pin the JSON the client sends and
    the one place it refuses to send anything at all.
    """

    def test_move_sends_both_axes_in_one_command(self, monkeypatch: pytest.MonkeyPatch):
        """One round-trip, and one guest-visible motion: the server sums them."""
        client, fake = _connect(monkeypatch, {"return": {}})
        client.mouse_move(50, -20)
        assert fake.sent == [
            {
                "execute": "input-send-event",
                "arguments": {
                    "events": [
                        {"type": "rel", "data": {"axis": "x", "value": 50}},
                        {"type": "rel", "data": {"axis": "y", "value": -20}},
                    ]
                },
            }
        ]

    def test_move_sends_a_zero_axis_rather_than_omitting_it(self, monkeypatch: pytest.MonkeyPatch):
        """A single-axis move is still two events, so the JSON stays predictable.

        A zero `rel` costs nothing: the server adds it into the same summed
        motion it was going to queue for the other axis.
        """
        client, fake = _connect(monkeypatch, {"return": {}})
        client.mouse_move(7, 0)
        assert fake.sent[0]["arguments"]["events"] == [
            {"type": "rel", "data": {"axis": "x", "value": 7}},
            {"type": "rel", "data": {"axis": "y", "value": 0}},
        ]

    @pytest.mark.parametrize("button", ["left", "right", "middle"])
    def test_button_down_and_up_for_every_supported_button(
        self, monkeypatch: pytest.MonkeyPatch, button: str
    ):
        client, fake = _connect(monkeypatch, {"return": {}}, {"return": {}})
        client.mouse_button(button, True)
        client.mouse_button(button, False)
        assert fake.sent == [
            {
                "execute": "input-send-event",
                "arguments": {
                    "events": [{"type": "btn", "data": {"button": button, "down": down}}]
                },
            }
            for down in (True, False)
        ]

    @pytest.mark.parametrize("button", ["wheel-up", "wheel-down", "side", "extra", "LEFT", ""])
    def test_unknown_button_raises_instead_of_being_sent(
        self, monkeypatch: pytest.MonkeyPatch, button: str
    ):
        """The server would answer success and drop it, so nothing is sent.

        `qmp.cpp`'s button lookup falls through to a warning and `continue`,
        then replies `{"return": {}}` for the command as a whole. Forwarding
        the name would turn "the guest never saw this" into a successful
        call.
        """
        client, fake = _connect(monkeypatch)
        with pytest.raises(ValueError, match="unknown mouse button"):
            client.mouse_button(button, True)
        assert fake.sent == []

    def test_click_is_a_separate_press_and_release(self, monkeypatch: pytest.MonkeyPatch):
        """Two commands, not one batch: the queue is drained in a single pass.

        Batched, press and release would be applied in the same emulation
        tick and a guest polling INT 33h would never see the button down.
        """
        client, fake = _connect(monkeypatch, {"return": {}}, {"return": {}})
        client.mouse_click(hold_time=0)
        assert fake.sent == [
            {
                "execute": "input-send-event",
                "arguments": {
                    "events": [{"type": "btn", "data": {"button": "left", "down": True}}]
                },
            },
            {
                "execute": "input-send-event",
                "arguments": {
                    "events": [{"type": "btn", "data": {"button": "left", "down": False}}]
                },
            },
        ]

    def test_click_defaults_to_the_left_button(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(monkeypatch, {"return": {}}, {"return": {}})
        client.mouse_click(hold_time=0)
        assert all(
            command["arguments"]["events"][0]["data"]["button"] == "left" for command in fake.sent
        )

    def test_click_holds_for_the_requested_time(self, monkeypatch: pytest.MonkeyPatch):
        """The hold is what makes a click observable, so it is not optional.

        `time.sleep` is patched rather than waited on: what matters is that
        the press and the release are separated by the requested interval,
        not that the test spends it.
        """
        slept: list[float] = []
        monkeypatch.setattr("dbxdebug.qmp.time.sleep", slept.append)
        client, _fake = _connect(monkeypatch, {"return": {}}, {"return": {}})
        client.mouse_click("right", hold_time=0.25)
        assert slept == [0.25]

    def test_unknown_button_is_rejected_by_click_too(self, monkeypatch: pytest.MonkeyPatch):
        client, fake = _connect(monkeypatch)
        with pytest.raises(ValueError, match="unknown mouse button"):
            client.mouse_click("wheel-up")
        assert fake.sent == []
