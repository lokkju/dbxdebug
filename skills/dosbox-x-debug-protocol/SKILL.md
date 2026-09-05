---
name: dosbox-x-debug-protocol
description: The contracts governing the DOSBox-X remote debug servers — the GDB RSP stub (src/debug/gdbserver.cpp), the QMP server (src/debug/qmp.cpp) and their hooks in src/debug/debug.cpp. Use when editing those files, adding a QMP command or GDB packet, changing the halt/resume paths, or explaining why the stub behaves the way it does.
license: LicenseRef-Polyform-Shield-1.0.0
metadata:
  source-repo: dosbox-x (fork, branch `remotedebug`)
  verified-against: 040b15757
---

# DOSBox-X remote debug protocol

Three files carry the whole remote debug surface:

| File | Role |
| --- | --- |
| `src/debug/gdbserver.cpp` | GDB remote serial protocol stub. **Polled**, no thread. |
| `src/debug/qmp.cpp` | QMP server. **Owns a thread.** |
| `src/debug/debug.cpp` | The bridge: register/memory/breakpoint accessors, `DEBUG_CheckGDBStep`, halt bookkeeping. |
| `src/dosbox.cpp` | `Normal_Loop` — where both are serviced. |

Everything below was read out of the source at commit `a4ff6a8cd`. Line
numbers move; the invariants do not. If a citation no longer lands where this
says, re-read and fix the citation — do not assume the invariant changed.

---

## 1. The threading contract

**The two servers do not have the same threading model, and that is the single
easiest thing to get wrong here.**

### GDB: polled, single-threaded

`GDBServer` has no thread. `GDBServer::poll()` is called only from
`DEBUG_CheckGDBStep()` (`debug.cpp:6207`, calling `poll()` at `debug.cpp:6213`),
which `Normal_Loop` calls at `dosbox.cpp:469`. Sockets are non-blocking
(`O_NONBLOCK` in `setup_socket`/`try_accept`), so `poll()` returns immediately
when there is nothing to read.

Consequence: **every GDB packet is parsed and answered on the emulation
thread.** GDB handlers may touch guest state freely — `DEBUG_ReadMemory`,
`DEBUG_GetRegister`, `CBreakpoint::*` are all safe from there. Outbound writes
(`send_stop_reply` from `DEBUG_Enable_Handler`, `debug.cpp:4860`) are on the
same thread too.

The one thing that follows from being on the emulation thread: an uncaught
exception kills the emulator, not the connection. That is why
`process_command` wraps its whole dispatch in one try/catch
(`gdbserver.cpp:308-360`) — `std::stoul`/`std::stoi` on malformed hex throws
`std::invalid_argument` or `std::out_of_range` out of five different handlers.
Any new handler that parses client input inherits that protection; do not add a
handler outside the try block.

### QMP: its own thread, and it must not touch guest state

`QMPServer::start()` spawns `server_thread = std::thread(&QMPServer::run, this)`
(`qmp.cpp:247`). `run()` → `wait_for_client()` (blocking `accept`) →
`handle_client()` (blocking `recv`) → `process_command()` (`qmp.cpp:401`).
**Every QMP handler body runs on that socket thread**, concurrently with the
CPU executing guest code.

The correct idiom is **request-then-poll**: the handler publishes a request,
the emulation thread performs it inside `Normal_Loop`, the handler polls a
completion flag and then answers the client.

| Handler | Publishes | Drained by | Poll / timeout |
| --- | --- | --- | --- |
| `savestate` / `loadstate` | `SAVESTATE_RequestSave` (`savestates.cpp:322`) / `RequestLoad` (`:331`) | `SAVESTATE_CheckPendingRequest` (`savestates.cpp:355`) | `SAVESTATE_IsPending`, 100 ms / 30 s |
| `stop` / `cont` | `EMULATOR_RequestPause` (`sdlmain.cpp:1453`) / `RequestResume` (`:1457`) | `EMULATOR_CheckPendingControl` (`sdlmain.cpp:1473`) | `EMULATOR_IsPaused`, 10 ms / 1 s |
| `system_reset` | `EMULATOR_RequestReset` (`sdlmain.cpp:1465`) | same | none — answers immediately |
| `send-key`, `input-send-event` | `queue_input_event` (`qmp.cpp:464`), mutex-guarded `std::queue` | `process_pending_input_events` (`qmp.cpp:469`) | none — answers immediately |
| `screendump` | `CAPTURE_TakeScreenshot` (`qmp.cpp:787`) | the capture path on the emulation thread | `CAPTURE_IsScreenshotPending`, 50 ms / 5 s |

