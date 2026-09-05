# Bug archaeology

These stories exist so nobody has to re-derive them from source again. Several
of them already have been, more than once. Each entry says what broke, why it
was invisible, what the fix was, and what would happen if someone "simplified"
it back.

Sources: the git history on branch `remotedebug`, and
`docs/superpowers/specs/2026-09-03-dosbox-debug-harness-design.md` (findings
F1-F8, §3.1). All source citations verified at `a4ff6a8cd`.

---

## F1 — the `FP_SEG` breakpoint bug

**The stub disagreed with itself about what an address is.**

`GDBServer::handle_breakpoint` parsed the `Z0` address as a plain hex number
and handed it to `DEBUG_SetBreakpoint`, which then split it as a **packed far
pointer**:

```c
 #define FP_SEG(x) (uint16_t)((uint32_t)(x) >> 16)
 #define FP_OFF(x) (uint16_t)((uint32_t)(x))
 bool DEBUG_SetBreakpoint(uint32_t address) {
     uint16_t seg = FP_SEG(address);
     uint16_t off = FP_OFF(address);
     DEBUG_ShowMsg("Adding Breakpoint %x:%x", seg, off);
     return CBreakpoint::AddBreakpoint(seg, off, false);
 }
```

Meanwhile `m` / `M` went through `DEBUG_ReadMemory` → `mem_readb_checked`, which
is genuinely linear. Two packets, two incompatible address spaces, one stub.

**Why it survived so long.** Below `0x10000` the two readings coincide: `seg`
is 0, `off` is the address, and `GetAddress(0, off) == off`. Every casual test
set a breakpoint in low memory and it worked. Above `0x10000` the address
became `((addr >> 16) << 4) + (addr & 0xffff)` — for `0x30000` that is `0x30`,
in the interrupt vector table.

**Why it was undetectable over the wire.** `AddBreakpoint` returns a
freshly-`new`ed `CBreakpoint*`, which the old code returned as a `bool`. It was
*never* null. So `Z0` answered `OK` unconditionally, for a breakpoint it had
just filed at a garbage address. Real `gdb` set a breakpoint above 64 KB, was
told `OK`, and never stopped.

**The collateral damage.** In a build **without** `C_HEAVY_DEBUG`,
`CBreakpoint::Activate(true)` patches `0xCC` into guest memory at `location`
and saves the old byte (`debug.cpp:630-672`). With `location` computed from the
far-pointer split, the next `c` wrote `0xCC` into whatever the garbage address
happened to be — corrupting guest memory somewhere the user never asked about.
**Caveat, verified:** the whole `Activate` memory-patching body is inside
`#if !C_HEAVY_DEBUG` (`debug.cpp:632-671`), and this tree builds with `C_HEAVY_DEBUG 1`
(`config.h:109`), where breakpoints are checked per-instruction by
`DEBUG_HeavyIsBreakpoint` and no `0xCC` is ever written. So the corruption was
real for plain-`C_DEBUG` builds and not for the heavy-debug build used here;
the silent never-firing breakpoint hit both.

**The fix** (`ad299fd89`). Both functions take the linear address the protocol
specifies:

```c
 bool DEBUG_SetBreakpoint(uint32_t address) {
     ...
     return CBreakpoint::AddBreakpoint(0, address, false) != NULL;
 }
```

Segment zero makes `GetAddress(0, off) == off` in real mode, so the stored
`location` *is* the linear address, and `CheckBreakpoint` compares it against
the true physical PC `GetAddress(SegValue(cs), reg_eip)` — unchanged. The
`FP_SEG` / `FP_OFF` macros are deleted; `gdbserver.cpp` was their only
consumer. The `!= NULL` is now explicit, so the return type stops lying.

**Two follow-on pieces.**

