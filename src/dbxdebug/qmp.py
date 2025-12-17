"""
QEMU Monitor Protocol (QMP) client for DOSBox-X keyboard input.

Provides keyboard input injection via the QMP protocol:
- send_key(): Press and release keys with timing control
- key_down()/key_up(): Explicit press/release control
- type_text(): Type a string of characters
"""

import json
import socket
import time

from loguru import logger

from .dbx_kbd import DBX_KEY, DBX_KEY_TO_QCODE, char_needs_shift, char_to_qcode


class QMPError(Exception):
    """QMP protocol error."""

    pass


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

    def close(self) -> None:
        """Close the connection."""
        if self.sock:
            self.sock.close()
            self.sock = None  # type: ignore

    def __enter__(self) -> "QMPClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