`EMULATOR_CheckPendingControl` throws `int(3)` / `int(6)` for the two reset
flavours (`sdlmain.cpp:1493`, `:1500`) — the reset is a longjmp-ish unwind out
of the loop, which is another reason it cannot be done from the socket thread.

### The drain, and why it is written twice

`Normal_Loop` (`src/dosbox.cpp:430`) drains all three queues once per loop
iteration:

```c
            if (DEBUG_CheckGDBStep()) {                 // dosbox.cpp:469
                SAVESTATE_CheckPendingRequest();        // :475
                EMULATOR_CheckPendingControl();         // :476
                QMP_ProcessPendingInputEvents();        // :477
                return 0;
            }
            SAVESTATE_CheckPendingRequest();            // :482
            EMULATOR_CheckPendingControl();             // :484
            QMP_ProcessPendingInputEvents();            // :486
```

**The duplication at 475-477 is load-bearing.** `DEBUG_CheckGDBStep()` returns
true whenever the CPU is halted for GDB (`GDBAction::STOP`, a completed
`STEP`, or `NONE` while `gdb_cpu_paused && has_client()` —
`debug.cpp:6216-6258`). On that path `Normal_Loop` returns before ever reaching
482-486, so without the copy **the QMP drains stop running entirely the moment
the CPU halts for GDB**. Observed consequence: `savestate` at a breakpoint sat
out its full 30 s timeout and returned an error; keys queued at a breakpoint
were never delivered. See `references/history.md` (F6).

**Do not "fix" this by hoisting the drains above the check.** Hoisting changes
ordering on *every* iteration of the emulation hot loop — pending QMP control
would be handled before the GDB step check on every instruction batch, trading
a behaviour change in the running path for a fix in the halted path. The branch
copy leaves the running path bit-identical and adds behaviour only where there
was none. It also drains *after* a completed step, which is wanted. Latency is
not a concern: while halted, `Normal_Loop` returns immediately and is
re-entered, so the drain runs at spin frequency, not on a poll interval.

If you add a fourth per-loop drain, **add it in both places.**

### The one handler that violates the contract: `memdump`

`handle_memdump` calls `DEBUG_SaveMemoryBin` directly from the socket thread —
it is the only QMP handler that reads guest state without deferring. Rather
than build a marshal for it, it is **guarded** (`qmp.cpp:721`):

```c
    if (!DEBUG_IsCpuPausedForDebug() && !EMULATOR_IsPaused()) {
        if (use_temp) unlink(filepath.c_str());
        send_error("GenericError",
                   "memdump requires the CPU to be stopped for debugging; "
                   "halt via GDB or QMP stop first");
        return;
    }
```

Two disjoint flags, both meaning "no guest code is executing, memory is
quiescent": `DEBUG_IsCpuPausedForDebug()` (`debug.cpp:6359`) covers the
interactive debugger and a GDB halt; `EMULATOR_IsPaused()` (`sdlmain.cpp:1469`)
covers a QMP `stop`, which parks the emulation thread in `PauseDOSBoxLoop`.
Running guest ⇒ refuse. The refusal path does **not** queue the request;
a request/poll marshal was deliberately not built, because the existing
`SAVESTATE_*` idiom polls at 100 ms and would destroy the 30-60 Hz use case
`memdump` exists for, and dumping a *running* guest at that rate has no
measured consumer.

`system_reset` carries a related but different guard (`qmp.cpp:1071`): it
refuses on `DEBUG_IsCpuPausedForDebug()` alone, and deliberately **not** on
`EMULATOR_IsPaused()`. The reason is coherency of the debug *session*, not of
memory — rebooting while a GDB client still thinks it is attached at a halt
leaves that client staring at registers, memory and breakpoints that no longer
exist. A plain QMP `stop` has no debug client to confuse, so reset from there
stays allowed.

---

## 2. The address-semantics contract

**Breakpoints and memory are LINEAR. `seg * 16 + off`. No exceptions.**

- `m` / `M` → `DEBUG_ReadMemory` / `DEBUG_WriteMemory` (`debug.cpp:6180`,
  `:6189`) → `mem_readb_checked` / `mem_writeb_checked`. Linear, always were.
