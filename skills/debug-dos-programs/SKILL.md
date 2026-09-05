---
name: debug-dos-programs
description: Drive a DOS program under DOSBox-X emulation with the `dbxdebug` Python package — launch an emulator, wait for a real prompt, type at the guest, break on an address, read registers, memory, stack frames and the text screen, and clean up strays. Use when running or automating a DOS program under emulation, capturing its screen or memory, setting breakpoints, or when emulator processes are piling up.
license: LicenseRef-Polyform-Shield-1.0.0
metadata:
  source-repo: dbxdebug
  verified-against: 5cd96ce88
  companion-skill: dosbox-x-debug-protocol (for editing the C++ servers)
---

# Debugging DOS programs under DOSBox-X

This is the **client** side: you have a DOS program and you want to run it,
poke at it, and get data back out. The Python package is `dbxdebug`; the
emulator is a DOSBox-X **fork** with `gdbserver` and `qmpserver` compiled in.
Nothing here works against a stock DOSBox-X build.

If you are editing the emulator's C++ (`src/debug/gdbserver.cpp`,
`src/debug/qmp.cpp`), you want the `dosbox-x-debug-protocol` skill instead.
This one deliberately does not restate the server-side contracts.

Working snippets: `references/recipes.md`.
Something already looks wrong: `references/troubleshooting.md`, indexed by
symptom.

---

## 0. Before you write any code

**The published PyPI release is not the package this skill describes.** As of
`5cd96ce88` the only release on PyPI is **0.2.1**, and it ships the original
client surface only — no `session`, no `addressing`, no `frames`, no
`registry`, no `paths`, no `doctor`. Install from source (`uv add --editable
../dbxdebug`, or a git dependency) and confirm before doing anything else:

```bash
uv run python -c "import dbxdebug.session, dbxdebug.addressing, dbxdebug.frames; print('ok')"
```

An `ImportError` there means everything below is inapplicable to what you
installed.

**Run `dbxdebug doctor` before fanning out** across concurrent sessions. It
never starts an emulator: it reports whether a binary is found, whether the
remote-debug features look compiled in, the host CPU count as a rough
concurrency ceiling, whether the registry directory is writable, and whether
orphaned sessions are already lying around. Fan-out onto a host with strays
already on it is how you end up debugging someone else's guest.

Point it at a specific build with `DBXDEBUG_DOSBOX=/path/to/dosbox-x`.

---

## 1. `DosboxSession` is the only sanctioned launcher

Always as a context manager. Never hand-roll a `subprocess.Popen`.

```python
from dbxdebug.session import DosboxSession   # NOT `from dbxdebug import ...`

with DosboxSession(mounts={"c": host_dir}) as session:
    ...
```

The session owns four things a hand-rolled launcher has to reimplement, and
gets wrong at least once:

- **ephemeral ports**, allocated fresh and cross-checked against the registry,
  with the launch retried on a lost bind race (`port_retries`, default 3);
- **a private scratch workdir**, removed with `shutil.rmtree` from Python —
  never a shell `rm -rf`, which some sandboxes refuse outright, which is
  exactly how orphaned scratch dirs happen;
- **a registry entry** recording pid, pgid, owner pid, ports, workdir and
  `/proc` start times, so "is this still mine" has an answer;
- **process-GROUP teardown** (SIGTERM then SIGKILL), so a child the emulator
  spawned cannot outlive it.

Teardown runs from three independent paths onto the same idempotent `stop()`:
`__exit__`, an `atexit` hook, and SIGINT/SIGTERM handlers. Only a SIGKILL of
the owning process can leave a stray behind — which is what `dbxdebug session
reap` exists for.

Two things that surprise people:

- **`program=` is recorded, not auto-run.** A caller arming break-on-exec has
  to arm before the exec happens, so the session never runs it for you. Use
  `autoexec=` if you want it launched at boot, or type it yourself.
- **Headless is the DEFAULT.** A session opens no window and steals no
  keyboard focus. The debug surface is unaffected -- `screen_lines()` and
  `qmp.screendump()` both work headless, verified byte-for-byte against a
  windowed session. Pass `headless=False` only when a human wants to watch
  the guest; it takes the user's focus.

