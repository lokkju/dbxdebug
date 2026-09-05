"""
GDB Remote Serial Protocol client for DOSBox-X.

Provides debugging capabilities:
- Memory read/write
- Register read/write
- Breakpoint management
- Execution control (step, continue)
"""

import binascii
import socket

from loguru import logger

from .addressing import linear_pc, parse_address
from .utils import parse_x86_address

# The vendor capability that means "Z0/z0 and m/M take a linear address."
# Builds that lack it split the Z0/z0 argument as a packed far pointer
# (seg = addr >> 16), so a breakpoint above 64 KB answers OK and is stored
# at a garbage location -- silently, since the response looks identical
# either way. See addressing.py for the full history.
LINEAR_BREAKPOINTS_CAPABILITY = "dosbox-x-linear-bp+"

# Order of the 16 registers the `g`/`G`/`P` packets exchange, 4 bytes each,
# little-endian. Position matches `addressing.CS_INDEX` (10) and
# `addressing.EIP_INDEX` (8).
REGISTER_NAMES = [
    "eax",
    "ecx",
    "edx",
    "ebx",
    "esp",
    "ebp",
    "esi",
    "edi",
    "eip",
    "eflags",
    "cs",
    "ss",
    "ds",
    "es",
    "fs",
    "gs",
]


class IncompatibleStubError(RuntimeError):
    """Raised when the connected GDB stub lacks a required vendor capability."""


