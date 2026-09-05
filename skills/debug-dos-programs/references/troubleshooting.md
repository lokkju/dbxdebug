# Troubleshooting, by symptom

Indexed by what you saw, not by what caused it — that is the order you
actually arrive in. Each entry: **symptom → cause → fix**.

Verified against `dbxdebug` at `5cd96ce88`.

---

## "The breakpoint returns OK and never fires"

**Cause, most likely: you packed the address.** The old convention was
`(seg << 16) | off`. Current stubs decode `Z0`/`z0` as a **linear** address,
`seg * 16 + off`. Below `0x10000` the two readings coincide exactly, which is
why the packing convention survived for years; above 64 KB the breakpoint goes
in at a mangled location and never fires. The stub answers `OK` either way, so
the response tells you nothing.

**Fix.** `addressing.linear(seg, off)`, or `session.set_breakpoint(seg, off)`,
or the `"seg:off"` string form. Grep for `bp_addr`, `<< 16` and `>> 16`;
`addressing.bp_addr` raises `PackedAddressError` on contact, so the call sites
you missed fail loudly rather than silently.

**Cause, second: the guest is in protected mode.** `DEBUG_SetBreakpoint` bails
in pmode and the stub sends `E01` — so `set_breakpoint` returns **False**, not
True. Assert on the return value; that is the only place this surfaces.

**Cause, third: you armed a breakpoint and never continued.** A `Z0`
breakpoint armed while free-running is inert. Activation happens on continue.
The clean sequence is `gdb.halt()` → `set_breakpoint(...)` →
`gdb.continue_execution()`, and the `S05` comes back as the reply to your `c`.

**Cause, fourth: you are connected to an old build.** A stub without
`dosbox-x-linear-bp+` splits `Z0`'s argument as a far pointer. `GDBClient`
refuses such a build at connect by default (`IncompatibleStubError`), so you
only reach this if you passed `require_capabilities=False`.

---

## "My capture is all banner, or all blank"

**Cause.** The debug ports accept as soon as DOSBox-X opens its listeners,
which is long before DOS reaches a prompt. Anything read in that window is
either an unpainted screen or the DOSBox-X welcome banner. The banner is
*text*, so a "count the non-blank cells" content gate passes it happily — this
is how a downstream capture once shipped 24 frames of banner and exited 0.

**Fix.**

- Do not set `boot_settle=0`. The 2.5 s default is the sleep the hand-rolled
  launchers had between spawn and first connect, and it is load-bearing.
- Wait on content, not on time: `session.wait_for_text("C:\\>", timeout=60)`.
  It returns `None` on timeout or on a dead process — **check the return
  value**, or you have written a sleep with extra steps.
- If you read the screen immediately after `start()`, call
  `session.assert_screen_readable()` first. It raises on a blank screen and on
  any line containing `DOSBox-X`.

---

## "I connected to someone else's emulator"

**Cause, most likely: your conf used `gdbport` / `qmpport`.** Those are not
options. DOSBox-X's `Section_prop::HandleInputline` walks its property list,
finds no match, and returns false **with no warning**. The property keeps its
compiled-in default, so the server binds 2159 / 4444 — and your client, told
to use port N, connects to whatever else is on N. On a shared machine that is
plausibly another agent's guest.

**Fix.** The keys contain a space: `gdbserver port`, `qmpserver port`. Better,
let `DosboxSession` render the conf: it allocates ephemeral ports and
cross-checks them against the registry, so the fixed-port collision cannot
happen at all.

**Cause, second: you hardcoded 2159 / 4444.** Those are the CLI defaults, not
a session's ports. Pass `--port $(session.gdb_port)` explicitly, and read §7
of the SKILL before pointing the CLI at a session that already has a client.

**Confirm what is yours** with `dbxdebug session list` — it prints pid, pgid,
both ports, age, state and owner for everything the registry knows.

---

## "Reads return zeros"

