# GDB RSP surface

Everything here is read from `src/debug/gdbserver.cpp` at commit `a4ff6a8cd`.
This is the complete set — there is no packet handling anywhere else.

## Framing

- Packets are `$<body>#<cs>`, `<cs>` = two lowercase hex digits, the sum of the
  body bytes mod 256 (`send_packet`, `gdbserver.cpp:265`).
- `has_complete_packet` (`:182`) accepts either a leading `0x03` byte or a
  full `$…#xx`. **`0x03` is only recognised as an interrupt when it is the
  first byte in the receive buffer** — a `0x03` arriving behind an incomplete
  packet is not seen as an interrupt.
- `extract_packet` (`:203`) discards everything before the first `$`, so
  stray `+`/`-` acks from the client are silently dropped.
- On checksum mismatch it sends `-` (when acking) and **drops the packet with
  no reply** (`:244-250`). On success it sends `+` (when acking) and returns
  the body.
- `send_packet` never waits for the client's ack and never retransmits
  (`:277-278`). The stub is fire-and-forget on output.
- `QStartNoAckMode` suppresses only *outbound* `+`/`-`. Incoming acks were
  already ignored either way.
- `noack_mode` resets to `false` on every accept and every disconnect
  (`try_accept` `:118`, `poll` `:143`, `stop` `:43`). **No-ack does not
  survive a reconnect** — a reconnecting client must renegotiate.

## Connection lifecycle

- `listen(server_fd, 1)` — one client at a time. `try_accept` runs only while
  `client_fd < 0`, so a second connection sits in the backlog until the first
  disconnects.
- If the interactive debugger is active at accept time, the stub writes the
  raw bytes `$E99#b7` and closes (`:104-112`). This is the only hand-written
  packet in the file; everything else goes through `send_packet`, which
  computes the checksum.
  **It read `$E99#b2` until `8bdc17b5`** — a wrong checksum (`'E'+'9'+'9'` is
  `0xb7`), so a checksum-verifying client such as real `gdb` rejected the
  rejection itself as a corrupt packet, and the one path that exists to say
  "the interactive debugger has the CPU" surfaced as a protocol fault instead.
  A unit test now scans this file for any `"$...#xx"` literal and verifies its
  checksum, so a future hand-written packet cannot reintroduce it.

## Dispatch order

`process_command` (`gdbserver.cpp:291`) tests in this order. Several tests are
**one-character prefix matches**, so a new packet sharing a first letter is
swallowed by the existing handler.

| # | Test | Match kind |
| --- | --- | --- |
| 1 | `"\x03"` | exact (interrupt marker) |
| 2 | `QStartNoAckMode` | exact |
| 3 | `vMustReplyEmpty` | exact |
| 4 | `?` | exact |
| 5 | `H…` | prefix |
| 6 | `p…` | prefix |
| 7 | `P…` | prefix |
| 8 | `g` | exact |
| 9 | `G…` | prefix |
| 10 | `m…` | prefix |
| 11 | `M…` | prefix |
| 12 | `Z…` / `z…` | prefix |
| 13 | `s…` | prefix |
| 14 | `c…` | prefix |
| 15 | `q…` | prefix |
| 16 | `vCont…` | 5-char prefix |
| 17 | `D` or `D;…` | exact / prefix |
| — | anything else | empty packet |

The whole block sits inside one `try`/`catch (const std::exception&)`
(`:305`, `:352-356`) that answers `E01`. This is not defensive style — the stub
runs on the emulation thread, so an escaping `std::invalid_argument` from
`std::stoul` would call `std::terminate` and take the emulator down.

---

## Packets

### `?` — halt reason

Reply `S05`. **Side effect: it stops the CPU** (`GDBAction::STOP`,
`gdbserver.cpp:314-318` → `debug.cpp:6246`). Querying the halt reason is not a
read-only operation here.

### `\x03` — interrupt

Bare byte, not a packet. Reply `S05`, CPU stops.

### `H<op><thread>` — set thread

Always `OK`. The operation and thread id are ignored entirely.

### `g` — read all registers

128 hex chars: 16 registers × 8. Order is
`EAX ECX EDX EBX ESP EBP ESI EDI EIP EFLAGS CS SS DS ES FS GS`
(`handle_read_registers`, `:393`; values from `DEBUG_GetRegister`,
`debug.cpp:6132`).