- **Protected mode is refused, not faked** (`6d0fdc4cd`). `GetAddress(0, off)`
  in protected mode routes through `LinMakeProt(0, off)`, which rejects
  selector 0 and returns `mem_no_address`. The breakpoint would go somewhere
  useless while `Z0` answered `OK` — the same failure the fix just removed. So
  both functions bail on `cpu.pmode && !(reg_flags & FLAG_VM)`
  (`debug.cpp:6286`, `:6295`) and the stub sends `E01`. Protected-mode
  breakpoints are **not implemented**; the guard only stops the stub from
  claiming otherwise.
- **The breakpoint list display** (`debug.cpp:919-928`). A GDB-set breakpoint
  stores segment 0 and a full linear offset, so `"%04X:%04X"` would print
  `0000:30000` — an offset wider than every other entry's field. `ShowList`
  special-cases `segment == 0 && offset > 0xFFFF` and prints
  `%08X (linear)`. Display only.

## F1's twin — register 8 was a linear address

Same shape, same invisibility, found at the same time (`ebd34a82c`).

`DEBUG_GetRegister(8)` returned `SegPhys(cs) + reg_eip` — a linear address —
while `DEBUG_SetRegister(8)` has always written `reg_eip`. RSP defines register
8 as EIP, an offset within CS. Consequences:

- real `gdb` displayed a wrong `$pc` and got wrong answers from every
  `$pc`-relative expression;
- a `g` then `G` round-trip **silently moved the program counter**, because the
  read returned linear and the write took it as an offset.

Fixed: register 8 reports `reg_eip` (`debug.cpp:6147`). Clients wanting the
linear PC compute `cs * 16 + eip` from registers 10 and 8. The same commit
added the `P` packet, because a full `G` was previously the only way to write
one register — and `G` was itself the corrupting operation.

## Why the vendor `qSupported` flags exist

`08e23d1b0`. **Neither fix is detectable by probing.** `Z0` answers `OK`
whether it reads its argument as linear or as a packed far pointer; register 8
returns a plausible-looking number under either interpretation. Without an
advertisement both semantics are silent in both directions — a new client
against an old stub and an old client against a new one each compute wrong
addresses and never find out.

So the stub advertises `dosbox-x-linear-bp+` and `dosbox-x-eip-offset+`
(`gdbserver.cpp:515-517`). Real `gdb` ignores unrecognised `qSupported`
features, so both stay protocol-legal. **If either semantic ever changes, the
flag name must change with it.**

---

## F6 — the halted drain

**Pending QMP work was never serviced while the CPU was halted for GDB.**

`DEBUG_CheckGDBStep()` returns `true` whenever the CPU is paused for GDB, and
`Normal_Loop` returned immediately on that path — *before*
`SAVESTATE_CheckPendingRequest`, `EMULATOR_CheckPendingControl` and
`QMP_ProcessPendingInputEvents`, which sit further down the loop body. So none
of them ran while stopped at a breakpoint.

Two concrete failures:

- `savestate` at a breakpoint published its request, polled
  `SAVESTATE_IsPending()` for the full **30 s** timeout, and returned
  `GenericError: Save state operation timed out`. The request was still sitting
  there, never drained.
- Keys queued at a breakpoint went into the mutex-guarded input queue and were
  **never delivered** — the queue was only drained on the running path.

Both look like server bugs in the individual commands. Neither is.

**The fix** (`559bb21d8`): duplicate the three drains inside the
`DEBUG_CheckGDBStep()` branch, immediately before `return 0`
(`dosbox.cpp:475-477`).

**Why not hoist them above the check** — this is the part that gets
re-litigated. Hoisting `dosbox.cpp:482-486` above `DEBUG_CheckGDBStep()` looks
tidier and removes the duplication. It also changes ordering on **every**
iteration of the emulation hot loop: pending QMP control would be handled
before the GDB step check on every instruction batch. That trades a behaviour
change in the running path for a fix in the halted path. The branch copy leaves
the running path bit-identical and adds behaviour only where there was none —
four lines that are far easier to defend in review. It also drains *after* a
completed step, which is wanted. Latency is a non-issue: while halted,
`Normal_Loop` returns immediately and is re-entered, so the drain runs at spin
frequency rather than on a poll interval.