**Cause.** The stub cannot report a failed read as a failure: **unmapped
memory and zeroed memory look identical over the wire.** A `m` on a region
that is not backed comes back as zeros, not as `E01`.

**Fix.** Establish a known-good probe first. The 8086 reset vector at
`0xFFFF0` is ROM and stable for the whole life of a session:

```python
assert gdb.read_memory(0xFFFF0, 16).startswith(bytes.fromhex("ea5be000"))
```

If the probe reads correctly and your address does not, the address is wrong
(or the program has not loaded there yet) — not the transport. If the probe is
*also* zeros, suspect the stream trouble below instead.

**Also check:** you may be reading a segment:offset pair you packed. Passing
`(seg << 16) | off` as an int to `read_memory` raises `PackedAddressError`
above `0x110000`, but a **small** segment slips through: `0010:0000` packs to
`0x00100000`, which is accepted and means linear `0x100000`. Real DOS programs
load well above segment 0, so the realistic packing is caught — the guard is
not a proof that a call site is converted.

---

## "A GDB call raised GDBTimeoutError"

**Cause: the stub did not answer that packet** (lokkju/dbxdebug#4, fixed —
this used to be an unbounded hang). `GDBClient` arms a 30 s read timeout on
every read, so an unanswered packet now raises `GDBTimeoutError`, naming the
packet:

```
GDBTimeoutError: GDB stub did not answer b'mffff0,10' within 30.0s. ...
```

The easiest way in is protocol interaction: **while the emulator is
QMP-stopped, the GDB stub does not answer at all** (it is polled from the
emulation thread). So `qmp.stop()` followed by any GDB request can never be
served — and it is a natural thing to write, because `memdump` requires a
stopped CPU.

**Fix.**

- Stop the CPU with `gdb.halt()` when you also intend to talk GDB. Reserve
  `qmp.stop()` for QMP-only work — `memdump` itself works fine while
  QMP-stopped.
- Set your own bound if 30 s is wrong for your workload:
  `GDBClient(timeout=...)`, or `session.gdb.sock.settimeout(...)` after
  `start()`. Nothing is cached; every read consults the socket's own value.
- The connection **survives** this. The client drains what the abandoned
  exchange still owes before it sends anything else, so the next request gets
  its own reply. Do not tear the session down reflexively.

**Second cause: a second GDB client.** See "a second client just hangs" below.

---

## "A GDB call raised GDBDesyncError"

