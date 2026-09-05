# Migrating onto dbxdebug

For a consumer that already drives DOSBox-X through its own launcher and its
own copy of the `dosbox_debug` helper clients, and now wants to use this
package instead. It is written for code that works today: every section says
what breaks, how it fails, and what to write instead.

Read section 2 first even if you read nothing else. It is the only change on
this list that can be wrong at runtime without producing an error.

## 0. What breaks, at a glance

| Change | How it fails | Fix |
|---|---|---|
| Breakpoints take a linear address | Silently, above 64 KB, on old code that packed `(seg << 16) \| off` | `addressing.linear(seg, off)`, or `DosboxSession.set_breakpoint(seg, off)` |
| `addressing.bp_addr` raises | Loudly, at the first call | Delete the call; use `linear` |
| `read_registers()["eip"]` is an offset in CS | **Silently. No exception, no wrong-looking value** | `gdb.linear_pc()` |
| Client method names differ | Loudly, `AttributeError` | Section 3's tables |
| `GDBClient` demands a vendor capability | Loudly, `IncompatibleStubError` at connect | Rebuild the emulator, or `require_capabilities=False` |
| No read timeout, no resynchronisation | Hangs forever, or returns the previous reply's bytes | Section 5 |

## 1. Imports

The legacy launcher was a module inside the consumer: a hand-rolled
`subprocess.Popen` around `dosbox-x`, its own teardown, its own `bp_addr`
helper, wrapping the `dosbox_debug` GDB and QMP clients that ship with the
DOSBox-X fork's integration tests (`tests/integration/dosbox_debug.py` in the
emulator repo). All of that is now a dependency:

```bash
uv add dbxdebug
```

**Check what you got before you write any of the code below.** The version
published on PyPI at the time of writing is 0.2.1, and it ships the original
client surface only: no `session`, no `addressing`, no `frames`, no
`registry`, no `doctor`. Everything this document describes needs a release
newer than that, which has not been published yet. Until it is, depend on the
repository directly (`uv add --editable ../dbxdebug`, or a git dependency),
and confirm with:

```bash
uv run python -c "import dbxdebug.addressing, dbxdebug.session, dbxdebug.frames; print('ok')"
```

An `ImportError` there means you are on a release that predates this
migration, and the rest of this document does not apply to it yet.

Then delete the vendored copy. Nothing in the consumer should still import
`dosbox_debug`.

| What you had | Where it lives now |
|---|---|
| The launcher class (`Popen`, ports, cleanup) | `from dbxdebug.session import DosboxSession` |
| `dosbox_debug.GDBClient` | `from dbxdebug import GDBClient` |
| `dosbox_debug.QMPClient`, `QMPError` | `from dbxdebug import QMPClient, QMPError` |
| `dosbox_debug.GDBError` | No equivalent: see section 3 |
| The launcher's `bp_addr` | `from dbxdebug.addressing import linear` (and see section 2.2) |
| Screen decoding on the GDB client | `DosboxSession.screen_lines()`, or `from dbxdebug import DOSVideoTools` |
| Frame walking, if you had it | `from dbxdebug.frames import walk_frames, steps_out` |
| Finding the emulator binary | `from dbxdebug.paths import find_dosbox_x`, or `$DBXDEBUG_DOSBOX` |
| Nothing: this is new | `from dbxdebug.registry import list_sessions`; the `dbxdebug session` and `dbxdebug doctor` CLI |

### The surface is not tidy, and this is worth knowing before you write imports

`dbxdebug/__init__.py` exports the original client surface only. The newer
modules -- the session, the registry, the addressing helpers, the frame
walker -- are importable from their own modules and nowhere else. So the
package's primary entry point is the one thing you cannot import from the
package root:

```python
>>> from dbxdebug import GDBClient, QMPClient, DOSVideoTools   # fine
>>> from dbxdebug import DosboxSession
ImportError: cannot import name 'DosboxSession' from 'dbxdebug'
>>> from dbxdebug.session import DosboxSession                 # this is the way
```

