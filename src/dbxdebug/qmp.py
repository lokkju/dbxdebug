"""
QEMU Monitor Protocol (QMP) client for DOSBox-X.

Provides keyboard input injection via the QMP protocol:
- send_key(): Press and release keys with timing control
- key_down()/key_up(): Explicit press/release control
- type_text(): Type a string of characters

Also wraps the rest of the commands the DOSBox-X QMP server dispatches:
memory and screen capture (memdump, screendump), save state control
(savestate, loadstate), run control (stop, cont, system_reset,
query_status), and debugger setup (debug_break_on_exec).
"""

import base64
import json
import socket
import time

from loguru import logger

from .dbx_kbd import DBX_KEY, DBX_KEY_TO_QCODE, char_needs_shift, char_to_qcode

__all__ = [
    "CpuNotStoppedError",
    "QMPClient",
    "QMPError",
]


class QMPError(Exception):
    """QMP protocol error."""

    pass


class CpuNotStoppedError(QMPError):
    """Raised when `memdump` is refused because the guest CPU is still running.

    A subclass of `QMPError`, not a new hierarchy: callers (and tests) that
    already catch the raw protocol error keep catching this, and the stub's
    own wording is preserved verbatim inside the message for the same
    reason.

    It exists because the raw refusal names two remedies and one of them is
    a trap. The stub says "halt via GDB or QMP stop first", but a QMP `stop`
    parks the emulation thread, and the GDB stub is polled FROM that thread.
    After `qmp.stop()` the dump does work -- and every GDB request goes
    unanswered until `qmp.cont()`. Only the GDB halt leaves both clients
    usable, which is what `CPU_NOT_STOPPED_REMEDY` spells out.
    """


# Substring of the stub's refusal that identifies "the CPU is still
# running". Matched case-insensitively, so a reworded stub that keeps the
# phrase still routes here; if it ever stops matching, the caller gets a
# plain `QMPError` carrying the stub's text, which is exactly the
# pre-existing behaviour rather than a new failure.
CPU_NOT_STOPPED_MARKER = "cpu to be stopped"

# What to do instead, appended to the stub's own refusal. The stub cannot
# say this itself: it does not know which of its clients is asking, and from
# the emulator's side a QMP `stop` really is a valid way to quiesce memory.
# It is only wrong for a caller that also intends to keep talking GDB --
# which is every caller of this package, since `memdump` is reached through
# a session that holds both clients.
CPU_NOT_STOPPED_REMEDY = (
    "Halt through GDB before dumping -- `gdb.halt()` then `qmp.memdump(...)` -- "
    "or call `DosboxSession.read_bulk(address, length)`, which halts, dumps and "
    "restores the previous run state in one call. Do NOT reach for `qmp.stop()`: "
    "it parks the emulation thread, and the GDB stub is polled from that thread, "
    "so the dump would succeed while every later GDB request goes unanswered "
    "until `qmp.cont()`."
)


