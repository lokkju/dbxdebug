# dbxdebug

Client library and CLI for DOSBox-X remote debug protocols.

## Features

- **GDB Client**: Remote debugging via GDB Remote Serial Protocol
  - Memory read/write
  - Register inspection
  - Breakpoint management
  - Execution control (step, continue)

- **QMP Client**: Keyboard input via QEMU Monitor Protocol
  - Key press/release with timing control
  - Text typing with shift handling
  - Key combinations (Ctrl+C, Alt+F4, etc.)

- **Video Tools**: DOS text mode screen capture
  - Screen dumps (text and raw)
  - HTML output with colors
  - BIOS timer correlation

## Installation

```bash
uv sync
```

## CLI Usage

```bash
# GDB debugging
dbxdebug gdb read-mem 0xb800:0000 4000 --hex
dbxdebug gdb registers
dbxdebug gdb break 0x1000

# Keyboard input
dbxdebug qmp key a
dbxdebug qmp key ctrl c
dbxdebug qmp type "Hello World"

# Screen capture
dbxdebug screen dump
dbxdebug screen html -o output.html
dbxdebug screen watch
```

## Library Usage

```python
from dbxdebug import GDBClient, QMPClient, DOSVideoTools

# Debugging
with GDBClient() as gdb:
    regs = gdb.read_registers()
    mem = gdb.read_memory("0xb800:0000", 4000)

# Keyboard input
with QMPClient() as qmp:
    qmp.send_key(["ctrl", "c"])
    qmp.type_text("Hello")

# Screen capture
with DOSVideoTools() as video:
    lines = video.screen_dump()
```

## Ports

- GDB server: 2159 (default)
- QMP server: 4444 (default)

Enable in DOSBox-X config:
```ini
[dosbox]
gdbserver=true
qmpserver=true
```