This is self-consistent (no newer module is exported, so there is no
arbitrary line) but it is an odd shape, and it is an open question, not a
settled convention: lokkju/dbxdebug#7. Import from the modules for now. If
the re-exports land, module-level imports keep working, so nothing written
today has to be rewritten.

### The launcher itself

`DosboxSession` replaces the hand-rolled launch. It allocates ephemeral
ports instead of the fixed 2159/4444, isolates each run in its own workdir,
records the process in a registry so an orphan can be found and reaped
later, and tears everything down on context exit -- including on an
exception, and again from an `atexit` hook and from SIGINT/SIGTERM handlers,
so only a SIGKILL of the owner can leave a stray emulator behind (which is
what `dbxdebug session reap` is for).

```python
from dbxdebug.session import DosboxSession

with DosboxSession(mounts={"c": "/path/to/host/dir"}, autoexec=["c:"]) as session:
    session.qmp.type_text("PROG\r")
    session.wait_for_text("READY", timeout=30)
    lines = session.screen_lines()
```

Two things a migrating launcher usually gets wrong here:

* `program=` is recorded, not auto-run. A caller arming break-on-exec has to
  arm before the exec happens, so the session never runs it for you.
* `boot_settle` (default 2.5 s) is the sleep the old launcher had between
  spawn and first connect, and it is load-bearing. The debug ports accept as
  soon as DOSBox-X opens its listeners, which is long before DOS has booted;
  keystrokes sent into that window are simply lost. If you read the screen
  immediately after `start()`, call `session.assert_screen_readable()`, which
  fails on a blank screen and on one still showing the DOSBox-X banner.

## 2. Addressing

This is the consequential part. Three separate changes, one of which has no
runtime signal at all.

### 2.1 Breakpoints take a LINEAR address

`GDBClient.set_breakpoint` and `remove_breakpoint` send `Z0`/`z0` with a
linear address -- `seg * 16 + off`, the same encoding `m`/`M` already used.
The legacy `bp_addr` packed a far pointer, `(seg << 16) | off`.

```python
# BEFORE -- packed far pointer
gdb.set_breakpoint(bp_addr(seg, off))          # (seg << 16) | off

# AFTER -- linear, any of these
from dbxdebug.addressing import linear
gdb.set_breakpoint(linear(seg, off))           # seg * 16 + off
gdb.set_breakpoint(f"{seg:04x}:{off:04x}")     # the client parses seg:off
session.set_breakpoint(seg, off)               # section 2.4
```

`set_breakpoint` also accepts a bare hex string, and a bare string with no
`0x` prefix is read as **hex, not decimal**: `"1000"` means `0x1000`.

### 2.2 `addressing.bp_addr` raises instead of encoding

`bp_addr` still exists, and it now does exactly one thing:

```python
>>> from dbxdebug.addressing import bp_addr
>>> bp_addr(0x0824, 0x5A90)
PackedAddressError: bp_addr(0x824, 0x5a90) refused: breakpoints take a linear
address, not a packed seg:off pair; use linear(seg, off) instead
```

This is deliberate, and it is the reason a `bp_addr` shim exists at all
rather than the name simply being deleted. A packed value and a linear value
are both plausible integers, and the stub answers `OK` to either, so a call
site you missed cannot be told apart from one you converted by looking at
the response. Raising turns "a breakpoint that never fires, months from now"
into "an `ImportError` or a `PackedAddressError` the first time this line
runs". Grep for `bp_addr` in the consumer, delete every call, and let the
ones you missed fail on contact.

A second guard backs it up. Every address `GDBClient` sends goes through
`addressing.parse_address`, which rejects an integer at or above
`REAL_MODE_CEILING` (`0x110000`), because no real-mode `seg * 16 + off` can
reach it -- the highest reachable real-mode address, including the HMA, is
`0xFFFF0 + 0xFFFF == 0x10FFEF`:

