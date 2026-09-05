# QMP command surface

Read from `src/debug/qmp.cpp` at commit `a4ff6a8cd`. `QMPServer::process_command`
(`qmp.cpp:401`) is the single dispatch point; there is no command handling
anywhere else.

## Transport

- Line-ish JSON over TCP. Every reply ends `\r\n` (`send_response`, `:354`).
- On connect the server sends a greeting before reading anything
  (`send_greeting`, `:347`):
  ```json
  {"QMP": {"version": {"qemu": {"micro": 0, "minor": 0, "major": 0}, "package": "DOSBox-X"}, "capabilities": ["oob"]}}
  ```
  The version triple is `0.0.0` — this is not QEMU and does not pretend to be.
  **`oob` is advertised but not implemented**: there is no out-of-band
  execution path, no `id` handling, and no `exec-oob` support anywhere in the
  file. Commands are strictly serial.
- `receive_command` (`:369`) brace-counts to find one complete JSON object,
  tracking string context so braces inside strings do not confuse it. It does
  **not** handle escaped quotes correctly beyond a single `\"` check.
- One client at a time (`listen(server_fd, 1)`); `handle_client` loops until
  `recv` returns 0.
- **Success** is `{"return": {}}` (`send_success`, `:360`) or a handler-built
  `{"return": {...}}`.
  **Failure** is `{"error": {"class": "<class>", "desc": "<text>"}}`
  (`send_error`, `:364`). Only two classes are ever used: `CommandNotFound`
  and `GenericError`.
- **There is no `id` echo.** A client cannot correlate replies to requests
  except by order. This is what makes a timed-out request permanently
  desynchronising — see `history.md`.

## Argument parsing — read this before adding a command

`extract_string` / `extract_int` / `extract_bool` / `extract_array`
(`qmp.cpp:153-236`) are substring scanners, not a JSON parser:

- They find the **first** occurrence of `"key"` anywhere in the buffer they are
  given, including inside a nested object or inside a string value.
- `extract_string` returns "" for anything that is not a quoted scalar — so
  `extract_string(cmd, "arguments")` on a real command **always returns ""**,
  because `arguments` is an object. Every handler that needs arguments
  therefore brace-matches the `"arguments"` object by hand first; see
  `handle_memdump` (`:651-667`) and the four near-identical copies in
  `handle_screendump` (`:765`), `handle_savestate` (`:858`), `handle_loadstate`,
  `handle_system_reset` and `handle_debug_break_on_exec`.
- `extract_array` finds the first `[` after the key and the first `]` after
  that — **a nested array inside the value terminates it early**.
- `extract_int` runs `std::stoi` on the tail of the buffer, so it silently
  truncates values past `INT_MAX` to the default and cannot read a negative
  sentinel distinctly.

Follow the existing pattern rather than inventing a new one, and do not assume
key uniqueness.

---

## Dispatched commands

### `qmp_capabilities`

Arguments: none (ignored). Reply `{"return": {}}`.
No state change beyond acknowledging — there is no capability negotiation.

### `send-key`

Arguments:
- `keys` (array, required) — objects of the form
  `{"type": "qcode", "data": "<qcode>"}`. Non-`qcode` types are skipped.
- `hold-time` (int, optional, default 100) — **parsed and discarded**
  (`:507`, cast to void). Press and release are both queued immediately;
  the gap between them is one drain of the input queue, not `hold-time` ms.

Empty or absent `keys` → `GenericError` "No keys specified".
Unknown qcodes are logged and skipped, **not** an error — a request of entirely
unknown keys still returns success having done nothing.

Presses are queued in order, releases in **reverse** order (`:532-547`), so
chords work.

Reply `{"return": {}}`. Precondition: none. Delivery is deferred to
`process_pending_input_events` on the emulation thread, which means **keys sent
while the CPU is halted for GDB are still delivered**, because the drain is
duplicated onto the halted path (SKILL.md §1).

Recognised qcodes are the map at `qmp.cpp:74-141`: digits, `a`-`z`, `f1`-`f24`,
`shift`/`ctrl`/`alt` (+`_r`), `meta_l`/`meta_r`/`menu`, `esc`, `tab`,
`backspace`, `ret`, `spc`, the three lock keys, the punctuation set
(`grave_accent`, `minus`, `equal`, `backslash`, `bracket_left`,
`bracket_right`, `semicolon`, `apostrophe`, `comma`, `dot`, `slash`, `less`),
navigation, `kp_*`, `print`/`sysrq`/`pause`, and the Japanese keys
(`henkan`, `muhenkan`, `hiragana`, `yen`, `ro`). Anything else maps to
`KBD_NONE`.