**If you add a fourth drain, add it in both places.** There is no mechanism
preventing the copies from diverging.

## F7 — the `memdump` race

`memdump` is the only QMP handler that reads guest state from the socket
thread; everything else defers (`savestate`/`loadstate` via `SAVESTATE_Request*`,
`stop`/`cont`/`system_reset` via `EMULATOR_CheckPendingControl`, `screendump`
via `CAPTURE_TakeScreenshot`, input via the mutex-guarded queue). It called
`DEBUG_SaveMemoryBin` directly, racing the emulation thread and returning bytes
that were never coherent.

Fixed by **guarding rather than deferring** (`97c34f3f2`, widened in
`e671d6efb`): allowed when `DEBUG_IsCpuPausedForDebug() || EMULATOR_IsPaused()`
— two disjoint flags, both meaning no guest code is executing — and refused
otherwise with a message saying how to fix it. Dumping at a breakpoint, the
common case, keeps zero added latency.

The deferred path was **deliberately not built**. The existing `SAVESTATE_*`
idiom sleep-polls at 100 ms, which would defeat the 30-60 Hz use case `memdump`
exists for, and no consumer needs running-guest dumps. If you are about to add
a condition-variable marshal here, that is the bar to clear: a measured need.

`system_reset` picked up a related guard in `6d0fdc4cd` — refusing while
GDB-halted, but **not** while merely QMP-paused. Different reason: session
coherency, not memory coherency. Rebooting while a GDB client believes it is
attached at a halt leaves that client staring at registers, memory and
breakpoints that no longer exist. The alternative — reset anyway and "notify"
the client — was considered and rejected because what "notify" should mean (a
synthetic stop reply? a forced detach?) is not obvious, and refusing is honest
where guessing is not.

---

## F4 — the config-name trap

The options are **`gdbserver port`** and **`qmpserver port`**, with a space
(`dosbox.cpp:1734`, `:1740`).

`gdbport` and `qmpport` are not options. Writing one in a `.conf` produces no
warning: `Section_prop::HandleInputline` (`setup.cpp:844`) scans the section's
declared properties for a name match, finds none, and returns `false`. The real
property keeps its declared default, so the server binds **2159 / 4444**.

The failure this produced in practice: a client configured for its own port
connected to `2159`/`4444` and reached **a different agent's emulator** on the
same machine. The symptom was

```
QMPError: Failed to receive valid JSON: b''
```

— which reads like a broken server and is not. Anything that goes wrong here
looks like a server bug, because the client did connect, to something.

`tests/integration/test_config.py` (`476079743`) pins it: a conf written with
`gdbserver port = N` must produce a listener on `N` and none on 2159.

Also note both port options are `Property::Changeable::OnlyAtStart` — changing
them at runtime is silently ineffective.

## F5 — readiness is not port-accept

The servers bind and accept **well before** DOS reaches a prompt. A launcher
that waits for the port to accept and then starts typing is racing the boot.

Two consequences, both silent:

- keys typed in that window are simply lost;
- a screen capture in that window records the **DOSBox-X welcome banner**.

The banner is the nasty part: it is *text*. A naive readiness gate of
"the screen is not blank" passes it. Two downstream capture scripts shipped 24
frames of banner before anyone noticed.

Readiness has to be measured against **content** — wait for the specific text
that means the guest is where you think it is — not against a port, and not
against a bare sleep.

---

## The protocol desync — what a client must tolerate

**Newly confirmed, reproduced live against this build.** This is the least
obvious thing in the file and it constrains anyone touching the servers.

The GDB stub's stream is *mostly* strict request/response, and clients written
against it assume that completely. It is not.

### Trigger 1: break-on-exec sends an unsolicited stop reply

`DEBUG_CheckExecuteBreakpoint` (`debug.cpp:5596-5601`) runs when a program is
about to execute. If the QMP `debug-break-on-exec` flag is set:

```c
    if (gdb_break_on_exec) {
        LOG(LOG_REMOTE, LOG_DEBUG)("DEBUG: GDB break on exec at %04X:%08X", seg, off);
        CBreakpoint::AddBreakpoint(seg,off,true);
        CBreakpoint::ActivateBreakpointsExceptAt(SegPhys(cs)+reg_eip);
    }
```

It **arms and immediately activates**. No `c` packet is involved, and the CPU
may be free-running with a GDB client attached but idle.

On the hit, the core reaches `DEBUG_Enable_Handler` (`debug.cpp:4849`), which
sees a connected GDB client and calls `send_stop_reply(5)` at
**`debug.cpp:4860`** — writing `$S05#b8` onto a stream the client believes is
strictly request/response.

For a client in **ACK mode** the damage is immediate: it sends a request,
expects the stub's `+` ack as the next byte, and reads `$` instead. From there
its byte stream is misaligned.

### Non-trigger: a `Z0` breakpoint armed while free-running is INERT

Worth stating explicitly, because it is the natural first suspect and it is
wrong. `DEBUG_SetBreakpoint` calls `CBreakpoint::AddBreakpoint(0, address,
false)` (`debug.cpp:6291`), which only pushes onto `BPoints` — it never calls
`Activate`. `CheckBreakpoint` requires `bp->IsActive()` (`debug.cpp:740`).

Activation on the GDB path happens in exactly one place: `GDBAction::CONTINUE`
→ `CBreakpoint::ActivateBreakpoints()` (`debug.cpp:6242`). So a `Z0` sent while
the guest runs, with no following `c`, cannot fire and cannot desynchronise
anything.

**The asymmetry is the whole point:** `debug-break-on-exec` activates,
`Z0` does not. Same breakpoint list, two different arming semantics, one of
which can produce an unsolicited packet.

### Trigger 2 (client-side): a timed-out request leaves the client one packet behind

This one is not a stub bug at all, and it is included here precisely because it
is indistinguishable from one.

A client whose `recv` times out while waiting for a reply abandons that reply —
but the reply still arrives, and nothing discards it. The next request reads
the *previous* request's payload. The client is now permanently one packet
behind, silently, returning plausible-looking answers to the wrong questions.
The in-repo raw client has no reply-to-request correlation to catch this
(`tests/integration/protocol/gdb.py`), and QMP has no `id` echo either, so
neither protocol can self-correct.

Symptoms that look like a stub bug but are this: register values that belong to
the previous step; a memory read returning the bytes you asked for one call
ago; `OK` arriving in response to a `g`.

### What a client must therefore tolerate

1. **An `S05` may arrive at any time**, including as the very next bytes after
   a request in ACK mode. Skip and record asynchronous stop replies rather than
   treating them as the answer to the outstanding request.
2. **Prefer `QStartNoAckMode`.** It removes the `+`/`-` interleaving that makes
   trigger 1 corrupting rather than merely surprising. Remember it does not
   survive a reconnect (`gdbserver.cpp:121`, `:143`).
3. **Treat a timeout as fatal to the connection**, not as a recoverable error.
   Reconnect and renegotiate; do not send another request on a socket whose
   reply queue you have lost track of.
4. **Do not assume `?` is read-only.** It stops the CPU (`gdbserver.cpp:314-318`).
5. **Never assume a `Z0` is live.** Always `Z0` then `c`.
6. Expect `S05` and nothing else — no `T` packets, no signal other than
   SIGTRAP, no `swbreak`/`hwbreak` annotation, regardless of what `qSupported`
   advertises.

### If you are changing the servers

The desync is a real constraint, not just client advice. Any new code path that
can write to the GDB socket from outside `process_command` widens it. There are
currently exactly two such paths — `DEBUG_Enable_Handler` (`debug.cpp:4860`)
and the post-step reply in `DEBUG_CheckGDBStep` (`debug.cpp:6235`) — and the
second is at least solicited. Before adding a third, decide whether the client
can distinguish it from a reply, and write that down here.