class GDBClient:
    """GDB Remote Serial Protocol client for DOSBox-X debugging."""

    DEFAULT_PORT = 2159

    def __init__(
        self,
        host: str = "localhost",
        port: int = DEFAULT_PORT,
        require_capabilities: bool = True,
    ):
        """
        Connect to DOSBox-X GDB server.

        Args:
            host: Server hostname
            port: Server port (default 2159)
            require_capabilities: If True (the default), refuse to proceed
                unless the stub advertises `dosbox-x-linear-bp+` in its
                `qSupported` reply. A stub that lacks it splits breakpoint
                addresses as packed far pointers rather than linear
                addresses, so breakpoints above 64 KB answer OK and never
                fire. Pass False only when deliberately driving such a
                build.

        Raises:
            IncompatibleStubError: If `require_capabilities` is True and the
                stub does not advertise `dosbox-x-linear-bp+`.
        """
        logger.debug(f"Connecting to GDB server at {host}:{port}")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        self.buffer = b""
        self._no_ack_mode = False

        # Initial handshake
        self._send_packet(b"qSupported:multiprocess+")
        response = self._read_packet()
        logger.debug(f"Handshake response: {response!r}")
        self.capabilities: set[str] = self._parse_capabilities(response)

        if require_capabilities:
            self.require_linear_breakpoints()

    @staticmethod
    def _parse_capabilities(response: bytes) -> set[str]:
        """Parse a `qSupported` reply into a set of feature tokens.

        Args:
            response: Raw `qSupported` reply, semicolon-separated feature
                tokens such as `swbreak+`, `PacketSize=3fff`, or `foo-`.

        Returns:
            The set of feature tokens exactly as advertised (decoded, split
            on `;`, otherwise unmodified).
        """
        text = response.decode("ascii", errors="replace")
        return {token for token in text.split(";") if token}

    def require_linear_breakpoints(self) -> None:
        """Raise unless the stub advertises linear breakpoint addressing.

        Raises:
            IncompatibleStubError: If `dosbox-x-linear-bp+` is absent from
                `self.capabilities`.
        """
        if LINEAR_BREAKPOINTS_CAPABILITY not in self.capabilities:
            raise IncompatibleStubError(
                f"GDB stub does not advertise {LINEAR_BREAKPOINTS_CAPABILITY}: "
                "this build splits Z0/z0's address as a packed far pointer "
                "(seg = addr >> 16), so a breakpoint set above 64 KB will "
                "answer OK and never fire. Pass require_capabilities=False "
                "to GDBClient to proceed against this build anyway."
            )

    def _calculate_checksum(self, data: bytes) -> int:
        """Calculate GDB packet checksum."""
        checksum = 0
        for b in data:
            checksum = (checksum + b) & 0xFF
        return checksum

    def _send_packet(self, packet: bytes) -> None:
        """Send a GDB packet with checksum."""
        checksum = self._calculate_checksum(packet)
        packet_with_checksum = b"$" + packet + b"#" + f"{checksum:02x}".encode()

        if self.sock is None:
            raise ConnectionError("Socket not initialized")

        self.sock.sendall(packet_with_checksum)

        if not self._no_ack_mode:
            ack = self.sock.recv(1)
            if ack != b"+":
                raise ConnectionError(f"Failed to receive ACK. Got: {ack}")

    def _read_packet(self) -> bytes:
        """Read a GDB packet and verify checksum."""
        while True:
            if self.sock is None:
                raise ConnectionError("Socket not initialized")

            if not self.buffer:
                self.buffer = self.sock.recv(4096)
                if not self.buffer:
                    raise ConnectionError("Connection closed")

            # Find packet start
            if self.buffer[0:1] != b"$":
                self.buffer = self.buffer[1:]
                continue

            # Find packet end
            hash_pos = self.buffer.find(b"#")
            if hash_pos == -1:
                more_data = self.sock.recv(4096)
                if not more_data:
                    raise ConnectionError("Connection closed while waiting for packet end")
                self.buffer += more_data
                continue

            # Need checksum bytes
            if len(self.buffer) < hash_pos + 3:
                more_data = self.sock.recv(4096)
                if not more_data:
                    raise ConnectionError("Connection closed while waiting for checksum")
                self.buffer += more_data
                continue

            packet_data = self.buffer[1:hash_pos]
            checksum_bytes = self.buffer[hash_pos + 1 : hash_pos + 3]

            calculated_checksum = self._calculate_checksum(packet_data)
            received_checksum = int(checksum_bytes, 16)

            if calculated_checksum == received_checksum:
                if not self._no_ack_mode:
                    self.sock.sendall(b"+")
                self.buffer = self.buffer[hash_pos + 3 :]
                return packet_data
            else:
                if not self._no_ack_mode:
                    self.sock.sendall(b"-")
                self.buffer = self.buffer[hash_pos + 3 :]
                continue

    @staticmethod
    def _resolve_address(address: str | int) -> int:
        """Resolve a caller-supplied address to a validated linear address.

        Accepts every format `parse_x86_address` understands -- a bare int,
        a bare hex string, or a `"seg:off"` string -- then validates
        the result with `addressing.parse_address` to reject values that
        look like a packed far pointer left over from the pre-fix protocol
        convention. `Z0`/`z0`/`m`/`M` all take a linear address, so a caller
        that still packs `(seg << 16) | off` into a plain int must be told
        loudly rather than have that int forwarded as-is.

        Args:
            address: Linear address, or segmented address (e.g.
                `"b800:0000"`), or a bare hex/decimal string.

        Returns:
            A linear address confirmed not to look like a packed far
            pointer.

        Raises:
            PackedAddressError: If the resolved address is at or above
                `addressing.REAL_MODE_CEILING`, indicating it is almost
                certainly a packed far pointer rather than a real linear
                address.
        """
        return parse_address(parse_x86_address(address))

    def enable_no_ack_mode(self) -> bool:
        """Enable no-ACK mode for faster communication."""
        self._send_packet(b"QStartNoAckMode")
        response = self._read_packet()
        if response == b"OK":
            self._no_ack_mode = True
            return True
        return False

    def read_memory(self, address: str | int, length: int) -> bytes:
        """
        Read memory from the target.

        Args:
            address: Linear address or segmented address (e.g., "b800:0000")
            length: Number of bytes to read

        Returns:
            Raw bytes from memory

        Raises:
            MemoryError: If read fails
        """
        linear_addr = self._resolve_address(address)
        cmd = f"m{linear_addr:x},{length:x}".encode()
        self._send_packet(cmd)
        response = self._read_packet()

        if response.startswith(b"E"):
            error_code = response[1:].decode()
            raise MemoryError(f"Error reading memory at 0x{linear_addr:x}: {error_code}")

        return binascii.unhexlify(response)

    def write_memory(self, address: str | int, data: bytes) -> None:
        """
        Write memory to the target.

        Args:
            address: Linear address or segmented address
            data: Bytes to write

        Raises:
            MemoryError: If write fails
        """
        linear_addr = self._resolve_address(address)
        hex_data = binascii.hexlify(data).decode()
        cmd = f"M{linear_addr:x},{len(data):x}:{hex_data}".encode()
        self._send_packet(cmd)
        response = self._read_packet()

        if response != b"OK":
            raise MemoryError(f"Error writing memory at 0x{linear_addr:x}: {response.decode()}")

    def read_register_list(self) -> list[int]:
        """Read the raw 16-register `g` packet, in stub order.

        Returns:
            16 register values in the order `g`/`G`/`P` use: EAX, ECX, EDX,
            EBX, ESP, EBP, ESI, EDI, EIP, EFLAGS, CS, SS, DS, ES, FS, GS.
            `registers[addressing.EIP_INDEX]` is an offset within
            `registers[addressing.CS_INDEX]`, not a linear address --
            pass this list to `addressing.linear_pc` (or call `linear_pc()`
            below) to get the linear program counter.
        """
        self._send_packet(b"g")
        response = self._read_packet()

        registers = []
        for i in range(len(REGISTER_NAMES)):
            hex_val = response[i * 8 : (i + 1) * 8]
            val_bytes = binascii.unhexlify(hex_val)
            registers.append(int.from_bytes(val_bytes, "little"))

        return registers

    def read_registers(self) -> dict[str, int]:
        """
        Read all CPU registers.

        Returns:
            Dict mapping register names to values. **`registers["eip"]` is
            an offset within `registers["cs"]`, not a linear address.**
            DOSBox-X's GDB stub used to return `SegPhys(cs) + reg_eip` here
            (and write `reg_eip` verbatim on `G`), so a `g`/`G` round-trip
            against an old build silently moved the program counter. Code
            written against that old build that treats this `eip` value as
            a linear address is now silently wrong. Use `linear_pc()` to
            get the linear program counter instead of combining `eip`
            yourself.
        """
        registers = self.read_register_list()
        return dict(zip(REGISTER_NAMES, registers, strict=True))

    def read_register(self, reg_num: int) -> int:
        """
        Read a single register.

        Args:
            reg_num: Register number (0-15)

        Returns:
            Register value. If `reg_num` is `addressing.EIP_INDEX` (8),
            this is an offset within CS, not a linear address -- see
            `read_registers`.
        """
        self._send_packet(f"p{reg_num:x}".encode())
        response = self._read_packet()
        val_bytes = binascii.unhexlify(response)
        return int.from_bytes(val_bytes, "little")

    def write_register(self, index: int, value: int) -> bool:
        """Write a single register with the `P` packet.

        Args:
            index: Register number (0-15), in the same order as
                `read_register_list` / `REGISTER_NAMES`. Writing
                `addressing.EIP_INDEX` (8) sets the offset within CS, not a
                linear address -- see `read_registers`.
            value: New register value, encoded little-endian to match
                `g`/`G`.

        Returns:
            True if the stub acknowledged the write with `OK`, False
            otherwise (e.g. an `E`-prefixed error reply).
        """
        hex_val = value.to_bytes(4, "little").hex()
        self._send_packet(f"P{index:x}={hex_val}".encode())
        response = self._read_packet()
        return response == b"OK"

    def linear_pc(self) -> int:
        """Read registers and compute the linear program counter.

        Returns:
            `CS * 16 + EIP`, per `addressing.linear_pc`. This is the value
            to use as a linear address (e.g. for `read_memory` or
            `set_breakpoint`) -- `read_registers()["eip"]` alone is not one.
        """
        return linear_pc(self.read_register_list())

    def set_breakpoint(self, address: str | int) -> bool:
        """
        Set a software breakpoint.

        Args:
            address: Linear or segmented address

        Returns:
            True if successful
        """
        linear_addr = self._resolve_address(address)
        self._send_packet(f"Z0,{linear_addr:x},1".encode())
        response = self._read_packet()
        return response == b"OK"

    def remove_breakpoint(self, address: str | int) -> bool:
        """
        Remove a breakpoint.

        Args:
            address: Linear or segmented address

        Returns:
            True if successful
        """
        linear_addr = self._resolve_address(address)
        self._send_packet(f"z0,{linear_addr:x},1".encode())
        response = self._read_packet()
        return response == b"OK"

    def step(self) -> bytes:
        """
        Single-step one instruction.

        Returns:
            Stop reason response
        """
        self._send_packet(b"s")
        return self._read_packet()

    def continue_execution(self) -> bytes:
        """
        Continue execution until breakpoint or stop.

        Returns:
            Stop reason response
        """
        self._send_packet(b"c")
        return self._read_packet()

    def halt(self) -> bytes:
        """
        Request halt/break into debugger.

        Returns:
            Stop reason response
        """
        self._send_packet(b"?")
        return self._read_packet()

    def close(self) -> None:
        """Close the connection."""
        if self.sock:
            self.sock.close()
            self.sock = None  # type: ignore

    def __enter__(self) -> "GDBClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