### `input-send-event`

Arguments: `events` (array, required). Each event is
`{"type": ..., "data": {...}}` with three supported types:

| `type` | `data` | Effect |
| --- | --- | --- |
| `key` | `{"key": {"type": "qcode", "data": "<qcode>"}, "down": bool}` | press/release; `down` defaults true |
| `rel` | `{"axis": "x"\|"y", "value": int}` | relative mouse motion |
| `btn` | `{"button": "left"\|"right"\|"middle", "down": bool}` | mouse button |

Empty or absent `events` → `GenericError` "No events specified".

All `rel` events in one request are **accumulated into a single** mouse-move
event (`:636-643`). Unknown buttons and unknown qcodes are logged and skipped.

Reply `{"return": {}}`. Precondition: none. Deferred like `send-key`.

### `query-commands`

Arguments: none. Reply is a hand-maintained list of 13 names
(`handle_query_commands`, `:445`):

`qmp_capabilities`, `send-key`, `input-send-event`, `query-commands`,
`query-status`, `memdump`, `screendump`, `savestate`, `loadstate`, `stop`,
`cont`, `system_reset`, `debug-break-on-exec`.

**Mismatch:** `quit` and `system_powerdown` are dispatched but **not listed**
(see below). The list is not generated from the dispatch, so it drifts. Any new
command must be added in both places.

### `query-status`

Arguments: none. Reply (`handle_query_status`, `:1085`):

```json
{"return": {"status": "running"|"paused", "running": bool,
            "emulator-paused": bool,
            "debug": {"active": bool, "paused": bool, "reason": "gdb"|"breakpoint"}}}
```

- `status`/`running` are the **union**: paused if either the emulator or the
  debugger has the CPU stopped.
- `emulator-paused` is `EMULATOR_IsPaused()` alone (a QMP `stop`).
- `debug.active` is `DEBUG_IsDebuggerActive()` — true if the interactive
  debugger is up **or** a GDB client is merely connected, even while running.
- `debug.paused` is `DEBUG_IsCpuPausedForDebug()`.
- `reason` is omitted when nothing is paused for debug; otherwise `"gdb"` or
  `"breakpoint"` (`DEBUG_GetDebuggerPauseReason`, `debug.cpp:6372`).

This is the command to poll before `memdump` — it reports exactly the two
flags `memdump`'s guard tests.

### `memdump`

Arguments (inside `arguments`):
- `address` (int, required, ≥ 0) — **LINEAR**
- `size` (int, required, > 0, ≤ 16 MiB)
- `file` (string, optional)

Errors:
- missing/invalid `address` or `size` → `GenericError`
  "Missing or invalid 'address' and/or 'size' arguments"
- `size > 16*1024*1024` → `GenericError` "Size too large (max 16MB)"
- **`!DEBUG_IsCpuPausedForDebug() && !EMULATOR_IsPaused()`** → `GenericError`
  "memdump requires the CPU to be stopped for debugging; halt via GDB or QMP
  stop first" (`:721`)
- dump failure → "Failed to dump memory"; temp-file failures → their own
  messages

Reply:
- with `file`: `{"return": {"file": "<path>", "size": <requested size>}}`
- without: `{"return": {"data": "<base64>", "size": <requested size>}}`,
  written to a `mkstemp` temp file which is read back and `unlink`ed

Note `size` in the reply is the **requested** size, not the number of bytes
actually produced.

**Precondition: halted.** This is the only handler that reads guest state on
the socket thread, hence the guard. See SKILL.md §1.

### `screendump`

Arguments: `file` (string, optional).

Triggers `CAPTURE_TakeScreenshot()` and polls `CAPTURE_IsScreenshotPending()`
at 50 ms up to 5 s, then sleeps a further 50 ms for the path to appear
(`:787-806`).

Errors: "Screenshot capture timed out", "Screenshot capture failed - no file
created", "Failed to read screenshot file", "Failed to copy screenshot to
`<file>`".

Reply:
- without `file`: `{"return": {"data": "<base64 png>", "size": <bytes>, "format": "png", "file": "<internal path>"}}`
- with `file`: `{"return": {"file": "<path>", "size": <bytes>, "format": "png"}}`

**Precondition: none is enforced**, but a screenshot needs the emulation thread
to render a frame. While the CPU is halted for GDB the render path still runs,
so this generally works; while paused via QMP `stop` the behaviour depends on
`PauseDOSBoxLoop` still servicing capture. Not covered by an explicit guard —
if you need certainty here, add a conformance test rather than trusting this
paragraph.

