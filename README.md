# dbxdebug

[![CI](https://github.com/lokkju/dbxdebug/actions/workflows/test.yml/badge.svg)](https://github.com/lokkju/dbxdebug/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/dbxdebug)](https://pypi.org/project/dbxdebug/)
[![Python](https://img.shields.io/pypi/pyversions/dbxdebug)](https://pypi.org/project/dbxdebug/)
[![License](https://img.shields.io/github/license/lokkju/dbxdebug)](https://github.com/lokkju/dbxdebug/blob/main/LICENSE)

Client library and CLI for DOSBox-X remote debug protocols.

## Features

- **GDB Client**: Remote debugging via GDB Remote Serial Protocol
  - Memory read/write
  - Register inspection
  - Breakpoint management
  - Execution control (step, continue, halt)

- **QMP Client**: Keyboard input via QEMU Monitor Protocol
  - Key press/release with timing control
  - Text typing with shift handling
  - Key combinations (Ctrl+C, Alt+F4, etc.)

- **Video Tools**: DOS text mode screen capture
  - Screen capture (text, raw, HTML)
  - Timed multi-frame recording
  - Color analysis
  - BIOS timer correlation

## Installation

```bash
uv sync
```

## CLI Usage

```
dbxdebug
├── mem              # Memory operations (alias: gdb)
│   ├── read         # Read memory
│   └── write        # Write hex bytes
│
├── cpu              # CPU and execution control
│   ├── regs         # Display registers
│   ├── break        # Set breakpoint
│   ├── delete       # Remove breakpoint
│   ├── step         # Single step
│   ├── cont         # Continue execution
│   └── halt         # Stop execution
│
├── key              # Keyboard input (alias: qmp)
│   ├── send         # Key chord (e.g., ctrl c)
│   ├── type         # Type text string
│   ├── down         # Press and hold key
│   ├── up           # Release key
│   └── list         # List QMP commands
│
└── screen           # Screen capture
    ├── show         # Display text to stdout
    ├── capture      # Save frame to file (-f raw|html|text)
    ├── record       # Multi-frame timed capture
    ├── watch        # Real-time display
    ├── info         # Video mode, BIOS ticks
    └── colors       # Analyze color palette
```

### Examples

```bash
# Memory
dbxdebug mem read b800:0000 4000 --hex
dbxdebug mem write 0x1000 90909090

# CPU / Debugging
dbxdebug cpu regs
dbxdebug cpu break 0x1000
dbxdebug cpu step
dbxdebug cpu cont

# Keyboard
dbxdebug key send a
dbxdebug key send ctrl c
dbxdebug key send ctrl alt delete
dbxdebug key type "Hello World!"

# Screen
dbxdebug screen show
dbxdebug screen capture -f html -o snapshot
dbxdebug screen record -d 60 -r 30 -o session.capture.gz
dbxdebug screen watch
dbxdebug screen colors
```

## Library Usage

```python
from dbxdebug import (
    GDBClient, QMPClient, DOSVideoTools,
    ScreenRecorder, load_capture,
    ctrl_key, CTRL_C, DBX_KEY,
)

# Memory and debugging
with GDBClient() as gdb:
    regs = gdb.read_registers()
    mem = gdb.read_memory("b800:0000", 4000)
    gdb.write_memory(0x1000, b"\x90\x90")
    gdb.set_breakpoint(0x1000)
    gdb.step()
    gdb.continue_execution()

# Keyboard input
with QMPClient() as qmp:
    qmp.send_key(["ctrl", "c"])       # Key chord
    qmp.send_key(CTRL_C)              # Using constant
    qmp.type_text("Hello World!")     # Type string
    qmp.key_down("shift")             # Hold key
    qmp.key_up("shift")               # Release key

# Screen capture
with DOSVideoTools() as video:
    lines = video.screen_dump()           # Text lines
    raw = video.screen_raw()              # Raw bytes with attrs
    lines, ticks = video.screen_dump_with_ticks()

# Timed recording
with DOSVideoTools() as video:
    recorder = ScreenRecorder()
    recorder.record(video, duration=10.0, sample_rate=50)
    recorder.save("session.capture.gz")

# Load and analyze capture
data = load_capture("session.capture.gz")
```

## Ports

| Protocol | Default Port | Purpose |
|----------|--------------|---------|
| GDB      | 2159         | Debugging, memory access |
| QMP      | 4444         | Keyboard input |

Enable in DOSBox-X config:
```ini
[dosbox]
gdbserver=true
qmpserver=true
```
