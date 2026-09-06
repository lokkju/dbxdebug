# Recipes

Working patterns, built from APIs read at `dbxdebug` `5cd96ce88`. Anything not
demonstrated by the package's own live suite is marked **UNVERIFIED** —
those are constructed from read source, not from an observed run.

Common preamble for every Python snippet:

```python
from dbxdebug import DosboxSession, linear

# Sessions are headless by default: no window, no keyboard focus. Pass
# headless=False only when a human wants to watch the guest.
```

---

## 1. Check the host before launching anything

```bash
uv run dbxdebug doctor
```

```
[ok]   dosbox-x binary found: /path/to/dosbox-x
[ok]   remote debugging: appears compiled in (gdbserver/qmpserver strings found)
[info] host CPUs: 16 (rough concurrency ceiling)
[ok]   registry dir writable: /home/you/.cache/dbxdebug-sessions
[ok]   orphaned sessions: none
```

It never starts an emulator — every check is a filesystem probe, a static read
of the binary's bytes, or a read of the registry. Run it before fanning out
across concurrent sessions, and treat a non-empty orphan list as something to
clear first (recipe 8).

`DBXDEBUG_DOSBOX` picks a specific build; `DBXDEBUG_REGISTRY` moves the
registry directory (default `~/.cache/dbxdebug-sessions`).

---

## 2. Start a session and wait for a real prompt

```python
with DosboxSession(mounts={"c": host_dir}, label="mywork") as session:
    # GDBClient arms a 30s read deadline of its own, so an unanswered packet
    # raises GDBTimeoutError rather than hanging. Override it only if that
    # bound is wrong for your workload:
    #     if session.gdb is not None and session.gdb.sock is not None:
    #         session.gdb.sock.settimeout(120.0)

    # Readiness OBSERVED, not assumed. Returns seconds, or None on timeout or
    # on a dead process -- check it.
    observed = session.wait_for_text("C:\\>", timeout=60)
    assert observed is not None, "the guest never reached a prompt"

    print(session.gdb_port, session.qmp_port, session.pid, session.workdir)
```

Leaving the `with` block kills the emulator's whole process group, deletes the
scratch workdir, and removes the registry entry.

Useful arguments: `mounts={"c": path}` (omitted, an empty per-session
`workdir/c` is mounted as C:), `files=` to stage host files onto the first
mounted drive — an entry may be a `(host_path, guest_name)` pair to rename —
`autoexec=`, `conf=` for your own template, `cycles=` (default `"max"`),
`connect=False` for the handle without clients, `label=` to name the scratch
workdir, `keep_workdir=True` to inspect it afterwards.

**If you must read the screen straight after `start()`** rather than waiting
on text:

```python
session.assert_screen_readable()   # raises on a blank screen or the DOSBox-X banner
```

---

## 3. Run a program at boot, via autoexec

```python
with DosboxSession(
    mounts={"c": drive},
    autoexec=[f"mount c {drive}", "c:", "PROG.EXE"],
) as session:
    assert session.wait_for_text("READY", timeout=60) is not None
```

`program=` does **not** do this — it stages the file and records the name, but
never runs it, precisely so a break-on-exec caller can arm first.

---

## 4. Type a program name with break-on-exec armed

Order matters: arm, then type. Arming after the exec does nothing for that
program.

```python
qmp = session.qmp
assert qmp is not None

qmp.debug_break_on_exec(True)      # arms AND activates immediately
qmp.type_text("PROG\r")            # "\r" and "\n" both map to the ret key

# Wait for the stop over QMP: a separate socket, undisturbed by the break.
# The break fires an unsolicited $S05 on the GDB connection, which the client
# queues on gdb.pending_stops rather than reading as an answer.
import time
deadline = time.time() + 60
while time.time() < deadline and qmp.query_status()["running"]:
    time.sleep(0.2)
assert qmp.query_status()["running"] is False
```

**The GDB connection survives this.** Drain the unrequested stop with
`gdb.take_pending_stops()` — it returns the queued stop replies and empties
the queue — and carry on reading registers and memory. You can wait on the
GDB side instead of over QMP: `gdb.wait_for_stop(timeout=60)` returns the
stop reply, or None if none arrives. If you would rather
not deal with an out-of-band stop at all, the alternative is an ordinary
breakpoint (recipe 5), whose stop reply answers your own `c` and is returned
to you directly.

> **UNVERIFIED.** Halting GDB *before* arming, so the eventual `S05` answers a
> pending `continue_execution()`, is plausible from the source — QMP input
> events are drained in the GDB-halted branch of `Normal_Loop`, so queued keys
> are delivered while halted — but nothing in the package demonstrates it. Do
> not treat it as a supported pattern without testing it yourself.

`type_text(text, delay=0.05)` handles shift for uppercase and punctuation, and
logs-and-skips a character it cannot map. For chords use
`qmp.send_key(["ctrl", "c"])`, or the constants: `from dbxdebug import CTRL_C,
ENTER, ESCAPE` (each is a list of qcodes).