### `savestate`

Arguments: `file` (string, **required**). Missing → `GenericError`
"Missing required 'file' argument".

`SAVESTATE_RequestSave(file)`, then polls `SAVESTATE_IsPending()` at 100 ms up
to **30 s**. Timeout → "Save state operation timed out". On completion,
`SAVESTATE_IsComplete` yields either an empty error (success) or the error
string, which is returned as `GenericError`.

Reply `{"return": {"file": "<path>"}}`.

**Precondition: none — and it must work while GDB-halted.** That is exactly
what the duplicated drain in `Normal_Loop` (SKILL.md §1) exists for. Before
that fix this command sat out its full 30 s and errored at a breakpoint.

### `loadstate`

Same shape as `savestate` plus an existence check: a missing file →
`GenericError` "State file not found: `<path>`" before any request is
published. Same 100 ms / 30 s poll, same reply.

### `stop`

Arguments: none. `EMULATOR_RequestPause()`, then polls `EMULATOR_IsPaused()` at
10 ms up to 1 s. Reply `{"return": {}}`; timeout → "Failed to pause emulator".

**Idempotent**: already paused returns success without republishing the
request.

### `cont`

Mirror of `stop` — `EMULATOR_RequestResume()`, polls for `!EMULATOR_IsPaused()`,
10 ms / 1 s, "Failed to resume emulator". Idempotent when already running.

Note `EMULATOR_RequestResume` (`sdlmain.cpp:1457`) does **not** go through the
`pending_control_request` atomic; it sets `unpause_now` and pushes a dummy SDL
event directly. It is the one control that does not use the drain.

### `system_reset`

Arguments: `dos_only` (bool, optional, default false).

**Precondition: refuses while GDB/debugger-halted** (`:1071`):
`GenericError` "system_reset refused: the CPU is halted for debugging;
continue or detach the debug client first".

`EMULATOR_IsPaused()` is deliberately *not* part of that condition — a plain
QMP `stop` has no debug client to leave stranded. The concern is session
coherency, not memory coherency: `system_reset` does not read guest state.

Otherwise `EMULATOR_RequestReset(dos_only)` and **immediate** success — the
reset happens later on the emulation thread, where
`EMULATOR_CheckPendingControl` throws `int(3)` (full reboot) or `int(6)`
(DOS kernel only). The client gets `{"return": {}}` before the reset occurs.

### `debug-break-on-exec`

Arguments: `enabled` (bool, optional, **default true**).

Calls `DEBUG_SetGDBBreakOnExec(enabled)` (`debug.cpp:6395`) and replies
`{"return": {"enabled": <bool>}}` echoing the value it set.

**This is the command that arms the unsolicited stop reply.** When the flag is
set and a program starts, `DEBUG_CheckExecuteBreakpoint` (`debug.cpp:5596-5601`)
both adds *and activates* a breakpoint at the entry point — unlike `Z0`, which
is inert until `c`. If the CPU was free-running, the resulting hit sends an
unsolicited `S05` to the GDB client. See `history.md`, "the protocol desync".

The flag clears itself when the breakpoint is hit (`debug.cpp:4863-4865` (inside `DEBUG_Enable_Handler`)), not
when it is armed.

### `quit` / `system_powerdown`

Arguments: none. Reply `{"return": {}}`.

**They do nothing.** The dispatch acknowledges and explicitly does not quit
(`:430-432`). **Both are dispatched but absent from `query-commands`.** A
client that trusts `query-commands` will not know they exist; a client that
sends them will believe it shut the emulator down.

---

## Unknown commands

- `execute` present but unrecognised → `{"error": {"class": "CommandNotFound",
  "desc": "Command not found: <name>"}}`
- `execute` absent or unparseable → `{"error": {"class": "GenericError",
  "desc": "Invalid command format"}}`

Because `extract_string` scans for the first `"execute"` anywhere in the
buffer, a payload containing that literal inside a *value* can be misdispatched.

## Advertised vs dispatched — summary

| Command | Dispatched | In `query-commands` |
| --- | --- | --- |
| the 13 listed names | yes | yes |
| `quit` | yes (no-op) | **no** |
| `system_powerdown` | yes (no-op) | **no** |
| `exec-oob` / OOB execution | **no** | n/a — but `oob` is in the greeting's `capabilities` |

Three mismatches, all in the same direction: the server claims less than it
dispatches for commands, and more than it implements for capabilities.
