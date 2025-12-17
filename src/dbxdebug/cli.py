"""
Command-line interface for DOSBox-X remote debugging.

Commands:
    dbxdebug mem <command>     - Memory operations (via GDB)
    dbxdebug cpu <command>     - CPU state and execution control (via GDB)
    dbxdebug key <command>     - Keyboard input (via QMP)
    dbxdebug screen <command>  - Screen capture and recording
"""

import sys
import time

import click
from loguru import logger

from .gdb import GDBClient
from .qmp import QMPClient, QMPError
from .video import DOSVideoTools
from .utils import hexdump, parse_x86_address
from .html import dos_video_to_html, analyze_dos_video_colors
from .capture_io import ScreenRecorder, load_capture


# Configure loguru
logger.remove()
logger.add(sys.stderr, level="WARNING")


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose output")
@click.option("--debug", is_flag=True, help="Enable debug logging")
@click.version_option(version="0.1.0")
def main(verbose: bool, debug: bool):
    """DOSBox-X remote debug client.

    Connects to DOSBox-X GDB server (port 2159) for debugging and memory access,
    and QMP server (port 4444) for keyboard input.
    """
    if debug:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG")
    elif verbose:
        logger.remove()
        logger.add(sys.stderr, level="INFO")


# =============================================================================
# Memory Commands (GDB)
# =============================================================================


@main.group()
@click.option("--host", default="localhost", help="GDB server hostname")
@click.option("--port", default=2159, help="GDB server port (default: 2159)")
@click.pass_context
def mem(ctx, host: str, port: int):
    """Memory read/write operations via GDB protocol."""
    ctx.ensure_object(dict)
    ctx.obj["gdb_host"] = host
    ctx.obj["gdb_port"] = port


@mem.command("read")
@click.argument("address", metavar="ADDRESS")
@click.argument("length", type=int, metavar="LENGTH")
@click.option("--hex", "output_hex", is_flag=True, help="Format output as hex dump")
@click.pass_context
def mem_read(ctx, address: str, length: int, output_hex: bool):
    """Read LENGTH bytes from ADDRESS.

    ADDRESS can be segment:offset (e.g., b800:0000) or linear (e.g., 0xb8000).

    Examples:
        dbxdebug mem read b800:0000 4000 --hex
        dbxdebug mem read 0x1000 256
    """
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb:
            data = gdb.read_memory(address, length)
            if output_hex:
                linear = parse_x86_address(address)
                for line in hexdump(data, start_addr=linear):
                    click.echo(line)
            else:
                sys.stdout.buffer.write(data)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@mem.command("write")
