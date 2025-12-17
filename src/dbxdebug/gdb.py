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

from .utils import parse_x86_address


class GDBClient:
    """GDB Remote Serial Protocol client for DOSBox-X debugging."""

    DEFAULT_PORT = 2159

    def __init__(self, host: str = "localhost", port: int = DEFAULT_PORT):
        """
        Connect to DOSBox-X GDB server.

        Args:
            host: Server hostname
            port: Server port (default 2159)
        """
        logger.debug(f"Connecting to GDB server at {host}:{port}")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        self.buffer = b""
        self._no_ack_mode = False

        # Initial handshake
        self._send_packet(
            b"qSupported:multiprocess+;swbreak+;hwbreak+;qRelocInsn+;"
            b"fork-events+;vfork-events+;exec-events+;vContSupported+;"
            b"QThreadEvents+;no-resumed+"
        )
        response = self._read_packet()
        logger.debug(f"Handshake response: {response}")

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
        linear_addr = parse_x86_address(address)
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
        linear_addr = parse_x86_address(address)
        hex_data = binascii.hexlify(data).decode()
        cmd = f"M{linear_addr:x},{len(data):x}:{hex_data}".encode()
        self._send_packet(cmd)
        response = self._read_packet()

        if response != b"OK":
            raise MemoryError(f"Error writing memory at 0x{linear_addr:x}: {response.decode()}")

    def read_registers(self) -> dict[str, int]:
        """
        Read all CPU registers.

        Returns:
            Dict mapping register names to values
        """
        self._send_packet(b"g")
        response = self._read_packet()

        # Response is 16 registers, 8 hex chars each (little-endian)
        reg_names = [
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

        registers = {}
        for i, name in enumerate(reg_names):
            hex_val = response[i * 8 : (i + 1) * 8]
            # Convert from little-endian
            val_bytes = binascii.unhexlify(hex_val)
            registers[name] = int.from_bytes(val_bytes, "little")

        return registers

    def read_register(self, reg_num: int) -> int:
        """
        Read a single register.

        Args:
            reg_num: Register number (0-15)

        Returns:
            Register value
        """
        self._send_packet(f"p{reg_num:x}".encode())
        response = self._read_packet()
        val_bytes = binascii.unhexlify(response)
        return int.from_bytes(val_bytes, "little")

    def set_breakpoint(self, address: str | int) -> bool:
        """
        Set a software breakpoint.

        Args:
            address: Linear or segmented address

        Returns:
            True if successful
        """
        linear_addr = parse_x86_address(address)
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
        linear_addr = parse_x86_address(address)
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