---

## 5. Set a breakpoint and stop on it

```python
gdb = session.gdb
assert gdb is not None

gdb.halt()                                   # `?` -- stop reply, CPU paused
assert session.set_breakpoint(seg, off)      # (seg, off) -> linear for you
reply = gdb.continue_execution()             # blocks until the stop reply

assert reply == b"S05", f"unexpected stop reply {reply!r}"
assert gdb.linear_pc() == linear(seg, off)   # cs*16 + eip, NOT regs["eip"]
assert session.remove_breakpoint(seg, off)
```

Assert on the `set_breakpoint` return: `Z0` answering anything but `OK` (a
protected-mode refusal is `E01`) surfaces only there. And assert on the
**address**, not just the stop — the historical bug's signature was a
breakpoint that answered `OK` and never fired, and a stop can come from
something else.

`GDBClient.set_breakpoint` takes one address in any of three forms; the
session helper takes the pair:

```python
gdb.set_breakpoint(0x10020)            # linear int
gdb.set_breakpoint("1000:0020")        # seg:off string, same address
gdb.set_breakpoint("1000")             # bare hex -> 0x1000, never decimal
session.set_breakpoint(0x1000, 0x0020)
```

---

## 6. Read a memory region efficiently

**One QMP `memdump`, not a loop of GDB `m` packets.** A downstream segment
scan cost 7,168 round-trips before this was understood.

```python
data = session.read_bulk(0xF0000, 0x10000)   # -> 65536 bytes, one round-trip
assert isinstance(data, bytes)
```

Measured on this build, one 64 KB segment: **2.8 ms** through `read_bulk` -- the status query, the halt and the resume included -- against **33.5 ms** for the same 65,536 bytes through 64 one-kilobyte `gdb.read_memory` calls. 12x against a running guest; 2.6 ms against 5.5 ms, so 2.6x, when the CPU is already halted and the loop has no emulation competing with it. Both ends set TCP_NODELAY now; before that a round-trip cost ~82 ms of Nagle stall and the same comparison read 42x. Both paths return
identical bytes.

`read_bulk` halts through GDB, dumps, and resumes. A CPU that was **already**
stopped — a breakpoint, the interactive debugger, or a QMP stop — is left
stopped: it resumes only what it halted itself.

Driving `qmp.memdump` by hand means holding two rules yourself. `memdump`
reads guest memory off the QMP socket thread, so DOSBox-X refuses it outright
while the guest runs rather than risk a torn read — you get
`CpuNotStoppedError`, a `QMPError` subclass whose message names the fix.

**Halt with `gdb.halt()`, not `qmp.stop()`,** if you will also talk GDB: while
the emulator is QMP-stopped the GDB stub does not answer at all, so every GDB
request in that combination burns the read timeout and raises
`GDBTimeoutError`. Put the CPU back with `gdb.resume()` — `continue_execution()`
blocks until the next stop reply, which never comes if nothing is armed to
produce one.

Server-side file variant, for a dump you do not want on the wire:

```python
path = qmp.memdump(0x0, 0x100000, file="/tmp/lowmem.bin")   # -> the path
```

Small reads are fine over GDB. The client resynchronises after an abandoned
reply and refuses outright when it cannot, so a short read is no longer the
only symptom you have — but checking the length is still cheap:

```python
want = 16
got = gdb.read_memory(0xFFFF0, want)
assert len(got) == want
```

---

## 7. Walk stack frames, and step out of one

Real-mode BP chain: `[BP]` saved BP, `[BP+2]` return offset, `[BP+4]` return
segment **if the call was far**. All read through SS. Sets no breakpoints.

```python
from dbxdebug.frames import walk_frames, steps_out, FrameWalkError

for frame in walk_frames(gdb, max_depth=32):     # innermost outward
    print(frame.depth, hex(frame.bp), hex(frame.return_off), hex(frame.return_seg))
```

`walk_frames` **never raises**. It stops and returns what it has on: a zero
saved BP, a saved BP not strictly above the current one (which is also what
terminates a cyclic chain), a short or failed read, or `max_depth`. An empty
list means the very first register read failed.

`frame.return_seg` is meaningful only for a far call, and the module does not
guess — for a near call the same word is whatever the caller left there
(typically the first pushed argument), reported unchanged. You must know your
target's calling convention.

```python
# Clear every breakpoint FIRST. A breakpoint hit during one of these single
# steps emits an unsolicited stop reply; the client queues it rather than
# desyncing, but the stop you get is not the step you asked for.
assert gdb.remove_breakpoint(body_addr)

steps_out(gdb, timeout=20.0)     # -> the final step's stop reply, as bytes
```

`steps_out` records the entry BP and single-steps until `SP & 0xFFFF` is
strictly greater than `BP + 2` — past the return-address slot, which only the
`ret` itself reaches, not the `pop bp` or `leave` before it. It raises
`FrameWalkError` if `SP > BP` on entry, or if neither bound (`timeout`,
`max_steps`) is met before the frame returns. It never returns silently
without having stepped out.