- `Z0` / `z0` → `DEBUG_SetBreakpoint` / `DEBUG_RemoveBreakpoint`
  (`debug.cpp:6285`, `:6294`). **Linear.** Implemented as
  `CBreakpoint::AddBreakpoint(0, address, false)` (`debug.cpp:6291`) /
  `DeleteBreakpoint(0, address)`: segment zero makes `GetAddress(0, off) == off`
  in real mode, so the stored `location` *is* the linear address, and
  `CheckBreakpoint` already compares it against the true physical PC
  `GetAddress(SegValue(cs), reg_eip)`.

The `FP_SEG(x) = x >> 16` / `FP_OFF` far-pointer split is **gone** — the macros
no longer exist anywhere in `src/debug/`. Do not reintroduce a
segment:offset reading of an RSP address. The full story is in
`references/history.md` (F1); the short version is that the stub used to
disagree with itself and answered `OK` to breakpoints it silently dropped.

**Protected mode is refused, not faked.** Both functions bail when
`cpu.pmode && !(reg_flags & FLAG_VM)` (`debug.cpp:6286`, `:6295`) and return
false, so the stub sends `E01`. `GetAddress(0, off)` in pmode routes through
`LinMakeProt(0, off)`, which rejects selector 0 and yields `mem_no_address` —
the breakpoint would be stored somewhere useless and could never fire.
Refusing is honest; answering `OK` was not.

**Register 8 (EIP) is an OFFSET within CS, not a linear address.**
`DEBUG_GetRegister(8)` returns `reg_eip` (`debug.cpp:6147`) and
`DEBUG_SetRegister(8)` writes `reg_eip` (`debug.cpp:6169`), so `g`/`G` and
`p`/`P` round-trip cleanly. Clients wanting the linear PC compute
`cs * 16 + eip` themselves. This asymmetry — addresses linear, `$pc` an offset
— is deliberate and is what real `gdb` expects on a 16-bit real-mode target.

---

## 3. The config option names

```
gdbserver port      # default 2159   dosbox.cpp:1734
qmpserver port      # default 4444   dosbox.cpp:1740
gdbserver           # bool, default false   dosbox.cpp:1731
qmpserver           # bool, default false   dosbox.cpp:1737
```

**The port options contain a space.** `gdbport` and `qmpport` are not options.
They are read back at `debug.cpp:5709` and `:5715` via
`Section_prop::Get_int("gdbserver port")`.

Writing `gdbport=5000` in a `.conf` fails silently and dangerously:
`Section_prop::HandleInputline` (`setup.cpp:844`) walks `properties` looking
for a name match, finds none, and returns `false` with no warning
(`Get_int` itself returns `0` for an unknown name — `setup.cpp:754-761` — but
that path is not even reached here). The property keeps its declared default,
so the server binds **2159 / 4444**. A client that then connects to its
intended port finds nothing — or worse, connects to *another emulator already
listening there*, which on a shared machine means another user's guest.

The observed failure mode is `QMPError: Failed to receive valid JSON: b''` —
which looks like a server bug and is not. `tests/integration/test_config.py`
pins this: a conf written with `gdbserver port = N` must produce a listener on
`N` and none on 2159.

Note also `Property::Changeable::OnlyAtStart` on both port options: changing
them at runtime does nothing.

---

## 4. The `qSupported` vendor flags

`handle_query` (`gdbserver.cpp:504`) answers `qSupported:` with exactly
(`gdbserver.cpp:515-517`):

```
PacketSize=3fff;swbreak+;hwbreak+;vContSupported+;QStartNoAckMode+;dosbox-x-linear-bp+;dosbox-x-eip-offset+
```

| Flag | Promise | Status |
| --- | --- | --- |
| `PacketSize=3fff` | 16383-byte packets accepted | honoured; no cap enforced on `m` length |
| `swbreak+` / `hwbreak+` | *(GDB meaning: stop replies carry `swbreak`/`hwbreak`)* | **not honoured** — the stub only ever sends bare `S05`, and `Z1` is refused with an empty reply (`gdbserver.cpp:489`) |
| `vContSupported+` | `vCont` is implemented | partly — `vCont?` advertises `c;s;t` but `t` is not dispatched (`gdbserver.cpp:370-378`) |
| `QStartNoAckMode+` | no-ack mode works | honoured (`gdbserver.cpp:309-311`) |
| `dosbox-x-linear-bp+` | **vendor.** `Z0`/`z0` take a LINEAR address, not a packed far pointer | honoured |
| `dosbox-x-eip-offset+` | **vendor.** Register 8 is `reg_eip`, an offset within CS, not `SegPhys(cs)+reg_eip` | honoured |