@click.argument("address", metavar="ADDRESS")
@click.argument("hexdata", metavar="HEXDATA")
@click.pass_context
def mem_write(ctx, address: str, hexdata: str):
    """Write HEXDATA bytes to ADDRESS.

    HEXDATA is a hex string without spaces (e.g., 'deadbeef').

    Examples:
        dbxdebug mem write 0x1000 90909090
        dbxdebug mem write b800:0000 4142
    """
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb:
            data = bytes.fromhex(hexdata)
            gdb.write_memory(address, data)
            click.echo(f"Wrote {len(data)} bytes to {address}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# CPU Commands (GDB)
# =============================================================================


@main.group()
@click.option("--host", default="localhost", help="GDB server hostname")
@click.option("--port", default=2159, help="GDB server port (default: 2159)")
@click.pass_context
def cpu(ctx, host: str, port: int):
    """CPU registers and execution control via GDB protocol."""
    ctx.ensure_object(dict)
    ctx.obj["gdb_host"] = host
    ctx.obj["gdb_port"] = port


@cpu.command("regs")
@click.pass_context
def cpu_regs(ctx):
    """Display all CPU registers."""
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb:
            regs = gdb.read_registers()
            click.echo("General Purpose:")
            click.echo(f"  EAX={regs['eax']:08X}  ECX={regs['ecx']:08X}  EDX={regs['edx']:08X}  EBX={regs['ebx']:08X}")
            click.echo(f"  ESP={regs['esp']:08X}  EBP={regs['ebp']:08X}  ESI={regs['esi']:08X}  EDI={regs['edi']:08X}")
            click.echo()
            click.echo("Instruction Pointer:")
            click.echo(f"  EIP={regs['eip']:08X}  EFLAGS={regs['eflags']:08X}")
            click.echo()
            click.echo("Segment Registers:")
            click.echo(f"  CS={regs['cs']:04X}  SS={regs['ss']:04X}  DS={regs['ds']:04X}  ES={regs['es']:04X}  FS={regs['fs']:04X}  GS={regs['gs']:04X}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cpu.command("break")
@click.argument("address", metavar="ADDRESS")
@click.pass_context
def cpu_break(ctx, address: str):
    """Set software breakpoint at ADDRESS.

    Example:
        dbxdebug cpu break 0x1000
    """
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb:
            if gdb.set_breakpoint(address):
                linear = parse_x86_address(address)
                click.echo(f"Breakpoint set at 0x{linear:X}")
            else:
                click.echo(f"Failed to set breakpoint at {address}", err=True)
                sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cpu.command("delete")
@click.argument("address", metavar="ADDRESS")
@click.pass_context
def cpu_delete(ctx, address: str):
    """Remove breakpoint at ADDRESS.

    Example:
        dbxdebug cpu delete 0x1000
    """
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb:
            if gdb.remove_breakpoint(address):
                linear = parse_x86_address(address)
                click.echo(f"Breakpoint removed at 0x{linear:X}")
            else:
                click.echo(f"No breakpoint at {address}", err=True)
                sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cpu.command("step")
@click.pass_context
def cpu_step(ctx):
    """Execute single instruction and stop."""
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb:
            response = gdb.step()
            click.echo(f"Stopped: {response.decode()}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cpu.command("cont")
@click.pass_context
def cpu_cont(ctx):
    """Continue execution until breakpoint or Ctrl+C."""
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb:
            click.echo("Continuing... (Ctrl+C to interrupt)")
            response = gdb.continue_execution()
            click.echo(f"Stopped: {response.decode()}")
    except KeyboardInterrupt:
        click.echo("\nInterrupted")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cpu.command("halt")
@click.pass_context
def cpu_halt(ctx):
    """Break into debugger (stop execution)."""
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb:
            response = gdb.halt()
            click.echo(f"Halted: {response.decode()}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# Keyboard Commands (QMP)
# =============================================================================


@main.group()
@click.option("--host", default="localhost", help="QMP server hostname")
@click.option("--port", default=4444, help="QMP server port (default: 4444)")
@click.pass_context
def key(ctx, host: str, port: int):
    """Keyboard input via QMP protocol."""
    ctx.ensure_object(dict)
    ctx.obj["qmp_host"] = host
    ctx.obj["qmp_port"] = port


@key.command("send")
@click.argument("keys", nargs=-1, required=True, metavar="KEY...")
@click.option("--hold", default=100, help="Hold time in milliseconds (default: 100)")
@click.pass_context
def key_send(ctx, keys: tuple[str, ...], hold: int):
    """Send key chord (simultaneous key presses).

    KEYS are QMP qcode names. Multiple keys are pressed together.

    Common qcodes: a-z, 0-9, f1-f12, ctrl, shift, alt, ret (Enter),
    spc (Space), esc, tab, backspace, delete, insert, home, end,
    pgup, pgdn, left, right, up, down.

    Examples:
        dbxdebug key send a
        dbxdebug key send ctrl c
        dbxdebug key send ctrl alt delete
    """
    try:
        with QMPClient(ctx.obj["qmp_host"], ctx.obj["qmp_port"]) as qmp:
            qmp.send_key(list(keys), hold)
            click.echo(f"Sent: {' + '.join(keys)}")
    except QMPError as e:
        click.echo(f"QMP error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@key.command("type")
@click.argument("text", metavar="TEXT")
@click.option("--delay", default=0.05, help="Delay between keys in seconds (default: 0.05)")
@click.pass_context
def key_type(ctx, text: str, delay: float):
    """Type a text string character by character.

    Automatically handles shift for uppercase letters and symbols.

    Example:
        dbxdebug key type "Hello World!"
    """
    try:
        with QMPClient(ctx.obj["qmp_host"], ctx.obj["qmp_port"]) as qmp:
            qmp.type_text(text, delay)
            click.echo(f"Typed: {text}")
    except QMPError as e:
        click.echo(f"QMP error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@key.command("down")
@click.argument("keycode", metavar="KEY")
@click.pass_context
def key_down(ctx, keycode: str):
    """Press and hold a key (no release).

    Use 'key up' to release. Useful for modifier keys.

    Example:
        dbxdebug key down shift
        dbxdebug key send a
        dbxdebug key up shift
    """
    try:
        with QMPClient(ctx.obj["qmp_host"], ctx.obj["qmp_port"]) as qmp:
            qmp.key_down(keycode)
            click.echo(f"Key down: {keycode}")
    except QMPError as e:
        click.echo(f"QMP error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@key.command("up")
@click.argument("keycode", metavar="KEY")
@click.pass_context
def key_up(ctx, keycode: str):
    """Release a held key.

    Example:
        dbxdebug key up shift
    """
    try:
        with QMPClient(ctx.obj["qmp_host"], ctx.obj["qmp_port"]) as qmp:
            qmp.key_up(keycode)
            click.echo(f"Key up: {keycode}")
    except QMPError as e:
        click.echo(f"QMP error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@key.command("list")
@click.pass_context
def key_list_cmd(ctx):
    """List available QMP commands on server."""
    try:
        with QMPClient(ctx.obj["qmp_host"], ctx.obj["qmp_port"]) as qmp:
            commands = qmp.query_commands()
            click.echo("Available QMP commands:")
            for cmd in sorted(commands):
                click.echo(f"  {cmd}")
    except QMPError as e:
        click.echo(f"QMP error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# Screen Commands (GDB memory access)
# =============================================================================


@main.group()
@click.option("--host", default="localhost", help="GDB server hostname")
@click.option("--port", default=2159, help="GDB server port (default: 2159)")
@click.pass_context
def screen(ctx, host: str, port: int):
    """Screen capture and video memory access."""
    ctx.ensure_object(dict)
    ctx.obj["gdb_host"] = host
    ctx.obj["gdb_port"] = port


@screen.command("dump")
@click.option("--raw", is_flag=True, help="Output raw bytes (char+attr pairs)")
@click.pass_context
def screen_dump(ctx, raw: bool):
    """Dump current DOS text screen (80x25).

    By default outputs text only. Use --raw for video memory with attributes.
    """
    try:
        with DOSVideoTools(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as video:
            if raw:
                data = video.screen_raw()
                if data:
                    sys.stdout.buffer.write(data)
                else:
                    click.echo("Failed to read screen", err=True)
                    sys.exit(1)
            else:
                lines = video.screen_dump()
                if lines:
                    for line in lines:
                        click.echo(line)
                else:
                    click.echo("Failed to read screen", err=True)
                    sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@screen.command("html")
@click.option("-o", "--output", default="screen.html", help="Output filename")
@click.pass_context
def screen_html(ctx, output: str):
    """Export screen as HTML with VGA colors.

    Creates an HTML file with the DOS screen rendered using VGA color palette.
    """
    try:
        with DOSVideoTools(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as video:
            data = video.screen_raw()
            if data:
                html = dos_video_to_html(data)
                with open(output, "w", encoding="utf-8") as f:
                    f.write(html)
                click.echo(f"Saved to {output}")
            else:
                click.echo("Failed to read screen", err=True)
                sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@screen.command("info")
@click.pass_context
def screen_info(ctx):
    """Show video mode and BIOS timer info."""
    try:
        with DOSVideoTools(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as video:
            mode = video.read_video_mode()
            ticks = video.read_timer_ticks()
            click.echo(f"Video mode: {mode} (0x{mode:02X})" if mode else "Video mode: unknown")
            click.echo(f"BIOS ticks: {ticks}" if ticks else "BIOS ticks: unknown")
            if ticks:
                seconds = ticks / 18.2065
                click.echo(f"Uptime: {seconds:.1f}s ({seconds/60:.1f}m)")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@screen.command("watch")
@click.option("-i", "--interval", default=0.5, help="Update interval in seconds")
@click.pass_context
def screen_watch(ctx, interval: float):
    """Watch screen in real-time. Press Ctrl+C to stop."""
    try:
        with DOSVideoTools(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as video:
            click.echo("Watching screen... (Ctrl+C to stop)\n")
            while True:
                click.echo("\033[2J\033[H", nl=False)  # Clear and home
                lines, ticks = video.screen_dump_with_ticks()
                if lines:
                    for line in lines:
                        click.echo(line)
                    if ticks is not None:
                        click.echo(f"\n[Ticks: {ticks}]")
                else:
                    click.echo("Failed to read screen")
                time.sleep(interval)
    except KeyboardInterrupt:
        click.echo("\nStopped")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@screen.command("record")
@click.option("-o", "--output", default="capture.capture.gz", help="Output filename")
@click.option("-d", "--duration", default=10.0, help="Duration in seconds")
@click.option("-r", "--rate", default=50.0, help="Sample rate in Hz")
@click.option("--raw", is_flag=True, help="Capture raw bytes (with attributes)")
@click.pass_context
def screen_record(ctx, output: str, duration: float, rate: float, raw: bool):
    """Record screen captures to a .capture.gz file.

    Creates a timestamped capture file for later analysis.

    Example:
        dbxdebug screen record -d 60 -r 30 -o session.capture.gz
    """
    try:
        with DOSVideoTools(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as video:
            recorder = ScreenRecorder({"duration": duration, "sample_rate": rate})
            total_samples = int(duration * rate)

            click.echo(f"Recording {duration}s at {rate}Hz ({total_samples} frames)...")

            with click.progressbar(length=total_samples, label="Recording") as bar:
                sample_interval = 1.0 / rate
                start_time = time.time()
                capture_fn = recorder.capture_raw if raw else recorder.capture

                for i in range(total_samples):
                    capture_fn(video)
                    bar.update(1)

                    elapsed = time.time() - start_time
                    next_sample = (i + 1) * sample_interval
                    if (sleep_time := next_sample - elapsed) > 0:
                        time.sleep(sleep_time)

            recorder.save(output)
            click.echo(f"Saved {len(recorder)} frames to {output}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@screen.command("colors")
@click.argument("capture_file", type=click.Path(exists=True), required=False)
@click.pass_context
def screen_colors(ctx, capture_file: str | None):
    """Analyze colors in current screen or capture file.

    Without argument, analyzes live screen. With file, analyzes capture.
    """
    try:
        if capture_file:
            data = load_capture(capture_file)
            screens = data.get("screens", data)
            # Get first raw screen from capture
            if isinstance(screens, dict):
                first_key = next(iter(screens))
                video_data = screens[first_key]
                if isinstance(video_data, list):
                    click.echo("Capture contains text-only screens (no color data)")
                    return
                pages = [video_data]
            else:
                pages = [screens]
        else:
            with DOSVideoTools(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as video:
                raw = video.screen_raw()
                if not raw:
                    click.echo("Failed to read screen", err=True)
                    sys.exit(1)
                pages = [raw]

        analysis = analyze_dos_video_colors(pages)
        summary = analysis["summary"]

        click.echo(f"Cells analyzed: {summary['total_cells']}")
        click.echo(f"Content cells: {summary['content_cells']}")
        click.echo(f"Foreground colors: {summary['unique_fg_colors']}")
        click.echo(f"Background colors: {summary['unique_bg_colors']}")
        click.echo(f"Color combinations: {summary['unique_combinations']}")
        click.echo(f"Blink used: {'Yes' if summary['blink_used'] else 'No'}")

        click.echo("\nForeground colors:")
        for color in analysis["foreground_colors"]:
            click.echo(f"  {color['id']:2d} {color['name']:<14} {color['count']:5d} ({color['percentage']:5.1f}%)")

        click.echo("\nBackground colors:")
        for color in analysis["background_colors"]:
            click.echo(f"  {color['id']:2d} {color['name']:<14} {color['count']:5d} ({color['percentage']:5.1f}%)")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# Backwards compatibility aliases
# =============================================================================

# Keep old 'gdb' and 'qmp' as aliases
main.add_command(mem, name="gdb")  # gdb is alias for mem
main.add_command(key, name="qmp")  # qmp is alias for key


if __name__ == "__main__":
    main()