class QMPClient:
    """QEMU Monitor Protocol client for DOSBox-X keyboard input."""

    DEFAULT_PORT = 4444

    def __init__(self, host: str = "localhost", port: int = DEFAULT_PORT):
        """
        Connect to DOSBox-X QMP server.

        Args:
            host: Server hostname
            port: Server port (default 4444)
        """
        logger.debug(f"Connecting to QMP server at {host}:{port}")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        # The protocol is strictly request/response with tiny packets, the
        # workload Nagle punishes worst: each side holds a small write waiting
        # for an ACK the peer has delayed. Measured ~82ms per round-trip with
        # it on, ~41ms with only this side fixed -- one ~40ms stall per
        # direction, so the stub has to set it too. There is no batching here
        # to preserve, so there is no tradeoff being made.
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buffer = ""

        # Read greeting
        greeting = self._read_message()
        logger.debug(f"QMP greeting: {greeting}")

        if "QMP" not in greeting:
            raise QMPError(f"Unexpected QMP greeting: {greeting}")

        # Send capabilities negotiation
        self._send_command("qmp_capabilities")
        logger.debug("QMP capabilities negotiated")

    def _send_raw(self, data: str) -> None:
        """Send raw string to server."""
        if self.sock is None:
            raise ConnectionError("Socket not initialized")
        self.sock.sendall((data + "\r\n").encode())

    def _read_message(self) -> dict:
        """Read a JSON message from the server."""
        if self.sock is None:
            raise ConnectionError("Socket not initialized")

        while True:
            # Look for complete JSON object in buffer
            if self.buffer:
                try:
                    # Try to parse what we have
                    msg = json.loads(self.buffer.strip())
                    self.buffer = ""
                    return msg
                except json.JSONDecodeError:
                    pass

            # Need more data
            data = self.sock.recv(4096).decode()
            if not data:
                raise ConnectionError("Connection closed")
            self.buffer += data

            # Try to find complete message (newline-delimited)
            if "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                if line.strip():
                    return json.loads(line.strip())

    def _send_command(self, execute: str, arguments: dict | None = None) -> dict:
        """
        Send a QMP command and return the response.

        Args:
            execute: Command name
            arguments: Optional command arguments

        Returns:
            Response dict

        Raises:
            QMPError: If command returns an error
        """
        cmd: dict = {"execute": execute}
        if arguments:
            cmd["arguments"] = arguments

        self._send_raw(json.dumps(cmd))
        response = self._read_message()

        if "error" in response:
            error = response["error"]
            raise QMPError(f"{error.get('class', 'Error')}: {error.get('desc', 'Unknown error')}")

        return response

    def send_key(self, keys: list[str], hold_time: int = 100) -> None:
        """
        Send simultaneous key presses with auto-release.

        All keys are pressed, held for hold_time ms, then released in reverse order.

        Args:
            keys: List of QMP qcode strings (e.g., ["ctrl", "alt", "delete"])
            hold_time: Milliseconds to hold before releasing (default 100)

        Example:
            >>> qmp.send_key(["ctrl", "c"])  # Ctrl+C
            >>> qmp.send_key(["a"])  # Press 'a'
        """
        key_objects = [{"type": "qcode", "data": k} for k in keys]
        self._send_command("send-key", {"keys": key_objects, "hold-time": hold_time})

    def send_key_dbx(self, keys: list[DBX_KEY], hold_time: int = 100) -> None:
        """
        Send keys using DBX_KEY enum values.

        Args:
            keys: List of DBX_KEY values
            hold_time: Milliseconds to hold before releasing

        Example:
            >>> qmp.send_key_dbx([DBX_KEY.KBD_leftctrl, DBX_KEY.KBD_c])
        """
        qcodes = []
        for key in keys:
            qcode = DBX_KEY_TO_QCODE.get(key)
            if qcode is None:
                raise ValueError(f"No QMP mapping for key: {key.name}")
            qcodes.append(qcode)
        self.send_key(qcodes, hold_time)

    def key_down(self, key: str) -> None:
        """
        Send a key press (down) event.

        Args:
            key: QMP qcode string
        """
        event = {
            "type": "key",
            "data": {"down": True, "key": {"type": "qcode", "data": key}},
        }
        self._send_command("input-send-event", {"events": [event]})

    def key_up(self, key: str) -> None:
        """
        Send a key release (up) event.

        Args:
            key: QMP qcode string
        """
        event = {
            "type": "key",
            "data": {"down": False, "key": {"type": "qcode", "data": key}},
        }
        self._send_command("input-send-event", {"events": [event]})

    def key_press(self, key: str, hold_time: float = 0.05) -> None:
        """
        Press and release a single key with timing control.

        Args:
            key: QMP qcode string
            hold_time: Seconds to hold (default 0.05)
        """
        self.key_down(key)
        time.sleep(hold_time)
        self.key_up(key)

    def type_text(self, text: str, delay: float = 0.05) -> None:
        """
        Type a string of text.

        Handles shift for uppercase and special characters.

        Args:
            text: Text to type
            delay: Delay between characters in seconds (default 0.05)

        Example:
            >>> qmp.type_text("Hello World!")
        """
        for char in text:
            qcode = char_to_qcode(char)
            if qcode is None:
                logger.warning(f"Cannot type character: {repr(char)}")
                continue

            if char_needs_shift(char):
                # Hold shift, press key, release key, release shift
                self.key_down("shift")
                time.sleep(0.01)
                self.key_press(qcode, 0.03)
                self.key_up("shift")
            else:
                self.key_press(qcode, 0.03)

            time.sleep(delay)

    def query_commands(self) -> list[str]:
        """
        Query available QMP commands.

        Returns:
            List of command names
        """
        response = self._send_command("query-commands")
        return [cmd["name"] for cmd in response.get("return", [])]

    def memdump(self, address: int, size: int, file: str | None = None) -> bytes | str:
        """
        Dump a range of guest memory in a single round-trip.

        This is the bulk-read path: one `memdump` call replaces thousands of
        GDB `m` round-trips. A recorded segment scan cost 7,168 of them
        before this existed, one per small chunk of memory; `memdump` reads
        the whole range server-side and ships it back in one reply.

        Without `file`, the server base64-encodes the dump into the reply
        and this method decodes it to `bytes`, capped at a 16 MB
        server-side limit. With `file`, the server writes the dump to that
        path on the machine running DOSBox-X and returns the path instead
        of transferring the bytes.

        IMPORTANT: this command REQUIRES the CPU to be stopped -- via a GDB
        halt, the interactive debugger, or a QMP `stop` -- before it will
        run. It reads guest memory directly, off the socket thread, and
        would race the emulation thread otherwise; DOSBox-X refuses the
        request rather than risk a torn read.

        Of the three ways to stop it, only the GDB halt leaves the rest of
        the debug surface usable: `qmp.stop()` parks the emulation thread,
        and the GDB stub is polled from that thread, so the dump succeeds
        and every GDB request after it goes unanswered. `DosboxSession.read_bulk`
        exists so a caller need not remember any of this.

        Args:
            address: Linear guest address to start reading from
            size: Number of bytes to read (server limit: 16 MB)
            file: Optional server-side path to write the dump to instead of
                returning it inline

        Returns:
            The dumped bytes, or the server-side file path if `file` was given

        Raises:
            CpuNotStoppedError: If the CPU is still running. Carries the
                stub's own refusal plus the fix, and subclasses `QMPError`.
            QMPError: If the dump fails for any other reason
        """
        arguments: dict = {"address": address, "size": size}
        if file is not None:
            arguments["file"] = file
        try:
            response = self._send_command("memdump", arguments)
        except QMPError as exc:
            if CPU_NOT_STOPPED_MARKER in str(exc).lower():
                raise CpuNotStoppedError(f"{exc}. {CPU_NOT_STOPPED_REMEDY}") from exc
            raise
        result = response.get("return", {})
        if "file" in result:
            return result["file"]
        return base64.b64decode(result["data"])

    def screendump(self, file: str | None = None) -> dict:
        """
        Capture the current display as a PNG.

        Args:
            file: Optional server-side path to write the screenshot to.
                Without it, the reply includes base64-encoded PNG data.

        Returns:
            The response's `return` dict (keys vary: `data`/`size`/`format`/
            `file` without `file`, `file`/`size`/`format` with it)
        """
        arguments = {"file": file} if file is not None else None
        response = self._send_command("screendump", arguments)
        return response.get("return", {})

    def savestate(self, file: str) -> dict:
        """
        Save emulator state to a file.

        Args:
            file: Path to write the save state to

        Returns:
            Response dict, e.g. `{"file": <path>}`

        Raises:
            QMPError: If the save fails or times out
        """
        response = self._send_command("savestate", {"file": file})
        return response.get("return", {})

    def loadstate(self, file: str) -> dict:
        """
        Load emulator state from a file.

        Args:
            file: Path to the save state to load

        Returns:
            Response dict, e.g. `{"file": <path>}`

        Raises:
            QMPError: If the file is missing, the load fails, or it times out
        """
        response = self._send_command("loadstate", {"file": file})
        return response.get("return", {})

    def stop(self) -> dict:
        """
        Pause the emulator.

        Idempotent: pausing an already-paused emulator succeeds.

        Returns:
            Response dict (empty on success)
        """
        response = self._send_command("stop")
        return response.get("return", {})

    def cont(self) -> dict:
        """
        Resume the emulator.

        Idempotent: resuming an already-running emulator succeeds.

        Returns:
            Response dict (empty on success)
        """
        response = self._send_command("cont")
        return response.get("return", {})

    def system_reset(self, dos_only: bool = False) -> dict:
        """
        Reset the emulated system.

        Refused while the CPU is halted for debugging (a GDB halt or the
        interactive debugger), since resetting out from under an attached
        debug client would leave it looking at registers, memory, and
        breakpoints that no longer exist. A plain QMP `stop` does not block
        this, since there is no debug client to confuse.

        Args:
            dos_only: If True, reset only the DOS environment rather than
                the whole emulated machine

        Returns:
            Response dict (empty on success)

        Raises:
            QMPError: If the CPU is halted for debugging
        """
        response = self._send_command("system_reset", {"dos_only": dos_only})
        return response.get("return", {})

    def query_status(self) -> dict:
        """
        Query emulator and debugger run state.

        The reply carries two independent signals: a flat `running` boolean
        for the emulator as a whole, and a nested `debug` object
        (`active`, `paused`, and optionally `reason`) for the debugger
        specifically. Both are returned as-is -- neither is flattened nor
        dropped, since a debugger pause and an emulator pause are distinct
        states that happen to overlap in the `running`/`status` summary
        fields.

        Returns:
            Response dict with `status`, `running`, `emulator-paused`, and
            `debug` (itself `active`/`paused`/optional `reason`)
        """
        response = self._send_command("query-status")
        return response.get("return", {})

    def debug_break_on_exec(self, enabled: bool) -> dict:
        """
        Set or clear break-on-exec for the next DOS program launch.

        When enabled, the debugger breaks at the entry point of the next
        program executed via DOS.

        Args:
            enabled: True to arm the break, False to clear it

        Returns:
            Response dict, e.g. `{"enabled": <bool>}`
        """
        response = self._send_command("debug-break-on-exec", {"enabled": enabled})
        return response.get("return", {})

    def quit(self) -> None:
        """
        Send the `quit` command.

        NOTE: `quit` (and its `system_powerdown` alias) is dispatched by the
        DOSBox-X QMP server but is NOT advertised by `query-commands` -- it
        won't appear in `query_commands()`'s output even though the server
        accepts it. It also does not actually quit DOSBox-X: the server
        acknowledges the command and does nothing else, by design.

        Returns:
            None
        """
        self._send_command("quit")

    def close(self) -> None:
        """Close the connection."""
        if self.sock:
            self.sock.close()
            self.sock = None  # type: ignore

    def __enter__(self) -> "QMPClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
