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

Reading `pending_stops` polls the socket itself, without blocking, so a stop
nobody asked for reaches the queue whether or not anything else is talking
GDB -- and `wait_for_stop` owns that poll loop. The poll is inert while
anything is owed, which is what keeps it from stealing a reply in flight.

There is deliberately no read-retry loop -- see `frames.py` for why retrying
masks this fault instead of surfacing it.
"""

import binascii
import contextlib
import socket
import time
from collections import deque

from loguru import logger

from .addressing import linear_pc, parse_address
from .utils import parse_x86_address

__all__ = [
    "DEFAULT_TIMEOUT",
    "GDBClient",
    "GDBDesyncError",
    "GDBTimeoutError",
    "IncompatibleStubError",
    "LINEAR_BREAKPOINTS_CAPABILITY",
    "REGISTER_NAMES",
    "looks_like_stop_reply",
]

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
        # The protocol is strictly request/response with tiny packets, the
        # workload Nagle punishes worst: each side holds a small write waiting
        # for an ACK the peer has delayed. Measured ~82ms per round-trip with
        # it on, ~41ms with only this side fixed -- one ~40ms stall per
        # direction, so the stub has to set it too. There is no batching here
        # to preserve, so there is no tradeoff being made.
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
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

        Reading it DOES read the socket, without blocking, so that asking
        the question also answers it. Before that, the queue only filled as
        a side effect of some other request passing through the framing
        layer: an unsolicited `S05` sat unread in the kernel buffer, and a
        caller polling this property directly never saw a stop that had
        genuinely happened (lokkju/dbxdebug#18). The poll is skipped while
        the stub owes a reply to a request already on the wire -- see
        `_drain_unsolicited` -- so it can never take bytes that belong to
        someone else's exchange.

        Returns:
            The queued stop reply payloads, oldest first, at most
            `MAX_PENDING_STOPS` of them.
        """
        self._drain_unsolicited()
        return tuple(self._pending_stops)

    def take_pending_stops(self) -> list[bytes]:
        """Remove and return every queued unrequested stop reply.

        Reads the socket first, without blocking, exactly as `pending_stops`
        does -- a stop that has arrived but not yet been parsed is returned
        here rather than requiring some unrelated read to shake it loose.

        Returns:
            The queued payloads, oldest first. The queue is left empty.
        """
        self._drain_unsolicited()
        drained = list(self._pending_stops)
        self._pending_stops.clear()
        return drained

    def wait_for_stop(self, timeout: float = 30.0, poll: float = 0.05) -> bytes | None:
        """Poll until the CPU stops of its own accord, or `timeout` elapses.

        Every consumer that arms a breakpoint needs this loop, and the one
        that wrote its own got it wrong in a way nothing reported: it looped
        on a queue that filled only when something else read the socket, so
        it spun on a stop that had already happened and its caller concluded
        the breakpoint had never fired (lokkju/dbxdebug#18). Owning the loop
        here means there is one correct version of it.

        This is for stops NOBODY asked for -- a QMP break-on-exec hit, say.
        When the point is to run until the next breakpoint, `continue_execution`
        sends `c` and waits for the reply that answers it; this does not
        resume anything.

        Args:
            timeout: Maximum wall seconds to wait. 0 polls exactly once,
                which is the cheap "has anything stopped?" question.
            poll: Wall seconds to sleep between polls.

        Returns:
            The oldest queued stop reply, removed from the queue, or None if
            none arrived in time. Any stop behind it stays queued.

        Raises:
            GDBDesyncError: If this client is unusable, or if the stub still
                owes a reply to a request already on the wire. The socket
                cannot be polled in that state without stealing that
                request's own bytes, so waiting here would spin forever --
                the exact failure this method exists to prevent. Finish or
                abandon that exchange first; the next `_send_packet` drains
                it.
        """
        self._ensure_usable()
        if self._owed_ack or self._owed_reply:
            raise GDBDesyncError(
                f"Cannot wait for a stop while the stub still owes a reply to "
                f"{self._last_sent!r}: polling the socket now would consume that "
                f"request's own bytes. Complete that exchange first -- the next "
                f"request drains an abandoned one."
            )
        deadline = time.monotonic() + timeout
        while True:
            self._drain_unsolicited()
            if self._pending_stops:
                return self._pending_stops.popleft()
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll)

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

    def _token_from_buffer(self) -> tuple[str, bytes] | None:
        """Consume the next complete protocol token already in the buffer.

        Splitting this out of `_next_token` is what lets the stream be read
        without blocking: a token is only ever consumed once all of its bytes
        are in hand, so a half-arrived `$S0` is left where it is rather than
        being mangled by a reader that could not wait for the rest.

        Bytes that are neither an ACK, a NACK, nor the start of a packet are
        discarded, matching what the previous parser did with them.

        Returns:
            `("ack", b"")`, `("nack", b"")`, or `("packet", payload)` with
            the payload's checksum already verified and acknowledged; None
            when the buffer holds no complete token, leaving the incomplete
            bytes buffered.
        """
        while True:
            if not self.buffer:
                return None

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
                return None

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

    def _next_token(self) -> tuple[str, bytes]:
        """Consume the next protocol token, blocking until one is complete.

        Returns:
            `("ack", b"")`, `("nack", b"")`, or `("packet", payload)` with
            the payload's checksum already verified and acknowledged.

        Raises:
            GDBTimeoutError: If the stream stalls mid-token.
            ConnectionError: If the peer closed the connection.
        """
        while True:
            token = self._token_from_buffer()
            if token is not None:
                return token
            self._recv()

    def _recv_ready(self) -> bool:
        """Append whatever is ALREADY waiting on the socket, without blocking.

        Distinct from `_recv`, which waits for the stub to answer something
        that was asked. Nothing here was asked for, so waiting would be
        wrong: an empty kernel buffer is the ordinary case and must cost
        nothing.

        Returns:
            True if bytes were appended to the read buffer, False if the
            socket had none ready, the peer closed, or it is no longer
            usable. Never raises -- callers are queue accessors, and a
            connection that has gone away is a fact about the next real
            request, not something to surface from reading a queue.
        """
        if self.sock is None:
            return False
        previous = self.sock.gettimeout()
        try:
            # 0.0 puts the socket in non-blocking mode, so an empty kernel
            # buffer raises BlockingIOError instead of waiting. `recv` on a
            # socket with data ready never blocks either way.
            self.sock.settimeout(0.0)
            chunk = self.sock.recv(4096)
        except (BlockingIOError, TimeoutError):
            return False
        except OSError as exc:
            logger.debug(f"Non-blocking poll of the GDB socket failed: {exc!r}")
            return False
        finally:
            # Restored before anything else touches the socket -- the ACK
            # `_token_from_buffer` writes must go out on a blocking socket.
            with contextlib.suppress(OSError):  # the socket may have gone
                self.sock.settimeout(previous)
        if not chunk:
            return False
        self.buffer += chunk
        return True

    def _drain_unsolicited(self) -> None:
        """Read anything the stub sent unprompted, without blocking.

        This is what makes the stop queue self-servicing. The queue used to
        fill only as a side effect of some OTHER request passing through the
        framing layer, so a caller polling it directly never saw a stop that
        had genuinely happened and spun forever (lokkju/dbxdebug#18).

        It is a no-op -- deliberately, not defensively -- whenever the stub
        still owes bytes for a request already on the wire. Those bytes
        belong to that exchange: consuming an ACK here would strand
        `_await_ack`, and consuming a reply here would hand the request's own
        answer to the queue. The owed state is only ever set between
        `_send_packet` and the matching `_read_packet` on the same thread, so
        a caller between requests never trips this; a caller that abandoned
        an exchange (a `GDBTimeoutError` it swallowed) does, and gets nothing
        new rather than a corrupted stream. `wait_for_stop` reports that case
        rather than spinning quietly through it.

        Only complete tokens are consumed, so a stop reply that is still
        arriving stays buffered and is picked up whole by the next poll or by
        the next real read.
        """
        if self._unusable is not None or self.sock is None:
            return
        if self._owed_ack or self._owed_reply:
            logger.debug(
                f"Not polling for unsolicited stops: the stub still owes a reply "
                f"to {self._last_sent!r}"
            )
            return
        try:
            while True:
                while (token := self._token_from_buffer()) is not None:
                    kind, payload = token
                    if kind != "packet":
                        # A stray ACK or NACK for an exchange already closed
                        # out. Nothing is owed, so it answers nothing.
                        continue
                    if looks_like_stop_reply(payload):
                        self._record_unsolicited(payload)
                    else:
                        logger.warning(f"Discarded an unrequested non-stop GDB packet: {payload!r}")
                if not self._recv_ready():
                    return
        except OSError as exc:  # a write of the ACK on a socket that has gone
            logger.debug(f"Stopped polling for unsolicited stops: {exc!r}")

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

    def resume(self) -> None:
        """Let the CPU run again, without waiting for the next stop.

        `continue_execution` sends the same `c` packet and then BLOCKS until
        a stop reply comes back, which is right for "run to the next
        breakpoint" and wrong for "put the guest back the way I found it".
        With no breakpoint armed nothing ever answers, so that call burns
        the socket's whole timeout and then raises.

        The stub really does owe nothing here: DOSBox-X's `c` handler clears
        its paused flag and returns without sending a packet, so the only
        reply that can ever follow is the stop reply for a LATER stop --
        which by then is unsolicited, and is queued in `pending_stops` where
        it belongs. This method records that by clearing `_owed_reply` after
        the send, so the next request does not try to drain a reply that is
        not coming (which would time out and mark this client unusable).

        Use `continue_execution` when the point IS to wait for the stop.
        """
        self._send_packet(b"c")
        # Deliberately after the send: `_send_packet` sets `_owed_reply`
        # before awaiting the ACK precisely so that a timeout in there
        # leaves an accurate record. Only once the ACK is in hand is it
        # true that the stub owes nothing further.
        self._owed_reply = False

    def close(self) -> None:
        """Close the connection."""
        if self.sock:
            self.sock.close()
            self.sock = None  # type: ignore

    def __enter__(self) -> "GDBClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
