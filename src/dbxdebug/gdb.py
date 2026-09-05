"""
GDB Remote Serial Protocol client for DOSBox-X.

Provides debugging capabilities:
- Memory read/write
- Register read/write
- Breakpoint management
- Execution control (step, continue)

FRAMING. The wire is read as a stream of three token kinds -- `+`, `-`, and
`$payload#xx` -- rather than as a strict request/response alternation, because
the alternation does not hold. Two things break it, both reproduced live:

1. The stub sends stop replies nobody asked for. QMP break-on-exec arms AND
   immediately activates a breakpoint while the CPU free-runs, so on the hit
   `$S05#b8` appears on a connection whose last request is still awaiting its
   ACK. Read as an alternation, that `$` is mistaken for the ACK.
2. A request whose reply is abandoned -- a timeout -- leaves the stub owing
   bytes that arrive later. Read as an alternation, they become the answer to
   whatever is asked NEXT, and every later reply is one packet behind. That is
   silent: the caller gets well-formed bytes belonging to someone else.

So this client tracks what the stub still owes it (`_owed_ack`,
`_owed_reply`), drains exactly that much before sending anything else, and
diverts stop replies that arrive where none was requested into
`pending_stops` instead of returning them as an answer. When a drain cannot
complete, the client marks itself unusable rather than guessing: a loud
failure on every later call beats a plausible wrong answer.

There is deliberately no read-retry loop -- see `frames.py` for why retrying
masks this fault instead of surfacing it.
"""

import binascii
import socket
from collections import deque

from loguru import logger

from .addressing import linear_pc, parse_address
from .utils import parse_x86_address

# Wall seconds any single socket operation -- the connect included -- may
# block before it raises. Without one, a packet the stub never answers hangs
# the caller forever, which is easy to reach by accident: while the emulator
# is QMP-stopped the GDB stub is not serviced at all (it is polled from the
# emulation thread), so `qmp.stop()` followed by any GDB request deadlocks
# with no diagnostic. Pass `timeout=None` to restore unbounded blocking.
DEFAULT_TIMEOUT = 30.0

# The packets after which a stop reply IS the answer. Everything else this
# client sends is answered with `OK`, an `E`-prefixed error, or lowercase
# hex, so a stop reply arriving after one of those was sent unprompted.
RESUMING_PACKETS = frozenset({b"c", b"s", b"?"})

# First byte of a GDB stop reply: `S`/`T` signalled stop, `W` exited, `X`
# terminated by signal. All uppercase, which is what makes the test
# unambiguous -- `binascii.hexlify` output is lowercase, and the only other
# replies this stub sends are `OK` and `E<xx>`.
STOP_REPLY_PREFIXES = b"STWX"

# How many unrequested stop replies `pending_stops` retains. A bound, not a
# design limit: a stub that floods the connection must not grow the queue
# without end, and the oldest of a flood is the least interesting.
MAX_PENDING_STOPS = 64

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


class GDBTimeoutError(TimeoutError):
    """Raised when the stub does not answer a sent packet in time.

    Subclasses the builtin `TimeoutError` -- which `socket.timeout` has been
    an alias of since Python 3.10 -- so callers that already catch a socket
    timeout keep catching this. The message names the packet that went
    unanswered, which is the whole point: the previous behaviour was an
    unbounded hang with nothing to read.

    The client is NOT unusable after this. The bytes the stub still owes are
    drained before the next request is sent; only if that drain fails does
    the client mark itself unusable (`GDBDesyncError`).
    """


class GDBDesyncError(ConnectionError):
    """Raised when the client cannot prove where it sits in the packet stream.

    Either the stub rejected a packet with `-`, or an abandoned reply could
    not be drained. Once raised for the latter reason the client stays
    unusable and every later call raises it again: continuing would mean
    handing back bytes that belong to a different request, which is the
    failure mode this whole class exists to prevent. Open a new `GDBClient`.

    Subclasses `ConnectionError`, which is what the pre-fix client raised on
    the same wire conditions.
    """