Each value goes through `swap32`, so the hex is **little-endian byte order**:
`EAX = 0x1234` is `34120000`. Segment registers are 16-bit values
zero-extended to 32 bits and then byte-swapped.

**Register 8 is `reg_eip` — an offset within CS, not a linear address.**
See SKILL.md §2. Advertised as `dosbox-x-eip-offset+`.

### `G<128 hex>` — write all registers

Writes `args.length()/8` registers in the order above. Always `OK`.

No validation: a short block writes only the registers it covers; a long block
walks past index 15, and `DEBUG_SetRegister`'s switch has no `default`, so
those writes are silently discarded (`debug.cpp:6159`).

### `p<n>` — read one register

`n` is parsed as **hex**. Reply is 8 hex chars, byte-swapped as for `g`.

No range check: `DEBUG_GetRegister` returns `0` for an unknown index, so
`p99` answers `00000000` instead of an error.

### `P<n>=<8 hex>` — write one register

`n` hex, value hex, both `std::stoul`.

- missing `=` → `E01` (`:420`)
- `n < 0 || n > 15` → `E01` (`:427`) — added deliberately, because
  `DEBUG_SetRegister` would otherwise silently discard the write and still
  answer `OK`
- otherwise `OK`

### `m<addr>,<len>` — read memory

Both fields hex. Missing comma → `E01`. Reply is `len × 2` hex chars.

**Addresses are LINEAR** — `DEBUG_ReadMemory` → `mem_readb_checked`
(`debug.cpp:6180`).

Two things `m` never does:
- **It never reports a bad address.** `DEBUG_ReadMemory` swallows the
  `mem_readb_checked` failure and returns `0`, so unmapped memory reads back as
  zeros indistinguishable from real zeros.
- **It does not enforce `PacketSize`.** Nothing caps `len` against the
  advertised `3fff`, so a large request produces an over-long packet.

### `M<addr>,<len>:<hex>` — write memory

Missing comma **or** colon → `E01`. Address hex, linear.

The `<len>` field is parsed off the wire but **never used** — the number of
bytes written is whatever `hex_decode` produces from the payload
(`handle_write_memory`, `:456`). Write failures from `mem_writeb_checked` are
ignored. Always `OK`.

### `Z<type>,<addr>,<kind>` / `z<type>,<addr>,<kind>` — breakpoints

- Needs two commas, else `E01` (`:479`).
- `<type>` is parsed as **decimal**. **Only type 0 (software breakpoint) is
  handled**; types 1-4 get an **empty packet** (`:486`), the RSP way of saying
  unsupported — despite `hwbreak+` in `qSupported`.
- `<addr>` is hex and **LINEAR**. `<kind>` is ignored.
- `Z0` → `DEBUG_SetBreakpoint(addr)`; `z0` → `DEBUG_RemoveBreakpoint(addr)`.
  `OK` on success, `E01` on failure.

`E01` here means one of two things:

1. **Protected mode.** Both functions refuse when
   `cpu.pmode && !(reg_flags & FLAG_VM)` (`debug.cpp:6286`, `:6295`).
   Breakpoints are stored as `AddBreakpoint(0, linear, false)`, and in
   protected mode `GetAddress(0, off)` routes through `LinMakeProt(0, off)`,
   which rejects selector 0 (selectors below 8 are never valid) and returns
   `mem_no_address`. The breakpoint would be stored at a useless location and
   could never fire, while `Z0` answered `OK`. **Protected-mode breakpoints are
   not implemented**; the refusal exists so the stub stops claiming success for
   something it silently dropped.
2. `z0` for an address with no matching breakpoint —
   `CBreakpoint::DeleteBreakpoint` returns false.

**A `Z0` set while the guest is free-running is INERT.**
`DEBUG_SetBreakpoint` calls `AddBreakpoint(…, false)`, which only appends to
`BPoints`; it never calls `Activate`. `CheckBreakpoint` requires `IsActive()`
(`debug.cpp:740`). Activation happens in exactly one place on the GDB path:
`GDBAction::CONTINUE` → `CBreakpoint::ActivateBreakpoints()`
(`debug.cpp:6242`). So the sequence is always **`Z0` then `c`**; a `Z0` on a
running guest with no following `c` will never fire.

### `s` / `s<addr>` — step