`import dbxdebug` does **not** re-export `DosboxSession`, `addressing`,
`frames`, `registry`, `paths` or `doctor` (lokkju/dbxdebug#7). Import those
from their own modules; `GDBClient`, `QMPClient` and `DOSVideoTools` come from
the package root.

---

## 2. Never run `pkill -f dosbox-x`. In any form.

`pkill`, `killall`, `pkill -9 -f dosbox-x` — none of them. That command kills
**every** emulator on the machine, including other agents' and other people's.
It matches on a name, and a name is not an identity.

Use the registry instead, which only ever touches a process it can identify by
pid **and** `/proc` start time (a bare pid gets recycled, on a busy host in
well under a minute):

```bash
dbxdebug session list          # what the registry knows; ORPHAN is flagged
dbxdebug session reap --dry-run
dbxdebug session reap          # orphans and stale entries only
dbxdebug session reap --all    # also live, still-owned sessions
```

**These are subcommands, not flags.** `dbxdebug session list`, not `dbxdebug
session --list`. `reap` also takes `--max-age SECONDS` to skip young sessions.

`reap` with no flags kills only what nobody owns any more: a registered
process whose launching process has exited, or a registry file whose process
is already gone (workdir left behind). Live, owned sessions are reported as
`kept`.

---

## 3. A port that accepts is not a guest that is ready

The debug ports accept as soon as DOSBox-X opens its listeners. That is well
before DOS has finished booting. Keys typed into that window are simply lost,
and a screen capture taken there shows the **DOSBox-X welcome banner** — which
is *text*, so a naive "is the screen blank?" content gate passes it. Two
downstream capture scripts once shipped 24 frames of exactly that banner and
exited 0.

Three defences, in order:

1. **`boot_settle`** (default 2.5 s) — a sleep after the clients connect,
   before `start()` hands back the handle. It is load-bearing. Do not set it
   to zero because it looks like a code smell.
2. **`wait_for_text()`** — poll the guest's screen until a substring appears.
   This is readiness *observed*, not assumed. It returns the observed seconds,
   or **`None`** on timeout or on a dead process. Check the return value; a
   bare call that ignores it is a sleep with extra steps.
3. **`assert_screen_readable()`** — raises `DosboxLaunchError` if the screen is
   entirely blank, or if any line still contains `DOSBox-X`. Call it if you
   read the screen right after `start()`.

```python
assert session.wait_for_text("C:\\>", timeout=60) is not None
```

Never a bare `time.sleep()` in place of these.

---

## 4. Addressing: linear, and EIP is not an address

**Breakpoints and memory take LINEAR addresses**: `seg * 16 + off`. That is
`addressing.linear(seg, off)`. `Z0`/`z0` and `m`/`M` all use the same
encoding.

**`addressing.bp_addr` raises rather than packing.** The historical helper
packed `(seg << 16) | off`, which was correct against old stubs and is wrong
against current ones — and the stub answers `OK` to either, so a silently
misplaced breakpoint above 64 KB is indistinguishable from a correct one by
its response. `bp_addr` exists only to fail loudly. `parse_address` backs it
up by rejecting an integer at or above `0x110000`.

**`read_registers()["eip"]` is an OFFSET within CS, not a linear address.**
Use `gdb.linear_pc()` (which is `cs * 16 + eip`), or
`addressing.linear_pc(register_list)` if you already have the raw list from
`read_register_list()`. This is the one change with no runtime signal at all:
code that treats `eip` as linear keeps running and keeps producing plausible
addresses.

Two `set_breakpoint` signatures, deliberately different:

| Call | Takes |
|---|---|
| `GDBClient.set_breakpoint(address)` | ONE address: linear `int`, `"seg:off"` string, or a bare hex string — `"1000"` means `0x1000`, never decimal |
| `DosboxSession.set_breakpoint(seg, off)` | a `(seg, off)` pair, converted for you |

Both return the stub's acknowledgement. **Assert on it** — a protected-mode
breakpoint refusal reaches you as `Z0` answering `E01` instead of `OK`.

---

## 5. Bulk reads go through QMP `memdump`, not a loop of GDB `m` packets

One `memdump` call reads a whole range server-side and returns it in a single
reply. A recorded segment scan in a downstream consumer cost **7,168 GDB
round-trips** before this was understood — one per small chunk.

```python
gdb.halt()                                # REQUIRED, see below
data = qmp.memdump(0xFFEF0, 0x110)        # -> bytes
```

**`memdump` requires the CPU to be stopped.** It reads guest memory directly
off the QMP socket thread, so DOSBox-X refuses the request outright rather
than risk a torn read against the running emulation thread. You will see a
`QMPError` mentioning "stopped".

**Stop it with `gdb.halt()`, not `qmp.stop()`,** if you also intend to talk
GDB. While the emulator is QMP-stopped the GDB stub does not answer at all —
it is polled from the emulation thread — so `qmp.stop()` followed by any GDB
request is a deadlock against a client that has no read timeout (§7).

Without a `file=` argument the server base64-encodes the dump inline, capped
at 16 MB server-side. With `file=`, the dump is written on the machine running
DOSBox-X and the path is returned instead.

---

## 6. Arm break-on-exec BEFORE typing the program name

`qmp.debug_break_on_exec(True)` breaks at the entry point of the *next*
program DOS executes. Arm it, then type the name. Arming after the exec has
happened does nothing for that program.

**And know what arming does to the wire.** `debug_break_on_exec` arms *and
immediately activates* a breakpoint, whether or not any GDB client asked to
continue. When it fires while the CPU is free-running, the stub sends a
`$S05#b8` **nobody requested**, landing where `GDBClient` expects the `+` ACK
for its own next packet. That used to break the connection outright; the
client now diverts it to a queue instead (lokkju/dbxdebug#5, fixed, with a
live test).

So, verified behaviour:

- The GDB connection **survives** the break. The read that follows is still
  answered with its own bytes.
- Learn that the CPU stopped from **`gdb.take_pending_stops()`** — it returns
  the queued stop replies and empties the queue — or by polling
  **`qmp.query_status()["running"]`** on the separate QMP socket. Either
  works; the QMP poll is what you want when you are waiting rather than
  reacting.
- A plain `Z0` breakpoint armed while free-running is **inert** and is not a
  trigger — activation only happens on continue. This is why the ordinary
  breakpoint recipe (`halt` → `set_breakpoint` → `continue_execution`) is
  clean: the `S05` is the answer to your own `c`, so it is returned to you
  rather than queued.

---

## 7. Known hazards — one open, two fixed but still worth knowing

**Reads are bounded** (lokkju/dbxdebug#4, fixed). `GDBClient` arms a 30 s read
timeout on every read, not just the connect, and raises `GDBTimeoutError` — a
`TimeoutError` subclass — naming the packet that went unanswered. Override it
with `GDBClient(timeout=...)`; `timeout=None` restores unbounded blocking. The
interaction that made this easy to hit is unchanged: while the emulator is
QMP-stopped the GDB stub is not serviced, so `qmp.stop()` followed by a GDB
request cannot be answered. Use `gdb.halt()` when you also intend to talk GDB.

**A disturbed stream resynchronises, or refuses** (lokkju/dbxdebug#5, fixed).
An unrequested stop reply goes to `gdb.pending_stops` instead of being read as
an answer, and an abandoned exchange is drained before the next packet is
sent, so the request after a `GDBTimeoutError` gets its own reply. If that
drain cannot complete the client marks itself **permanently unusable** and
every later call raises `GDBDesyncError` — a loud failure, by design, instead
of someone else's bytes. Open a new client at that point.

- Keep GDB traffic strictly serialised: one request per reply, on one thread.
  The resynchronisation is only provable because the client never pipelines.
- Catch `GDBTimeoutError` where you would have hung before. It does not spend
  the connection; `GDBDesyncError` does.
- **Do not add a read-retry loop.** "Read until two consecutive reads agree"
  is the obvious workaround and it is wrong twice over: it doubles the
  round-trips on every read, and two identical consecutive requests mask a
  one-packet lag *perfectly*. It converts a detectable protocol fault into an
  undetectable one — which is why the fix went in the client's packet layer.

**One GDB client at a time** (lokkju/dbxdebug#8, open). The stub serves a
single GDB client. A second one completes the TCP handshake and is then never
serviced — no refusal, and nothing on the wire; with the read timeout above it
now fails after 30 s rather than hanging forever. Consequences:

- Running `dbxdebug mem` / `cpu` / `screen` as a **separate process** against a
  session that already holds its own client hangs that command. Use
  `DosboxSession(connect=False)` if you want the CLI to be the one client, or
  drive `session.gdb` from Python. Driven **in-process**, those groups borrow a
  client you hand them:
  `main(["cpu", "regs"], obj={GDB_CLIENT_KEY: session.gdb}, standalone_mode=False)`.
- `DOSVideoTools(gdb=session.gdb)` borrows a session's client and never closes
  it. `DOSVideoTools(host, port)` still opens and owns its **own** `GDBClient`,
  which is right only when it is the sole client (lokkju/dbxdebug#11).

**Unmapped memory is indistinguishable from zeroed memory.** The stub cannot
report a failed read as such; a region that is not backed reads back as zeros.
Verify an address against something known before trusting a zero result.

---

## 8. The config keys contain a space

If you write a conf by hand instead of letting the session render one:

```ini
[dosbox]
gdbserver = true
gdbserver port = 2159
qmpserver = true
qmpserver port = 4444
```

**`gdbserver port`, not `gdbport`.** The no-space forms are not options at
all. DOSBox-X's config parser finds no match, returns false with **no
warning**, and the property keeps its compiled-in default (2159 / 4444). Your
client then connects to whatever else happens to be listening there — on a
shared machine, possibly another agent's emulator. The observed failure is a
QMP error about invalid JSON, which looks like a server bug and is not.

Both port options are `OnlyAtStart`: changing them at runtime does nothing.

---

## 9. Reading the screen

`session.screen_lines(width=80, height=25)` decodes the VGA text framebuffer
at linear `0xB8000` through **GDB**, two bytes per cell, discarding the
attribute byte. It does not strip lines and passes non-printables through
unchanged (only `0x00` becomes a space) — so do not compare its output against
fixtures captured by a different decoder.

Because it is a GDB read, it inherits every hazard in §7. `wait_for_text`
polls it, and swallows exceptions from a not-yet-ready guest, which is why it
returns `None` rather than raising on failure.

For a PNG of the actual display rather than the text plane, use
`qmp.screendump()` — that goes over QMP and is unaffected by GDB state.

---

## References

- `references/recipes.md` — runnable patterns: start and wait for a real
  prompt, type with break-on-exec armed, bulk-read a region, walk stack
  frames, step out, capture the screen, reap strays.
- `references/troubleshooting.md` — indexed by symptom, for when something
  already looks wrong.
- `dosbox-x-debug-protocol` skill — the server side, for editing the C++.
- `docs/migration.md` in the `dbxdebug` repo — for porting code written
  against an older emulator build.
