# dbxdebug

[![CI](https://github.com/lokkju/dbxdebug/actions/workflows/test.yml/badge.svg)](https://github.com/lokkju/dbxdebug/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/dbxdebug)](https://pypi.org/project/dbxdebug/)
[![Python](https://img.shields.io/pypi/pyversions/dbxdebug)](https://pypi.org/project/dbxdebug/)
[![License](https://img.shields.io/github/license/lokkju/dbxdebug)](https://github.com/lokkju/dbxdebug/blob/main/LICENSE)

Drive a DOSBox-X emulator from Python: launch one, break on an address, read
its memory and registers, type at it, and read its screen back.

`DosboxSession` is the entry point. It launches the emulator on ports nobody
else is using, in a workdir nobody else shares, connects both debug clients,
and guarantees teardown. The bare `GDBClient` / `QMPClient` are still there
for attaching to an emulator you started some other way.

## Requirements

An emulator build with the `gdbserver` and `qmpserver` remote-debug features
compiled in. **These are not upstream DOSBox-X** -- they are additions to this
project's fork, and nothing here works against a stock build. `dbxdebug
doctor` tells you whether the binary it would launch has them.

Python 3.11 or newer.

## Installation

```bash
uv add dbxdebug
```

**Check what that gave you before writing any of the code below.** The version
on PyPI at the time of writing is 0.2.1, and its sdist ships the original
client surface only -- no `session`, no `addressing`, no `frames`, no
`registry`, no `paths`, no `doctor`. Everything this README describes needs a
release newer than that, which has not been published yet. Until it is, depend
on the repository directly (`uv add --editable ../dbxdebug`, or a git
dependency), and confirm:

```bash
uv run python -c "import dbxdebug.session, dbxdebug.addressing, dbxdebug.frames; print('ok')"
```

An `ImportError` there means you are on the published release, and the rest of
this page does not apply to it yet.

For working on dbxdebug itself, `uv sync`.

## Quick start

```python
from dbxdebug import DosboxSession

# Headless by default: no window, no keyboard focus, no audio device.
# Pass headless=False when you want to watch the guest.
with DosboxSession() as session:
    print(f"pid={session.pid} gdb={session.gdb_port} qmp={session.qmp_port}")

    gdb = session.gdb
    assert gdb is not None  # connect=True is the default

    regs = gdb.read_registers()
    # regs["eip"] is an OFFSET within CS, not a linear address.
    print(f"cs={regs['cs']:#06x} eip={regs['eip']:#06x} linear_pc={gdb.linear_pc():#07x}")

    # Memory takes a LINEAR address. 0xB8000 is the VGA text framebuffer.
    cell = gdb.read_memory(0xB8000, 4)
    print(f"first two cells at 0xb8000: {cell.hex()}")

    print(session.screen_lines()[0].rstrip())
```

```
pid=3549359 gdb=55847 qmp=44097
cs=0xf000 eip=0xd186 linear_pc=0xfd186
first two cells at 0xb8000: ba1f201f
º Welcome to DOSBox-X !                         v2025.12.01, Linux SDL1 64-bit º
```

Leaving the `with` block kills the emulator's whole process group, deletes the
scratch workdir, and removes the registry entry. `atexit` and SIGINT/SIGTERM
handlers repeat that teardown for a process that leaves by some other door.

### Headless is the default

A session runs with SDL's `dummy` video and audio drivers, so it opens **no
window**, takes no keyboard focus, and claims no audio device. Starting one
does not take over the display of whoever is at the machine, which is what
makes a test suite -- or several sessions at once -- usable on a workstation.

What you give up is real: you cannot watch the guest. `headless=False` gives
you a normal window, and that window will take the focus of whoever is at the
keyboard, so it is for looking at one session, not for a suite.

The debug surface is unaffected. `screen_lines()` reads guest video memory
over GDB and `qmp.screendump()` renders through DOSBox-X's own capture path;
`dummy` removes the window, not the rendering. Compared live, a headless
session and a windowed session showing the same screen returned
**byte-identical** 720x400 PNGs and identical text lines.

`env=` still works and takes precedence over the headless variables, so
`DosboxSession(headless=True, env={"SDL_VIDEODRIVER": "x11"})` keeps the dummy
audio driver and gets an x11 window. Precedence, lowest to highest: inherited
`os.environ`, then the headless variables, then `env`.

Useful `DosboxSession` arguments: `mounts={"c": path}`, `program=` and
`files=` to stage host files onto the first mounted drive, `autoexec=`,
`conf=` for your own conf template, `cycles=`, `headless=False` for a visible
window, `connect=False` for the handle without clients, `boot_settle=`
(default 2.5s -- the debug ports accept long before the guest reaches a
prompt), and `label=` to name the scratch workdir.
Conveniences on the handle: `screen_lines()`, `read_bulk()`, `wait_for_text()`,
`assert_screen_readable()`, `running`, `set_breakpoint()`,
`remove_breakpoint()`.

### Reading a region: `session.read_bulk(address, length)`

One QMP `memdump` reply instead of thousands of GDB `m` round-trips.
Measured on this build, one 64 KB segment: **2.8 ms** through `read_bulk` against **33.5 ms** for the same bytes through 64 one-kilobyte `gdb.read_memory` calls -- 12x against a running guest, and 2.6x (2.1 ms against 5.5 ms) when the CPU is already halted, where the loop has no emulation competing with it. Both paths return
identical bytes; the live suite asserts it.

```python
data = session.read_bulk(0xF0000, 0x10000)   # -> 65536 bytes
```

It exists because the raw sequence has two traps. `memdump` is refused
while the CPU runs, and the obvious way to stop it -- `qmp.stop()` --
parks the emulation thread that services the GDB stub, so the dump
succeeds and every later GDB request goes unanswered. `read_bulk` halts
through GDB, dumps, and resumes. If the CPU was ALREADY stopped (a
breakpoint, the interactive debugger, or a QMP stop) it takes the dump
and leaves it stopped: it resumes only what it halted itself.

## Addressing -- read this once

This is the most consequential behaviour in the package, and the thing most
likely to silently break code written against an older emulator build.

**Breakpoints and memory take LINEAR addresses.** `seg * 16 + off`, which is
what `addressing.linear(seg, off)` computes. `Z0`/`z0` (breakpoints) and
`m`/`M` (memory) all use the same encoding.

**`addressing.bp_addr` raises rather than packing a far pointer.** It exists
only to fail loudly, because the historical helper it replaces packed
`(seg << 16) | off`, and the stub answers `OK` to both readings -- a silently
misplaced breakpoint above 64 KB is indistinguishable from a correct one by
its response. `addressing.parse_address` rejects an integer that looks packed
for the same reason.

**`read_registers()["eip"]` is an OFFSET within CS**, not a linear address.
Use `gdb.linear_pc()` (or `addressing.linear_pc(register_list)`) for the
linear program counter.

```python
from dbxdebug.addressing import linear, bp_addr, parse_address, PackedAddressError

linear(0x1000, 0x0020)          # -> 0x10020
bp_addr(0x1000, 0x0020)         # -> PackedAddressError, always
parse_address(0x10000020)       # -> PackedAddressError: looks like a packed far pointer
```

Note the signature difference between the two `set_breakpoint`s:

| Call | Takes |
|---|---|
| `GDBClient.set_breakpoint(address)` | ONE address: a linear `int`, a `"seg:off"` string, or a bare hex string -- `"1000"` means `0x1000`, never decimal 1000 |
| `DosboxSession.set_breakpoint(seg, off)` | a `(seg, off)` pair, converted with `addressing.linear` for you |

If you are porting code written against an older build, read
[docs/migration.md](docs/migration.md) -- it covers what breaks, how each
failure presents, and what to write instead.

## The capability handshake

A current emulator build advertises two vendor tokens in its `qSupported`
reply:

| Token | Means |
|---|---|
| `dosbox-x-linear-bp+` | `Z0`/`z0` take a linear address. Builds without it split the argument as a packed far pointer (`seg = addr >> 16`), so a breakpoint above 64 KB answers `OK` and never fires |
| `dosbox-x-eip-offset+` | The `g` packet reports EIP as an offset within CS. Builds without it returned `SegPhys(cs) + reg_eip`, so a `g`/`G` round-trip silently moved the program counter |

Neither semantics change is detectable by probing -- `Z0` answers `OK` under
either reading, and both `eip` conventions produce a plausible integer -- so
the advertisement is the only signal, and `GDBClient` treats it as
load-bearing. By default `GDBClient.__init__` raises `IncompatibleStubError`
unless `dosbox-x-linear-bp+` is present. Pass
`GDBClient(require_capabilities=False)` to drive an older build anyway, and
`client.require_linear_breakpoints()` to re-run the check by hand.

`dosbox-x-eip-offset+` is *not* separately enforced; it lands in
`client.capabilities` alongside everything else the stub advertised, for a
caller that wants to branch on it.

`DosboxSession` builds its clients with the default, so a session against an
old build fails at `start()` rather than at the first breakpoint.

## What is in the package

| Module | For |
|---|---|
| `dbxdebug.session` | `DosboxSession` -- launch, connect, tear down. Also `DEFAULT_CONF`, `render_conf`, `DosboxLaunchError` |
| `dbxdebug.gdb` | `GDBClient`, `IncompatibleStubError`, `REGISTER_NAMES` |
| `dbxdebug.qmp` | `QMPClient`, `QMPError`, `CpuNotStoppedError` -- keys, `memdump`, `screendump`, save/load state, `stop`/`cont`, `debug_break_on_exec` |
| `dbxdebug.addressing` | `linear`, `linear_pc`, `parse_address`, `bp_addr`, `PackedAddressError` |
| `dbxdebug.frames` | `walk_frames`, `steps_out`, `Frame`, `FrameWalkError` |
| `dbxdebug.registry` | `list_sessions`, `reap`, `format_table`, `free_port`, `kill_group` |
| `dbxdebug.paths` | `find_dosbox_x`, `configured_dosbox_x_path` |
| `dbxdebug.doctor` | `run()`, `DoctorReport` -- host readiness; never starts an emulator |
| `dbxdebug.video` / `.html` / `.capture_io` | `DOSVideoTools`, HTML rendering, `ScreenRecorder`, `load_capture` |
| `dbxdebug.keyboard` / `.dbx_kbd` | key-chord helpers and constants (`CTRL_C`, `ctrl_key`, `DBX_KEY`, ...) |

### The export rule

Every module declares its supported surface in `__all__`, and
`dbxdebug/__init__.py` re-exports the union of the **library** modules'
`__all__`. So everything in the table above except the last three rows'
worth of command machinery is importable straight from `dbxdebug`:

```python
from dbxdebug import DosboxSession, GDBClient, QMPClient, linear, walk_frames
```

The exceptions are `dbxdebug.cli`, `dbxdebug.registry` and `dbxdebug.doctor`
-- the `dbxdebug` command's own machinery. They act on the host's whole set
of sessions rather than on the one you launched, and their names mean
nothing unqualified: a package root should not own `run`, `reap`,
`list_sessions` or `free_port`. Import those from their modules:

```python
from dbxdebug.registry import list_sessions, reap
from dbxdebug import doctor

report = doctor.run()
```

Leaving `cli` out is also what keeps `import dbxdebug` free of `click`.

Module-path imports keep working everywhere, and the examples below use
whichever form reads better in context -- `addressing.bp_addr` says more
about `bp_addr` than a bare name does. `tests/test_exports.py` holds the
root to the union so the two cannot drift.

### Locating the emulator

`paths.find_dosbox_x()` and `paths.configured_dosbox_x_path()` resolve the
binary in one order, used by both `DosboxSession` and `doctor` so the two can
never disagree: the `DBXDEBUG_DOSBOX` environment variable first (trusted
exactly as given, never silently swapped for something found on `PATH`), then
a conventional checkout path, then `PATH`.

```bash
export DBXDEBUG_DOSBOX=/path/to/your/dosbox-x
```

`DBXDEBUG_REGISTRY` likewise overrides the session registry directory, which
defaults to `~/.cache/dbxdebug-sessions`.

### Stack frames

`frames` walks the real-mode BP chain -- `[BP]` saved BP, `[BP+2]` return
offset, `[BP+4]` return segment *if the call was far* -- reading through SS.
It sets no breakpoints.

```python
from dbxdebug.frames import walk_frames, steps_out

for frame in walk_frames(gdb):            # innermost outward, bounded by max_depth
    print(frame.depth, hex(frame.bp), hex(frame.return_off))

steps_out(gdb)                            # single-step until the current frame returns
```

`walk_frames` never raises: it stops and returns what it has on a zero saved
BP, a saved BP that is not strictly above the current one (which is also what
terminates a cyclic chain), a short or failed read, or `max_depth`.

`steps_out` is **a heuristic over SP**, with bounds worth knowing before you
rely on it. It records the entry BP and steps until `SP & 0xFFFF` is strictly
greater than `BP + 2` -- past the return-address slot, which only the `ret`
itself reaches, not the `pop bp` or `leave` before it. Consequences:

* it raises `FrameWalkError` if `SP > BP` on entry (no frame pointer
  established, or a stale BP) rather than returning after a single step;
* a callee that pops BP and jumps to a shared epilogue popping further
  registers raises SP past `BP+2` while still inside the callee, and this
  stops there, early. Telling that apart from a real return needs instruction
  decoding, which this does not do;
* called at a procedure's first instruction, before the prologue has run, BP
  still belongs to the caller and this measures the caller's frame;
* **clear every breakpoint first.** A breakpoint hit during one of these steps
  makes the stub emit an unsolicited stop reply. The connection no longer
  desyncs on one -- it is queued on `gdb.pending_stops` -- but the stop you
  get is still not the step you asked for, so the walk ends up measuring a
  frame you did not mean to be in. See Known hazards.

## CLI

```
dbxdebug
├── mem              # Memory operations (alias: gdb)
│   ├── read         # Read LENGTH bytes from ADDRESS
│   └── write        # Write hex bytes to ADDRESS
│
├── cpu              # CPU registers and execution control
│   ├── regs         # Display registers (also prints the linear PC)
│   ├── break        # Set breakpoint at ADDRESS
│   ├── delete       # Remove breakpoint at ADDRESS
│   ├── step         # Single step
│   ├── cont         # Continue execution
│   └── halt         # Break into the debugger
│
├── key              # Keyboard input (alias: qmp)
│   ├── send         # Key chord (e.g. ctrl c)
│   ├── type         # Type a text string
│   ├── down         # Press and hold a key
│   ├── up           # Release a key
│   └── list         # List QMP commands the server offers
│
├── screen           # Screen capture
│   ├── show         # Display the 80x25 text screen on stdout
│   ├── capture      # Save one frame to a file (-f raw|html|text)
│   ├── record       # Multi-frame timed capture
│   ├── watch        # Real-time display
│   ├── info         # Video mode, BIOS ticks
│   └── colors       # Analyze the color palette
│
├── session          # Sessions tracked in the local registry
│   ├── list         # What the registry knows, and whether it is orphaned
│   └── reap         # Kill orphans and delete their workdirs
│
└── doctor           # Host readiness check; never starts an emulator
```

`mem`, `cpu` and `screen` talk to the GDB server (`--port`, default 2159);
`key` talks to QMP (`--port`, default 4444). A `DosboxSession` picks ephemeral
ports instead, so pass `--port` its `gdb_port` / `qmp_port` to point the CLI
at one -- and see the single-client hazard below before you do.

```bash
dbxdebug doctor
dbxdebug session list
dbxdebug session reap --dry-run

dbxdebug mem read b800:0000 16 --hex
dbxdebug mem write 0x1000 90909090
dbxdebug cpu regs
dbxdebug cpu break 1000            # bare hex: 0x1000, not decimal 1000
dbxdebug cpu step
dbxdebug key send ctrl c
dbxdebug key type "Hello World!"
dbxdebug screen show
dbxdebug screen capture -f html -o snapshot
dbxdebug screen record -d 60 -r 30 -o session.capture.gz
```

`screen record` takes `-d` x `-r` samples rather than watching the clock, and
each sample is a GDB round-trip. When those cannot keep up with `-r` the
recorder never sleeps and the run overruns: `-d 60 -r 30` captured its 1800
frames in about 149 wall seconds here. Lower `-r` if wall time matters.

`dbxdebug doctor` on a ready host:

```
[ok]   dosbox-x binary found: /path/to/dosbox-x
[ok]   remote debugging: appears compiled in (gdbserver/qmpserver strings found)
[info] host CPUs: 16 (rough concurrency ceiling)
[ok]   registry dir writable: /home/you/.cache/dbxdebug-sessions
[ok]   orphaned sessions: none
```

## Attaching to an emulator you started yourself

`DosboxSession` writes its own conf. If you are launching DOSBox-X by hand
instead, the servers need these keys:

```ini
[dosbox]
gdbserver = true
gdbserver port = 2159
qmpserver = true
qmpserver port = 4444
```

**The port keys need the space.** `gdbserver port`, not `gdbport`. DOSBox-X
silently ignores the no-space form, leaves the server on its compiled-in
default port, and your client then connects to whatever else happens to be
listening there -- possibly a different emulator entirely.

Then:

```python
from dbxdebug import GDBClient, QMPClient, DOSVideoTools, CTRL_C

with GDBClient() as gdb:                       # localhost:2159
    gdb.read_registers()
    gdb.read_memory("b800:0000", 4000)
    gdb.set_breakpoint(0x10020)                # LINEAR
    gdb.set_breakpoint("1000:0020")            # the same address, seg:off form
    gdb.step()
    gdb.continue_execution()
    gdb.wait_for_stop(timeout=30.0)            # a stop NOBODY asked for

with QMPClient() as qmp:                       # localhost:4444
    qmp.send_key(CTRL_C)
    qmp.type_text("Hello World!")

with DOSVideoTools() as video:                 # owns its own client
    lines = video.screen_dump()
    lines, ticks = video.screen_dump_with_ticks()

with DOSVideoTools(gdb=session.gdb) as video:  # borrows a session's client
    lines = video.screen_dump()                # and never closes it
```

## Known hazards

One open defect, and two that are fixed but still shape how you should write
against this library.

**Unanswered GDB packets: bounded, not silent** (was
[#4](https://github.com/lokkju/dbxdebug/issues/4), fixed). `GDBClient` arms a
30 s read timeout on every read, not just the connect, and raises
`GDBTimeoutError` naming the packet that went unanswered. Override it per
client with `GDBClient(timeout=...)`, or pass `timeout=None` for the old
unbounded blocking. The underlying interaction is unchanged and still worth
knowing: while the emulator is QMP-stopped the GDB stub is not serviced at
all, so `qmp.stop()` followed by any GDB request cannot be answered. It now
fails in 30 s with a message instead of deadlocking. To read memory, reach
for `session.read_bulk()`, which halts over GDB for you; otherwise halt
with `gdb.halt()` rather than stopping over QMP. A `memdump` refused for
this reason now raises `CpuNotStoppedError` (a `QMPError`) naming the fix,
rather than surfacing the stub's refusal alone.

**Stream desync: resynchronised, or refused** (was
[#5](https://github.com/lokkju/dbxdebug/issues/5), fixed). Both triggers were
reproduced against a live build -- an unsolicited `$S05` stop reply (QMP
break-on-exec fires one nobody asked for) and a timed-out request leaving its
reply in the stream -- and both are handled at the framing layer:

* an unrequested stop reply is diverted to `gdb.pending_stops` instead of
  being read as an answer. Drain it with `gdb.take_pending_stops()`, or wait
  on it with `gdb.wait_for_stop(timeout=...)`; both read the socket
  themselves, so no other request is needed to shake a stop loose
  ([#18](https://github.com/lokkju/dbxdebug/issues/18), fixed). The queue
  keeps the most recent 64. This is how you learn the CPU stopped without
  polling QMP;
* an abandoned exchange is drained before the next packet is sent, so the
  request after a `GDBTimeoutError` gets its own reply rather than the
  previous one's;
* if that drain cannot complete, the client marks itself **permanently
  unusable** and every later call raises `GDBDesyncError`. That is
  deliberate: a loud failure beats a plausible wrong answer. Open a new
  `GDBClient`.

Still true, and still worth doing: keep GDB traffic serialised on one thread,
and never add a read-retry loop. Two identical consecutive requests mask a
one-packet lag perfectly, so retrying would look like it worked whether or
not the stream had shifted.

**One GDB client at a time**
([#8](https://github.com/lokkju/dbxdebug/issues/8)). The stub serves a single
GDB client. A second one completes the TCP connect and then never gets its
`qSupported` reply -- no refusal, and nothing on the wire. Two things changed
independently: it now fails after the read timeout instead of hanging forever
([#5](https://github.com/lokkju/dbxdebug/issues/5)), and nothing in this
package opens a competing connection behind your back any more
([#11](https://github.com/lokkju/dbxdebug/issues/11)). The stub limitation
itself is still open ([#8](https://github.com/lokkju/dbxdebug/issues/8)).

Lend the session's client out instead of opening a second one:

```python
from dbxdebug.cli import GDB_CLIENT_KEY, main
from dbxdebug.video import DOSVideoTools

with DosboxSession(...) as session:
    with DOSVideoTools(gdb=session.gdb) as video:   # borrowed, not reopened
        lines = video.screen_dump()

    # the CLI, driven in-process, borrows the same client
    main(["screen", "show"], obj={GDB_CLIENT_KEY: session.gdb}, standalone_mode=False)
```

A borrowed client is never closed by the borrower -- the session stays its
owner. `DOSVideoTools()` with no `gdb=` still builds and closes its own, which
is the right thing when it is the only client, and so does a `dbxdebug` command
run as a separate process. Running the CLI as a separate process against a
session that holds its own client is still a second connection however it is
spelled, so it now fails on the timeout rather than working: use
`DosboxSession(connect=False)` there, or drive the session's `session.gdb`
from Python.

QMP is a separate socket and is undisturbed by any of this, which is why
`qmp.query_status()` is the way to learn that the CPU stopped.

## Testing

```bash
uv run pytest                                      # launches no emulator; what CI runs
uv run pytest -m integration tests                 # every test that launches one
uv run pytest -m integration tests/integration -v  # just the live suite
```

The `integration` marker means "this test launches a real emulator". Every
test carrying it is deselected from the default run -- CI has no emulator, and
a developer machine has one that must not be started unasked. Nearly all of
them live in `tests/integration/`; `tests/test_session.py` carries one more,
for the `start()`/`stop()` lifecycle.

The live tests in `tests/integration/` launch a headless DOSBox-X per test and
prove the library actually drives one: the vendor GDB capabilities, `eip` as an
offset rather than a linear address, a breakpoint above 64 KB firing, `memdump`
agreeing with GDB reads and refusing while the CPU runs, `frames.steps_out`
stopping after a real 16-bit `ret`, and what the GDB client does when the
stream is disturbed -- an unrequested stop reply queued rather than read as an
answer, an abandoned reply drained rather than handed to the next request, and
a request the stub will never answer bounded rather than deadlocked. The
framing paths a live emulator will not produce on demand are covered against a
fake socket in `tests/test_gdb_framing.py`.
The binary is located with `dbxdebug.paths.find_dosbox_x`
-- set `DBXDEBUG_DOSBOX` to choose a specific build -- and the tests skip when
none is found.

One test in `tests/integration/test_headless.py` is skipped even under the
`integration` marker: the one that compares a headless screen capture against
a windowed one has to launch a real window, which takes the keyboard focus of
whoever is at the machine. Set `DBXDEBUG_ALLOW_WINDOWED=1` to run it.

The other gates:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

## License

[Polyform Shield 1.0.0](LICENSE).