def looks_like_stop_reply(payload: bytes) -> bool:
    """Report whether `payload` is a GDB stop reply.

    Args:
        payload: A packet payload, checksum and framing already stripped.

    Returns:
        True if the payload opens with an uppercase `S`, `T`, `W` or `X`
        followed by a two-digit hex code. No reply this stub sends to a
        non-resuming packet can match: `m`/`g`/`p` answer in lowercase hex,
        and the only other forms are `OK` and `E<xx>`.
    """
    if len(payload) < 3 or payload[0] not in STOP_REPLY_PREFIXES:
        return False
    try:
        int(payload[1:3], 16)
    except ValueError:
        return False
    return True


class GDBClient:
    """GDB Remote Serial Protocol client for DOSBox-X debugging."""

    DEFAULT_PORT = 2159

    def __init__(
        self,
        host: str = "localhost",
        port: int = DEFAULT_PORT,
        require_capabilities: bool = True,
        timeout: float | None = DEFAULT_TIMEOUT,
    ):
        """
        Connect to DOSBox-X GDB server.

        Args:
            host: Server hostname
            port: Server port (default 2159)
            timeout: Wall seconds any single socket operation may block
                before raising -- applied to every read, not only to the
                connect. On expiry `GDBTimeoutError` names the packet that
                went unanswered. `None` restores unbounded blocking, which
                is what the pre-fix client did unconditionally. Changing
                `sock.settimeout()` afterwards is honoured: every read
                consults the socket's own timeout, nothing is cached.
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
        self.sock.settimeout(timeout)
        self.sock.connect((host, port))
        self.buffer = b""
        self._no_ack_mode = False
        # Stop replies that arrived where none was requested -- see
        # `pending_stops` for what a caller is meant to do with them.
        self._pending_stops: deque[bytes] = deque(maxlen=MAX_PENDING_STOPS)
        # What the stub still owes for a packet already on the wire, and
        # which packet that was. Both flags survive a timeout on purpose:
        # they are exactly what lets the NEXT request drain the abandoned
        # exchange rather than read it as its own reply.
        self._owed_ack = False
        self._owed_reply = False
        self._last_sent = b""
        # Set once the stream's position can no longer be proven. Every
        # later exchange raises with this reason instead of answering.
        self._unusable: str | None = None

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

    @property
    def pending_stops(self) -> tuple[bytes, ...]:
        """Stop replies that arrived where none had been requested.

        The stub sends these unprompted -- QMP break-on-exec activates a
        breakpoint the GDB client never asked to run into, and the resulting
        `S05` lands on a connection mid-exchange. They are queued here rather
        than returned as an answer, because returning one would be exactly
        the off-by-one-packet corruption this client exists to avoid, and
        dropping one would hide that the CPU stopped.

        A queue was chosen over a callback: callers of this package poll
        (`wait_for_text`, the `frames` walkers) rather than run an event
        loop, a callback would fire from inside an unrelated `read_memory`
        and could not be reasoned about, and a plain attribute could not
        hold two stops from one exchange. Reading this property does not
        consume anything -- use `take_pending_stops` for that.

        Returns:
            The queued stop reply payloads, oldest first, at most
            `MAX_PENDING_STOPS` of them.
        """
        return tuple(self._pending_stops)

    def take_pending_stops(self) -> list[bytes]:
        """Remove and return every queued unrequested stop reply.

        Returns:
            The queued payloads, oldest first. The queue is left empty.
        """
        drained = list(self._pending_stops)
        self._pending_stops.clear()
        return drained

    def _ensure_usable(self) -> None:
        """Raise if this client can no longer prove its stream position.

        Raises:
            GDBDesyncError: If a previous drain failed. Permanent by
                design -- see the exception's own docstring.
        """
        if self._unusable is not None:
            raise GDBDesyncError(self._unusable)

    def _write(self, data: bytes) -> None:
        """Send raw bytes, checking the socket is still open.

        Args:
            data: Bytes to write.

        Raises:
            ConnectionError: If the socket has been closed.
        """
        if self.sock is None:
            raise ConnectionError("Socket not initialized")
        self.sock.sendall(data)

    def _recv(self) -> None:
        """Append one chunk from the socket to the read buffer.

        Raises:
            GDBTimeoutError: If nothing arrives within the socket's timeout.
                The message names the packet left unanswered.
            ConnectionError: If the peer closed the connection.
        """
        if self.sock is None:
            raise ConnectionError("Socket not initialized")
        try:
            chunk = self.sock.recv(4096)
        except TimeoutError as exc:
            raise GDBTimeoutError(
                f"GDB stub did not answer {self._last_sent!r} within "
                f"{self.sock.gettimeout()}s. Whatever it still owes is drained "
                f"before the next request; if that drain also fails this client "
                f"is marked unusable rather than answering with bytes belonging "
                f"to another request."
            ) from exc
        if not chunk:
            raise ConnectionError("Connection closed")
        self.buffer += chunk

    def _next_token(self) -> tuple[str, bytes]:
        """Consume the next protocol token from the stream.

        Bytes that are neither an ACK, a NACK, nor the start of a packet are
        discarded, matching what the previous parser did with them.

        Returns:
            `("ack", b"")`, `("nack", b"")`, or `("packet", payload)` with
            the payload's checksum already verified and acknowledged.

        Raises:
            GDBTimeoutError: If the stream stalls mid-token.
            ConnectionError: If the peer closed the connection.
        """
        while True:
            if not self.buffer:
                self._recv()

            head = self.buffer[0:1]
            if head == b"+":
                self.buffer = self.buffer[1:]
                return "ack", b""
            if head == b"-":
                self.buffer = self.buffer[1:]
                return "nack", b""
            if head != b"$":
                self.buffer = self.buffer[1:]
                continue

            hash_pos = self.buffer.find(b"#")
            if hash_pos == -1 or len(self.buffer) < hash_pos + 3:
                self._recv()
                continue

            payload = self.buffer[1:hash_pos]
            checksum_bytes = self.buffer[hash_pos + 1 : hash_pos + 3]
            self.buffer = self.buffer[hash_pos + 3 :]
            try:
                received_checksum = int(checksum_bytes, 16)
            except ValueError:
                received_checksum = -1

            if self._calculate_checksum(payload) == received_checksum:
                if not self._no_ack_mode:
                    self._write(b"+")
                return "packet", payload
            if not self._no_ack_mode:
                self._write(b"-")

    def _record_unsolicited(self, payload: bytes) -> None:
        """Queue a stop reply that arrived where none had been requested.

        Args:
            payload: The stop reply payload, e.g. `b"S05"`.
        """
        logger.warning(f"Unsolicited GDB stop reply queued: {payload!r}")
        self._pending_stops.append(payload)

    def _await_ack(self) -> None:
        """Consume the ACK the stub owes for the packet just sent.

        The stub acknowledges a packet before it answers it, so any framed
        packet seen here cannot be our own reply -- it was sent unprompted,
        and is queued rather than mistaken for the ACK (which is precisely
        what the pre-fix client did, raising `Failed to receive ACK. Got:
        b'$'`).

        Raises:
            GDBDesyncError: If the stub rejects the packet with `-`.
            GDBTimeoutError: If no ACK arrives in time.
        """
        while True:
            kind, payload = self._next_token()
            if kind == "ack":
                self._owed_ack = False
                return
            if kind == "nack":
                self._owed_ack = False
                self._owed_reply = False
                raise GDBDesyncError(f"GDB stub rejected {self._last_sent!r} with '-'")
            self._record_unsolicited(payload)

    def _next_reply(self, expect_stop_reply: bool) -> bytes:
        """Consume packets until one is an answer rather than an unprompted stop.

        Args:
            expect_stop_reply: True when the outstanding request was `c`,
                `s` or `?`, whose answer IS a stop reply.

        Returns:
            The answering packet's payload.

        Raises:
            GDBTimeoutError: If no such packet arrives in time.
            ConnectionError: If the peer closed the connection.
        """
        while True:
            kind, payload = self._next_token()
            if kind != "packet":
                # A stray ACK for something already accounted for.
                continue
            if not expect_stop_reply and looks_like_stop_reply(payload):
                self._record_unsolicited(payload)
                continue
            return payload

    def _resync(self) -> None:
        """Drain exactly what the stub still owes for an abandoned exchange.

        A timed-out request leaves the stub owing an ACK, a reply, or both.
        Those bytes still arrive: the pre-fix client read them as the answer
        to whatever it asked next and stayed one packet behind for the rest
        of its life, silently. How much is owed is exactly known -- this
        client never pipelines -- so the recovery is deterministic rather
        than a guess: read and discard precisely that much, first.

        Raises:
            GDBDesyncError: If the owed bytes do not arrive within the
                socket's timeout, or the connection drops while draining.
                The client is marked unusable at that point; where the
                stream sits can no longer be proven, and failing loudly on
                every later call is the only honest outcome.
        """
        if not (self._owed_ack or self._owed_reply):
            return
        abandoned = self._last_sent
        logger.warning(f"Resynchronising: draining the abandoned reply to {abandoned!r}")
        try:
            if self._owed_ack:
                self._await_ack()
            if self._owed_reply:
                # The abandoned request's own reply. If it was a resuming
                # packet that reply IS a stop reply, so it is kept rather
                # than skipped -- dropping it would hide that the CPU
                # stopped.
                payload = self._next_reply(expect_stop_reply=abandoned in RESUMING_PACKETS)
                self._owed_reply = False
                if looks_like_stop_reply(payload):
                    self._record_unsolicited(payload)
                else:
                    logger.warning(f"Discarded abandoned reply to {abandoned!r}: {payload!r}")
        except Exception as exc:
            self._unusable = (
                f"GDB stream position is unknown: the reply to {abandoned!r} was "
                f"abandoned and could not be drained ({exc!r}). This client is "
                f"permanently unusable -- open a new GDBClient."
            )
            raise GDBDesyncError(self._unusable) from exc

    def _send_packet(self, packet: bytes) -> None:
        """Send a GDB packet with checksum, resynchronising first if needed.

        Args:
            packet: The payload to frame and send.

        Raises:
            GDBDesyncError: If this client is unusable, if an earlier
                abandoned exchange cannot be drained, or if the stub rejects
                the packet with `-`.
            GDBTimeoutError: If the ACK does not arrive in time.
        """
        self._ensure_usable()
        self._resync()

        checksum = self._calculate_checksum(packet)
        self._write(b"$" + packet + b"#" + f"{checksum:02x}".encode())

        # Recorded BEFORE the ACK is awaited: a timeout in there must leave
        # behind an accurate record of what is still owed, because that
        # record is all the next request has to drain from.
        self._last_sent = packet
        self._owed_reply = True
        if not self._no_ack_mode:
            self._owed_ack = True
            self._await_ack()

    def _read_packet(self) -> bytes:
        """Read the answer to the packet just sent, verifying its checksum.

        A stop reply arriving here when the outstanding request was not `c`,
        `s` or `?` was sent unprompted; it goes to `pending_stops` and the
        read continues, rather than being handed back as the answer.

        Returns:
            The answering packet's payload.

        Raises:
            GDBDesyncError: If this client is already unusable.
            GDBTimeoutError: If no answer arrives in time.
            ConnectionError: If the peer closed the connection.
        """
        self._ensure_usable()
        payload = self._next_reply(expect_stop_reply=self._last_sent in RESUMING_PACKETS)
        self._owed_reply = False
        return payload

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