**Cause: the client cannot prove where it sits in the packet stream**
(lokkju/dbxdebug#5, fixed — this used to be silent wrong data). It happens
after a `GDBTimeoutError` whose abandoned reply never arrived either, so the
drain that would have put the stream back could not complete. The client marks
itself **permanently unusable** at that point and raises on every later call.

That is deliberate. What it replaced was worse:

```
baseline m 0x400,8    = f803f80200000000
m 0xFFFF0,16 timed out (request left unanswered)
next m 0x400,8  -> 16 bytes: ea5be000f030312f30312f393200fc55   <-- previous payload
then m 0x400,8  ->  8 bytes: f803f80200000000
```

Eight bytes asked for, sixteen returned, from a different address, and nothing
reported it. Code that sliced the result to the length it expected turned
wrong bytes into plausible ones.

**Fix.** Open a new `GDBClient` — the old one will not recover, by design. If
this keeps happening, the stub is not answering at all: check that no second
client holds the connection, and that the emulator is not QMP-stopped.

**What no longer causes it.**

- **An unsolicited stop reply.** `qmp.debug_break_on_exec(True)` arms *and
  immediately activates* a breakpoint at the next program's entry point,
  regardless of whether any GDB client asked to continue, and the resulting
  `$S05#b8` lands where the client expects its own `+` ACK. It is now diverted
  to `gdb.pending_stops`; drain it with `gdb.take_pending_stops()`. The read
  that follows still gets its own bytes.
- **A single timed-out request.** What the stub still owes is drained before
  the next packet is sent, so one timeout no longer shifts anything.

**Still worth doing.**

- Keep GDB traffic strictly serialised — one request per reply, one thread.
  The resynchronisation is only provable because the client never pipelines.
- Clear every breakpoint before calling `frames.steps_out`. A breakpoint hit
  during one of its single steps pushes an extra stop reply onto the wire; it
  is queued rather than mistaken for an answer now, but the stop you get is
  still not the step you asked for.

**Do NOT paper over stream trouble with a read-retry loop.** "Read the same
bytes until two consecutive reads agree" is the obvious workaround and it is
wrong twice over: it doubles the round-trips on every read, and two identical
consecutive requests mask a one-packet lag *perfectly* — so it would look like
it worked whether or not the stream had shifted. It converts a detectable
protocol fault into an undetectable one. That is why the fix went into the
client's packet layer.

---

## "A second client just hangs"

**Cause: the stub serves ONE GDB client at a time** (lokkju/dbxdebug#8). A
second connection completes the TCP handshake and is then never serviced. No
refusal and nothing on the wire, so the constructor sits in the `qSupported`
handshake until the read timeout expires and raises `GDBTimeoutError` — 30 s
by default. It used to hang there forever.

**Common ways in.**

- Pointing `dbxdebug mem` / `cpu` / `screen` at a session that already holds
  its own GDB client. The CLI opens a client of its own.
- `DOSVideoTools(host, port)` — it constructs its own `GDBClient` too.

**Fix.**

- Inside a session, drive `session.gdb`, and use `session.screen_lines()`
  rather than `DOSVideoTools`.
- If you want the CLI to be the one client, launch with
  `DosboxSession(connect=False)`.
- Reserve `DOSVideoTools` for the standalone case where it owns the only
  connection.

QMP has no such restriction and is the right channel for anything that does
not need the GDB stub.

---

## "Emulators are piling up"

**Do NOT reach for `pkill -f dosbox-x` or `killall`.** It matches on a name,
and a name is not an identity: it kills every emulator on the machine,
including other agents' and other people's. This is a rule, not a preference.

**Cause.** A session's owner was SIGKILLed. Ordinary exits, exceptions,
SIGINT and SIGTERM all reach `stop()` through one of three independent paths
(`__exit__`, `atexit`, signal handlers), so a SIGKILL of the owner is the only
way a stray survives.

**Fix.**

```bash
dbxdebug session list          # ORPHAN = process alive, owner gone
dbxdebug session reap --dry-run
dbxdebug session reap          # orphans and stale entries only
```

`reap` identifies a process by pid **and** its `/proc` start time, so a pid
recycled by something unrelated is never mistaken for the session — that
pairing is the entire point of the registry. It kills the process **group**
(SIGTERM, then SIGKILL after a 5 s grace period), removes the workdir with
`shutil.rmtree`, and deletes the registry file.

Flags: `--all` also reaps live, still-owned sessions; `--max-age SECONDS`
skips young ones, which is what you want when other agents are actively
launching.

**A workdir left behind with no process** shows as `stale` and is cleared by
the same command.

---

## "EIP does not look like an address"

**Cause. It is not one.** `read_registers()["eip"]` is an **offset within
CS**, not a linear address. Register 8 is `reg_eip` verbatim.

**Fix.** `gdb.linear_pc()` — that is `cs * 16 + eip`. If you already hold the
raw list from `read_register_list()`, use `addressing.linear_pc(registers)`
(it indexes `CS_INDEX = 10` and `EIP_INDEX = 8`, so it takes the **list**, not
the dict).

**Why this bites porters.** Older stubs returned `SegPhys(cs) + reg_eip` from
`g` and wrote `reg_eip` verbatim on `G`, so a `g`/`G` round-trip silently
moved the program counter. Code written against that build treats `eip` as
linear, keeps running, keeps producing plausible addresses, and is wrong. The
current build advertises `dosbox-x-eip-offset+` to say which convention it
uses; that advertisement is the only signal, because both conventions produce
a plausible integer.

One accident helps: the legacy clients returned a dataclass supporting
`regs.eip`; this package returns a plain `dict`, so every attribute-style
access breaks loudly with `AttributeError`. Only the subscript form is silent.
Grep for `["eip"]` and `read_register(8)` as well.

---

## "`memdump` failed with a QMPError about being stopped"

**Cause.** `memdump` reads guest memory directly off the QMP socket thread —
it is the one handler that does not defer to the emulation thread — so
DOSBox-X refuses it outright while the guest is running rather than risk a
torn read.

**Fix.** Stop the CPU first. Use `gdb.halt()` if you also intend to talk GDB;
`qmp.stop()` works too but then any GDB request goes unanswered and raises
`GDBTimeoutError` (see that entry). `system_reset` carries the mirror-image
guard: it refuses while
GDB-halted, and allows a plain QMP `stop`.

---

## "`IncompatibleStubError` at connect"

**Cause.** The stub did not advertise `dosbox-x-linear-bp+` in its
`qSupported` reply, so `GDBClient` refuses to proceed. Such a build splits
`Z0`/`z0`'s address as a packed far pointer, meaning breakpoints above 64 KB
answer `OK` and never fire — and neither that nor the EIP convention is
detectable by probing, so the advertisement is the only signal available.

**Fix, in order of preference.**

1. Rebuild the emulator. `dbxdebug doctor` checks that the binary it would
   launch has the remote-debug features compiled in at all.
2. `GDBClient(require_capabilities=False)`, and then pack breakpoint addresses
   and treat `eip` as linear again — confine that to a single adapter, do not
   thread the flag through your code.
3. `client.require_linear_breakpoints()` re-runs the check by hand on a client
   connected with the flag off, if you want to branch on it.

`DosboxSession` builds its clients with the default, so a session against an
old build fails at `start()` rather than at the first breakpoint.

---

## "`ImportError: cannot import name 'DosboxSession' from 'dbxdebug'`"

**Cause, one of two.**

1. `dbxdebug/__init__.py` re-exports the original client surface only. The
   session, registry, addressing, frames, paths and doctor modules are
   importable from their own modules and nowhere else (lokkju/dbxdebug#7).
   Write `from dbxdebug.session import DosboxSession`.
2. You installed the **published** release. PyPI's only release is 0.2.1 and
   it does not contain those modules at all. Confirm:
   ```bash
   uv run python -c "import dbxdebug.session, dbxdebug.addressing, dbxdebug.frames; print('ok')"
   ```
   An `ImportError` there means install from source instead.

---

## "`DosboxLaunchError: dosbox-x executable not found`"

**Cause.** Resolution order is `$DBXDEBUG_DOSBOX` → a conventional checkout
path → `PATH`. An explicitly set `DBXDEBUG_DOSBOX` is **trusted exactly as
given** and never silently swapped for something found on `PATH`, so a typo in
it surfaces here rather than launching a different binary.

**Fix.** `dbxdebug doctor` reports which of the three it found, and whether
that binary looks like a fork build. Set `DBXDEBUG_DOSBOX` to pick a specific
build.

---

## "`FrameWalkError: SP is above BP`"

**Cause.** `steps_out` requires `BP` to actually describe the current frame.
`SP > BP` on entry means no frame pointer has been established (`BP == 0`, a
fresh real-mode entry, hand-written asm) or `BP` is stale. Rejecting that
outright is deliberate: the comparison would otherwise be satisfied almost
immediately and `steps_out` would return having stepped one instruction and
left nothing. `SP == BP` is legal and accepted — a frame with no locals.

**Related, and not an error you will see:** called at a procedure's *first*
instruction, before the prologue has run, `BP` still belongs to the caller and
sits above `SP`, so the entry check passes and `steps_out` measures the
caller's frame instead. Break *after* the prologue.

**Also a known bound:** a callee that pops BP then jumps to a shared epilogue
which pops further registers raises SP past `BP+2` while still inside the
callee, and `steps_out` stops there, early. Telling that apart from a real
return needs instruction decoding, which this does not do.