Returns `GDBAction::STEP`; **no immediate reply**. The `<addr>` form parses but
the address is **ignored** — the stub does not resume elsewhere.

`DEBUG_CheckGDBStep` (`debug.cpp:6216-6239`) exits HLT if needed, runs exactly
one instruction with `skipFirstInstruction`/`mustCompleteInstruction`, then
sends `S05` and sets `gdb_cpu_paused`.

### `c` / `c<addr>` — continue

Returns `GDBAction::CONTINUE`; **no immediate reply**. `<addr>` ignored.
Activates breakpoints and clears `gdb_cpu_paused` (`debug.cpp:6240-6244`).

The stop reply arrives later, from `DEBUG_Enable_Handler`
(`send_stop_reply(5)` at `debug.cpp:4860`) when a breakpoint fires.

### `vCont?` / `vCont;<action>`

- `vCont?` → `vCont;c;s;t`
- `vCont;c…` → CONTINUE
- `vCont;s…` → STEP
- anything else → empty packet

**`t` is advertised but not dispatched** (`handle_v_packets`, `:361-379`) — it
falls to the default and answers empty.

Only `cmd[6]` is examined. Thread suffixes (`vCont;c:1`) therefore work by
accident, and a multi-action list (`vCont;s:1;c`) honours only the first
action.

### `q…` queries (`handle_query`, `:501`)

| Request | Reply |
| --- | --- |
| `qSupported:<features>` | `PacketSize=3fff;swbreak+;hwbreak+;vContSupported+;QStartNoAckMode+;dosbox-x-linear-bp+;dosbox-x-eip-offset+` |
| `qfThreadInfo` | `m1` |
| `qsThreadInfo` | `l` |
| `qAttached…` | `1` |
| `qC` | empty |
| anything else | empty |

The `qSupported` test is `cmd.substr(0,10) == "Supported:"` — **it requires the
colon**. A bare `qSupported` with no feature list falls through to the default
and answers with an empty packet.

See SKILL.md §4 for what each advertised flag actually promises and which two
are inaccurate.

### `QStartNoAckMode`

`OK`, and outbound acks stop *after* this packet's own `+`.

### `vMustReplyEmpty`

Empty packet. Present so `gdb`'s probe behaves.

### `D` / `D;<pid>`

`OK`, then the socket is closed immediately and `poll()` reports
`GDBAction::DISCONNECT`.

---

## Deliberately unimplemented

Everything below falls through to the default `send_packet("")`:

`!` (extended mode), `R` (restart), `k` (kill), `T` (thread alive),
`X` (binary memory write), `Z1`-`Z4` / `z1`-`z4` (hardware and watchpoints),
`qXfer:*`, `qRcmd`, `qOffsets`, `qSymbol`, `qTStatus`, `qThreadExtraInfo`,
`QNonStop`, `vRun`, `vKill`, `vFile:*`, `vAttach`.

Also absent by design:

- **No `T` stop replies.** Only `S05` is ever sent (`send_stop_reply`, `:282`),
  and every call site passes signal 5. There are no `thread:`, `swbreak:`,
  `hwbreak:` or `watch:` annotations, and no signal other than SIGTRAP is ever
  reported — including for guest faults.
- **No thread model.** One fixed thread `1`; `H` accepts anything.
- **No protected-mode breakpoints** (see `Z0` above).
- **No register set beyond the 16 32-bit x86 GPRs/segments.** No FPU, no MMX/SSE,
  no `qXfer:features:read` target description — the client must assume the
  standard i386 layout.

## Error conventions

| Reply | Meaning |
| --- | --- |
| empty packet `$#00` | "I do not implement this." Never an error. |
| `E01` | Malformed packet (missing `,` or `:` or `=`), `P` index out of range, breakpoint refused (protected mode) or not found, **or any exception escaping a handler**. It is the only error code `process_command` produces, so it does not distinguish these. |
| `E99` | Interactive debugger active — sent raw at accept time, then the socket closes. The only hand-written packet; its checksum was wrong until `8bdc17b5` (see above). |

**Silence is not an error path.** These conditions produce success-looking
replies: unmapped `m` reads (zeros), failed `M` writes (`OK`), out-of-range `p`
(`00000000`), out-of-range indices inside `G` (`OK`). If you are adding
diagnostics, these are the four places where the stub currently lies by
omission.