Bounds worth knowing: it always executes at least one instruction (the check
runs after each step); called at a procedure's first instruction it measures
the *caller's* frame; and a callee that jumps to a shared epilogue popping
extra registers stops it early.

---

## 8. Capture the screen

**Text plane, over GDB** — the VGA text framebuffer at linear `0xB8000`, two
bytes per cell, attribute discarded:

```python
for line in session.screen_lines():          # width=80, height=25 by default
    print(line.rstrip())
```

Lines are **not** stripped and non-printables pass through unchanged (only
`0x00` becomes a space), so do not diff this against fixtures produced by a
different decoder.

**A PNG of the actual display, over QMP** — unaffected by GDB state:

```python
result = qmp.screendump()                    # base64 PNG inline in `return`
result = qmp.screendump(file="/tmp/shot.png")  # written on the emulator's host
```

**Lend the client, do not open a second one.** The stub serves one GDB client
at a time, so a second connection blocks forever in `qSupported`
(lokkju/dbxdebug#8). Inside a session, hand yours over:

```python
from dbxdebug.cli import GDB_CLIENT_KEY, main
from dbxdebug.video import DOSVideoTools

with DOSVideoTools(gdb=session.gdb) as video:  # borrowed; not closed here
    lines = video.screen_dump()

main(["screen", "show"], obj={GDB_CLIENT_KEY: session.gdb}, standalone_mode=False)
```

`DOSVideoTools(host, port)` — with no `gdb=` — still opens and owns its own
client, which is right only when it is the sole client. Running the `dbxdebug
screen` / `mem` / `cpu` commands as a separate process is always a second
connection: launch with `DosboxSession(connect=False)` if you want that process
to be the one client.

Standalone (the CLI owns the only connection):

```bash
dbxdebug screen show --port 55847
dbxdebug screen capture -f html -o snapshot --port 55847
dbxdebug screen record -d 60 -r 30 -o run.capture.gz --port 55847
```

`screen record` takes `-d × -r` samples rather than watching the clock, and
each sample is a GDB round-trip; when those cannot keep up the run overruns
(`-d 60 -r 30` took ~149 wall seconds on one measured host). Lower `-r` if
wall time matters.

---

## 9. Reap strays

**Never `pkill -f dosbox-x` or `killall`.** It kills every emulator on the
machine, including other agents' and other people's.

```bash
dbxdebug session list
```

```
     PID     PGID    GDB    QMP      AGE STATE       OWNER  WORKDIR
--------------------------------------------------------------------
 3549359  3549359  55847  44097     412s ORPHAN    3549001  /tmp/mywork_ab12cd
```

`ORPHAN` = the emulator is alive but the process that launched it is gone.
`stale` = the registry file outlived its process.

```bash
dbxdebug session reap --dry-run     # report only
dbxdebug session reap               # orphans and stale entries
dbxdebug session reap --max-age 300 # skip anything younger than 5 minutes
dbxdebug session reap --all         # also live, still-owned sessions
```

Use `--max-age` when other agents are actively launching: it keeps you from
reaping a session that was registered a second ago and is still being started.

Identification is pid **plus** `/proc` start time, so a recycled pid is never
mistaken for the session. From Python:

```python
from dbxdebug.registry import list_sessions, reap, format_table

print(format_table(list_sessions()))
rows = reap(dry_run=True)            # one report row per session considered
```

---

## 10. Attach to an emulator someone else started

The servers need these keys — **note the spaces**, `gdbport` is not an option
and is silently ignored:

```ini
[dosbox]
gdbserver = true
gdbserver port = 2159
qmpserver = true
qmpserver port = 4444
```

```python
from dbxdebug import GDBClient, QMPClient

with GDBClient(port=2159) as gdb, QMPClient(port=4444) as qmp:
    gdb.read_registers()
    qmp.type_text("DIR\r")
```

Both clients connect (and, for GDB, complete the `qSupported` handshake)
inside `__init__` — there is no separate `.connect()`. `GDBClient` takes a
`timeout` (30 s by default, applied to every read); `QMPClient` has none.

You get no ephemeral ports, no workdir isolation, no registry entry and no
guaranteed teardown here. Prefer a `DosboxSession` whenever you are the one
launching.

---

## 11. Concurrency

Measured on a 16-core host: 8 concurrent sessions, every one reaching the DOS
prompt, boot time up 6 % and no throughput cost. There is no single-instance
guard, SDL's dummy video/audio devices do not serialise instances, and nothing
fights over a port.

**What that does not establish:** those guests sat *idle* at the prompt, and an
idle DOSBox-X costs almost nothing because it detects the keyboard wait and
stops burning host CPU. With a program actually running, one instance measured
0.19 host cores. `cycles = "max"`, which by construction takes what it can
get, is unmeasured. Treat 8 as a floor established for **launching**, not a
ceiling established for **working**.

Run `dbxdebug doctor` first; its CPU count is the rough ceiling to plan
against.