```python
>>> from dbxdebug.addressing import parse_address
>>> parse_address((0x1234 << 16) | 0x0100)
PackedAddressError: address 0x12340100 looks like a packed far pointer
(seg=0x1234, off=0x100); DOSBox-X's GDB stub expects a linear address
```

**Its blind spot, stated plainly:** a packed pair whose segment is small
lands below the ceiling and passes. `0010:0000` packs to `0x00100000`, which
is accepted and means linear `0x100000`, not offset `0x100` in segment
`0x10`. Real DOS programs load well above segment 0, so the realistic
packing is caught, but do not treat the guard as a proof that a call site is
converted. The grep is the proof.

### 2.3 `read_registers()["eip"]` is an OFFSET within CS

**This is the change with no runtime signal.** Old code that used `eip` as a
linear address keeps running, keeps producing addresses, and is wrong.

The DOSBox-X stub used to return `SegPhys(cs) + reg_eip` from `g` -- a linear
address in register 8 -- and wrote `reg_eip` verbatim on `G`, so a `g`/`G`
round-trip silently moved the program counter. Current builds report register
8 as EIP, an offset within CS, and advertise `dosbox-x-eip-offset+` to say so.

```python
# BEFORE -- eip used as a linear address
pc = gdb.read_registers()["eip"]
code = gdb.read_memory(pc, 16)

# AFTER
pc = gdb.linear_pc()                    # cs * 16 + eip
code = gdb.read_memory(pc, 16)
```

`linear_pc()` reads the registers itself. If you already have a register
list, `addressing.linear_pc(registers)` does the arithmetic on the raw list
from `read_register_list()`; it indexes `CS_INDEX` (10) and `EIP_INDEX` (8),
so it takes the **list**, not the dict.

One accident of the type change helps here. The legacy clients returned a
`Registers` dataclass supporting both `regs.eip` and `regs["eip"]`; this
package returns a plain `dict[str, int]`. So every attribute-style access
breaks loudly with `AttributeError`, and only the subscript style is silent.
Grep for both, plus `read_register(8)`, which is the same offset by index.

### 2.4 `DosboxSession.set_breakpoint(seg, off)` does the conversion

The session-level helper is the closest thing to the old call shape, and it
computes the linear address for you:

```python
assert session.set_breakpoint(seg, off)       # -> gdb.set_breakpoint(linear(seg, off))
...
assert session.remove_breakpoint(seg, off)
```

Both return the stub's acknowledgement. Assert on it: `Z0` answering
anything but `OK` is how a protected-mode breakpoint refusal reaches you.

### 2.5 Why the packed form ever appeared to work

Old stubs split the `Z0`/`z0` argument as a far pointer, `seg = addr >> 16`,
and then computed `seg * 16 + off` anyway. So there were two conventions in
play, and three of the four combinations agree:

| Caller sends | Old stub stores | Current stub stores |
|---|---|---|
| linear `0xDCD0` (`0824:5A90`, under 64 KB) | `0xDCD0` correct | `0xDCD0` correct |
| linear `0x12440` (`1234:0100`, over 64 KB) | **`0x2450` wrong** | `0x12440` correct |
| packed `0x12340100` | `0x12440` correct | **`0x12340100` wrong** (now refused) |

A packed caller against an old stub was correct at any address, which is why
the packing convention survived. A linear caller against an old stub was
correct too, as long as the address stayed under `0x10000`: with `addr >> 16`
equal to zero, the two readings of the same wire value coincide exactly. DOS
programs load low, breakpoints went in low, and the bug stayed invisible for
years.

Above 64 KB the breakpoint went in at the mangled location and never fired.
That much was true of every build. On some builds it did more than not fire,
and this part depends on how the emulator was compiled:

* **Plain `C_DEBUG` builds (`C_HEAVY_DEBUG` off).** The stub stores the
  mangled location and arms it on the next continue, and arming a physical
  breakpoint writes an `0xCC` trap byte into guest memory there, saving the
  displaced byte (`CBreakpoint::Activate`, `src/debug/debug.cpp:632-671`, the
  whole body inside `#if !C_HEAVY_DEBUG`). A wrong location therefore means a
  corrupted guest byte somewhere unrelated: in the case above, `0x2450`, in
  the middle of low DOS memory.
* **Heavy-debug builds (`C_HEAVY_DEBUG 1`, what `./build-debug` produces and
  what this project's `config.h` sets).** Nothing is written to guest memory
  at all; `DEBUG_HeavyIsBreakpoint` checks breakpoints per instruction. The
  same bug here is only a breakpoint that never fires.

So the silent non-firing hit everyone, and the memory corruption hit only
consumers running a non-heavy-debug build -- which is not a thing a consumer
knew it was depending on.

The current stub also refuses outright rather than lying when it cannot honour
a breakpoint: in protected mode it answers `E01` instead of `OK`.

## 3. Client method names

Mapped from `dosbox_debug`'s clients as they exist today. An older vendored
copy may differ further -- in particular it will not have the
`PackedAddressError` guard, so it will happily send a packed address.

### GDB

| `dosbox_debug.GDBClient` | `dbxdebug.GDBClient` | Notes |
|---|---|---|
| `GDBClient(host, port, timeout=5.0)` then `.connect()` | `GDBClient(host, port, require_capabilities=True)` | Connects and completes the `qSupported` handshake in `__init__`. **No `timeout` parameter at all** (section 5) |
| `connect()` | -- | Constructor connects |
| `close()` (sends `D` first) | `close()` | No detach packet. The stub treats a dropped connection as a resume, so the guest is not stranded |
| `read_registers() -> Registers` | `read_registers() -> dict[str, int]` | Dataclass gone; `regs.eip` no longer works, `regs["eip"]` does. **Meaning changed: section 2.3** |
| -- | `read_register_list() -> list[int]` | Raw `g` order, for `addressing.linear_pc` |
| -- | `linear_pc() -> int` | New. `cs * 16 + eip` |
| `read_register(n)` | `read_register(n)` | Same |
| -- | `write_register(index, value)` | New. `P` packet |
| `read_memory(addr, size)` | `read_memory(address, length)` | Raises `MemoryError` on an `E` reply instead of `GDBError`; validates the address (section 2.2) |
| `write_memory(...) -> bool` | `write_memory(...) -> None` | Raises `MemoryError` on failure instead of returning `False` |
| `set_breakpoint(addr) -> bool` | `set_breakpoint(address) -> bool` | Same shape, **different address encoding: section 2.1** |
| `remove_breakpoint(addr) -> bool` | `remove_breakpoint(address) -> bool` | As above |
| `step() -> str` | `step() -> bytes` | Every packet-returning method now returns `bytes` |
| `continue_() -> str` | `continue_execution() -> bytes` | Renamed |
| `halt() -> str` (sends `\x03`) | `halt() -> bytes` (sends `?`) | Different packet, same effect against this stub: both send a stop reply and pause the CPU |
| `query_halt_reason() -> str` (sends `?`) | `halt() -> bytes` | Collapsed into `halt()` |
| `wait_for_stop(timeout)` | -- | No equivalent. Poll `qmp.query_status()["running"]`, on the QMP connection |
| `detach()` | -- | No equivalent; `close()` only |
| `enable_no_ack_mode()` | `enable_no_ack_mode()` | Present, and never called by the package. Sessions run in ACK mode |
| `screen_raw()`, `screen_dump()`, `screen_line()`, `screen_dump_with_ticks()`, `screen_debug()`, `read_video_mode()`, `read_timer_ticks()` | Moved off the GDB client to `DOSVideoTools`, plus `DosboxSession.screen_lines()` | Signatures differ: `DOSVideoTools.screen_dump(page=1)` takes a page, not `(width, height)`; `session.screen_lines(width=80, height=25)` takes the geometry. `screen_line()` has no equivalent -- index the list |
| `GDBError` | `MemoryError`, `ConnectionError`, `IncompatibleStubError` | There is no single client exception type to catch |

Two things the table cannot show.

**Screen text is decoded differently.** The legacy `screen_dump`
right-stripped each line and replaced non-printable characters with spaces;
neither `session.screen_lines()` nor `DOSVideoTools.screen_dump()` strips,
and both pass non-printables through as-is (only `0x00` becomes a space). A
consumer comparing screen text against stored fixtures will see the
difference.

**`DOSVideoTools` owns or borrows its GDB connection.** `DOSVideoTools(host,
port)` constructs a `GDBClient` of its own, which it closes. The emulator's
stub serves one client at a time -- it only accepts a new connection while it
has none -- so pointing a second one at a session that already has a connected
client gets a TCP connection the stub never services, and the constructor
blocks in the `qSupported` handshake with no timeout (5.1). Pass
`DOSVideoTools(gdb=session.gdb)` to borrow the session's client instead; the
borrower never closes what it did not open, so the session stays its owner.
`gdb=` cannot be combined with `host`/`port` -- that raises `ValueError`
rather than quietly ignoring the port you named (lokkju/dbxdebug#11).
`session.screen_lines()` and `DOSVideoTools.screen_dump()` now share one
decode, `video.decode_text_screen`, so they can no longer drift; that they
were two loops was the first item of lokkju/dbxdebug#7.

### QMP

| `dosbox_debug.QMPClient` | `dbxdebug.QMPClient` | Notes |
|---|---|---|
| `QMPClient(host, port, timeout=5.0)` then `.connect()` | `QMPClient(host, port)` | Connects, reads the greeting, negotiates `qmp_capabilities` in `__init__`. No timeout parameter |
| `send_key(keys, hold_time=100) -> dict` | `send_key(keys, hold_time=100) -> None` | Same wire command |
| -- | `send_key_dbx(keys: list[DBX_KEY], ...)` | New. Enum instead of qcode strings |
| `key_down(key) -> dict` / `key_up(key) -> dict` | `key_down(key) -> None` / `key_up(key) -> None` | Same `input-send-event` payload |
| `key_press(key, hold_time=0.1) -> dict` | `key_press(key, hold_time=0.05) -> None` | **Default hold time halved** |
| `type_text(text, delay=0.1)` | `type_text(text, delay=0.05)` | **Default delay halved.** Unmapped characters are logged and skipped rather than silently skipped; `\r`, `\n`, `\t`, space and shifted punctuation map the same |
| `input_send_event(events) -> dict` | -- | No raw passthrough. Use `key_down`/`key_up` |
| `query_commands() -> list` | `query_commands() -> list[str]` | Same |
| `query_status() -> dict` | `query_status() -> dict` | **Returns the `return` payload, not the envelope**: `resp["return"]["running"]` becomes `status["running"]` |
| `stop()`, `cont()`, `debug_break_on_exec(enabled)` | Same names | Same unwrapping change as `query_status` |
| -- | `memdump`, `screendump`, `savestate`, `loadstate`, `system_reset`, `quit` | New. `quit` is accepted but does not quit the emulator, and is not listed by `query_commands()` |
| `QMPError` | `QMPError` | Same name, different module |

## 4. The capability handshake

`GDBClient.__init__` sends `qSupported` and, by default, refuses to proceed
unless the reply contains `dosbox-x-linear-bp+`:

```python
IncompatibleStubError: GDB stub does not advertise dosbox-x-linear-bp+: this
build splits Z0/z0's address as a packed far pointer (seg = addr >> 16), so a
breakpoint set above 64 KB will answer OK and never fire. Pass
require_capabilities=False to GDBClient to proceed against this build anyway.
```

This exists because neither semantics change is detectable by probing: `Z0`
answers `OK` under either reading, and both `eip` conventions produce a
plausible integer. The advertisement is the only signal, so the client
treats it as load-bearing.

Connecting to an older emulator build, in order of preference:

1. **Rebuild the emulator.** A current build advertises
   `dosbox-x-linear-bp+;dosbox-x-eip-offset+`; `dbxdebug doctor` checks that
   the binary it will launch has remote debugging compiled in at all.
2. **Opt out explicitly, and pay for it.** `GDBClient(require_capabilities=False)`
   connects to anything. Against such a build you must pack breakpoint
   addresses as `(seg << 16) | off` again, and treat `eip` as linear again --
   the exact code this migration is deleting. Confine it to one adapter, do
   not thread the flag through the consumer.
3. `client.require_linear_breakpoints()` re-runs the check by hand on a
   client connected with the flag off, if you want to branch on it.

`DosboxSession` builds its clients with the default, so a session against an
old build fails at `start()` rather than at the first breakpoint.

## 5. Hazards to plan for

Two open defects in `GDBClient`. Both are reproduced, both have live tests
pinning today's behaviour, and neither is fixed. Plan around them.

### 5.1 No read timeout: an unanswered packet hangs forever (lokkju/dbxdebug#4)

`GDBClient` never calls `settimeout`, so its socket is in blocking mode with
no deadline. Any packet the stub does not answer hangs the caller
permanently, with no diagnostic.

This is easy to reach by accident, because the two protocols interact: while
the emulator is QMP-stopped, the GDB stub does not answer at all (it is
polled from the emulation thread). So `qmp.stop()` followed by any GDB
request is a deadlock, and `qmp.stop()` followed by a GDB request is a
natural thing to write.

Until it is fixed, arm the socket yourself, immediately after the session
starts. This is what the package's own live suite does:

```python
session.start()
if session.gdb is not None and session.gdb.sock is not None:
    session.gdb.sock.settimeout(30.0)
```

That converts the hang into `TimeoutError` -- which lands you in 5.2, so read
on before you decide what to catch.

### 5.2 A disturbed stream is never resynchronised (lokkju/dbxdebug#5)

`GDBClient` assumes strict request/response and never puts the stream back.
Once a reply is left unread, every later request returns the **previous**
request's payload, silently and permanently. There are two triggers, both
confirmed against a live build.

**Trigger 1 -- an unsolicited stop reply.** QMP break-on-exec arms *and
immediately activates* a breakpoint at the next program's entry point,
whether or not any GDB client asked to continue. When it hits, the stub sends
a `$S05#b8` nobody requested. The client's next `_send_packet` reads that `$`
where it expects the `+` ACK for its own packet:

```
break-on-exec armed and a program typed; hammering `m` for 12s
read 1: RAISED ConnectionError: Failed to receive ACK. Got: b'$'
```

Note that this one at least raises. A `Z0` breakpoint armed while
free-running is inert and is *not* a trigger -- activation only happens on
continue.

**Trigger 2 -- a timed-out request.** The abandoned reply stays in the
stream and is handed to the next request:

```
baseline m 0x400,8    = f803f80200000000
baseline m 0xFFFF0,16 = ea5be000f030312f30312f393200fc55
m 0xFFFF0,16 timed out after 5.03s (request left unanswered)
next m 0x400,8  -> 16 bytes: ea5be000f030312f30312f393200fc55   <-- PREVIOUS request's payload
then m 0x400,8  ->  8 bytes: f803f80200000000
```

Eight bytes were asked for and sixteen arrived, from a different address, and
nothing reports it. A consumer that does not compare the returned length
against the requested one gets no signal at all, and one that slices the
result to the length it expected turns wrong bytes into plausible ones.

What to do until this is fixed:

* Keep GDB traffic strictly serialised, one request per reply, on one thread.
* Treat `TimeoutError` from a GDB call as fatal to that connection, not as
  something to retry. Tear the session down and start another.
* If you arm `debug_break_on_exec`, assume the GDB connection is spent once
  the break fires. Poll `qmp.query_status()` on the QMP connection (a
  separate socket, undisturbed) to learn that the CPU stopped.
* Check the length of every `read_memory` result against what you asked for.
  It is the one cheap symptom that is visible from outside.

**Do not paper over this with a read-retry loop.** A "read the same bytes
until two consecutive reads agree" helper is the obvious workaround and it is
the wrong one twice over: it doubles the round-trips on every read, and two
identical consecutive requests mask a one-packet lag *perfectly*, so it would
look like it worked whether or not the stream had shifted. It converts a
detectable protocol fault into an undetectable one. The fix belongs in the
client's packet layer.

## 6. Migration checklist

Work through it in order. Each step is a thing you can finish and check.

1. **Add the dependency and delete the vendored clients.** Add `dbxdebug` --
   from the repository until a release newer than 0.2.1 is published (see
   section 1) -- then remove the consumer's copy of `dosbox_debug` and its
   launcher module. `grep -rn 'dosbox_debug' .` should come back empty, and
   `uv run python -c "import dbxdebug.addressing, dbxdebug.session; print('ok')"`
   should print `ok`.
2. **Check the host.** `uv run dbxdebug doctor`. It never launches anything;
   it reports whether a binary is found, whether it has remote debugging
   compiled in, whether the registry is writable, and whether any orphaned
   session is already lying around. Set `DBXDEBUG_DOSBOX` to pick a specific
   build.
3. **Replace the launcher with `DosboxSession`.** Delete the hand-rolled
   `Popen`, the fixed 2159/4444 ports, the manual cleanup and any
   process-killing by name. Keep the boot wait: it is `boot_settle`.
4. **Find every packed address.**
   `grep -rnE 'bp_addr|<< *16|>> *16' .` Convert each to
   `addressing.linear(seg, off)`, or to `session.set_breakpoint(seg, off)`.
   Delete the consumer's own `bp_addr`; the one in this package raises.
5. **Find every use of EIP.** `grep -rnE '\.eip\b|\["eip"\]|read_register\(8\)' .`
   Attribute access already fails loudly. Every remaining subscript use that
   feeds an address must become `gdb.linear_pc()`. This step has no test that
   will fail for you -- do it by reading.
6. **Rename the client calls** using section 3. `continue_()` ->
   `continue_execution()`, `query_halt_reason()` -> `halt()`, screen helpers
   off the GDB client, and unwrap the QMP `return` envelope.
7. **Decide the capability policy.** Default (refuse an old stub) unless you
   deliberately drive one, in which case confine `require_capabilities=False`
   to a single adapter.
8. **Arm a socket timeout** on `session.gdb.sock` right after `start()`
   (5.1), and make a timeout tear the session down rather than retry (5.2).
9. **Verify against a real emulator.** Run this package's live suite, which
   is exactly the evidence a consumer needs:

   ```bash
   uv run pytest -m integration tests/integration -v
   ```

   Two tests in it are the ones this migration turns on:
   `test_breakpoint_above_64k_fires_where_it_was_set` sets a breakpoint at the
   INT 08h handler -- read from the guest, above 64 KB -- and asserts the stub
   stops there, which a packed-address stub could never do;
   `test_eip_is_an_offset_within_cs_not_a_linear_address` asserts `eip` is a
   16-bit offset and that `linear_pc()` is `cs * 16 + eip`.

10. **Verify in the consumer.** Pick a breakpoint in your own code whose
    linear address exceeds `0x10000`, set it, continue, and assert the stop
    reply is `S05` **and** that `gdb.linear_pc()` equals the address you set.
    Asserting the address, not just the stop, is the whole point: a stop can
    come from something else, and the old bug's signature was a breakpoint
    that answered `OK` and never fired.