The two vendor flags exist because **both broken behaviours were
indistinguishable from correct ones over the wire**: `Z0` answered `OK` under
either address reading, and register 8 returned a plausible-looking number
under either interpretation. A client cannot detect a pre-fix stub by probing;
it can only look for these flags. Real `gdb` ignores `qSupported` features it
does not recognise, so both stay RSP-legal.

**If you change either semantic, change the flag name.** A client that trusts
`dosbox-x-linear-bp+` and gets far-pointer behaviour is worse off than one
that got no flag at all.

The `swbreak+`/`hwbreak+`/`vCont;t` rows above are known-inaccurate
advertisements. Fixing them means either implementing the feature or dropping
the flag — both are behaviour changes for connected clients, so they need a
conformance test either way.

---

## 5. Checklist: adding a QMP command

1. **Decide which thread does the work.** If the handler needs guest state or
   emulator control, it does **not** do it inline. Publish a request, drain it
   in `Normal_Loop`, poll a completion flag (§1). Inline access from the socket
   thread is a race, and `memdump` is the sole exception — guarded, documented,
   and not a precedent.
2. **If you add a per-loop drain, add it in BOTH places** in `Normal_Loop`:
   the `DEBUG_CheckGDBStep()` branch (`dosbox.cpp:475-477`) *and* the running
   path (`:482-486`).
3. **Dispatch it** in `QMPServer::process_command` (`qmp.cpp:401`).
4. **Advertise it** in `handle_query_commands` (`qmp.cpp:445`). The list is
   hand-maintained and already out of sync — `quit` and `system_powerdown` are
   dispatched but unlisted. Do not add to the drift.
5. **Parse arguments with the existing helpers** — `extract_string`,
   `extract_int`, `extract_bool`, `extract_array` (`qmp.cpp:153-236`). They are
   naive substring scanners, not a JSON parser: `extract_string` finds the
   *first* occurrence of `"key"` anywhere in the buffer, including inside a
   nested object or a string value. Handlers that need a nested `arguments`
   object brace-match it by hand first (see `handle_memdump`,
   `qmp.cpp:651-667`). Follow that pattern; do not assume key uniqueness.
6. **Pick the state precondition explicitly** and say so in the error text.
   Halted-only? Running-only? Idempotent either way? `memdump` refuses while
   running; `system_reset` refuses while GDB-halted; `stop`/`cont` are
   idempotent. Whatever you choose, the error must be a `send_error` with a
   class and a description a human can act on.
7. **Answer exactly once, on every path.** Every branch must reach
   `send_success()`, `send_error()` or `send_response()`. A handler that
   returns without replying wedges the client on `recv` until its timeout, and
   from there it is permanently one packet behind (§6).
8. **Add a conformance test** in `tests/integration/test_qmp_conformance.py` —
   one assertion per dispatch entry, including the error shape and the state
   precondition.
9. **Add a client method** in the `dbxdebug` library.
10. **Update this skill** — `references/qmp-commands.md` at minimum, and §4/§5
    here if you changed a contract.

## 6. Checklist: adding or changing a GDB packet

1. Dispatch it inside the existing try/catch in `process_command`
   (`gdbserver.cpp:308`). Handlers throw on malformed hex; the catch is what
   keeps that from taking down the emulator.
2. Beware the prefix matching: the dispatch tests `cmd.substr(0,1)` for
   `H p P G m M Z z s c q`, so any new packet whose first character collides is
   swallowed by the existing handler. Exact-match packets (`?`, `g`,
   `QStartNoAckMode`, `vMustReplyEmpty`) are tested first.
3. Unknown packets must answer with an **empty packet** (`$#00`), not an error.
   That is the RSP way to say "unsupported". Errors are `E01`
   (`gdbserver.cpp:358`) and mean "I tried and failed".
4. If it changes an observable semantic, change or add a `dosbox-x-*`
   `qSupported` flag (§4).
5. Conformance test in `tests/integration/test_gdb_conformance.py`, then client
   method, then this skill.

---

## References

- `references/packets.md` — every GDB RSP packet this stub handles, exact
  request/response shapes, what is deliberately unimplemented, error
  conventions.
- `references/qmp-commands.md` — every command `process_command` dispatches,
  arguments, response shape, state preconditions, and the
  advertised-vs-dispatched mismatches.
- `references/history.md` — the bug archaeology. Read this before "fixing"
  anything in §1-§4; several of these have been re-derived from source more
  than once. Also carries **what a client must tolerate**, including the
  unsolicited-`S05` protocol desync.
- `docs/superpowers/specs/2026-09-03-dosbox-debug-harness-design.md` — the
  design that produced these fixes (findings F1-F8, §3.1).
