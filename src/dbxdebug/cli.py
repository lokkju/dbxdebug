"""
Command-line interface for DOSBox-X remote debugging.

Usage:
    dbxdebug gdb <command>     - GDB debugging operations
    dbxdebug qmp <command>     - QMP keyboard operations
    dbxdebug screen <command>  - Screen capture operations
"""

import sys

import click
from loguru import logger

from .gdb import GDBClient
from .qmp import QMPClient, QMPError
from .video import DOSVideoTools
from .utils import hexdump, parse_x86_address
from .html import dos_video_to_html


# Configure loguru
logger.remove()
logger.add(sys.stderr, level="WARNING")


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose output")
@click.option("--debug", is_flag=True, help="Enable debug logging")
def main(verbose: bool, debug: bool):
    """DOSBox-X remote debug client."""
    if debug:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG")
    elif verbose:
        logger.remove()
        logger.add(sys.stderr, level="INFO")


# =============================================================================
# GDB Commands
# =============================================================================


@main.group()
@click.option("--host", default="localhost", help="GDB server host")
@click.option("--port", default=2159, help="GDB server port")
@click.pass_context
def gdb(ctx, host: str, port: int):
    """GDB debugging commands."""
    ctx.ensure_object(dict)
    ctx.obj["gdb_host"] = host
    ctx.obj["gdb_port"] = port


@gdb.command("read-mem")
@click.argument("address")
@click.argument("length", type=int)
@click.option("--hex", "output_hex", is_flag=True, help="Output as hex dump")
@click.pass_context
def gdb_read_mem(ctx, address: str, length: int, output_hex: bool):
    """Read memory from target."""
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb_client:
            data = gdb_client.read_memory(address, length)
            if output_hex:
                linear = parse_x86_address(address)
                for line in hexdump(data, start_addr=linear):
                    click.echo(line)
            else:
                sys.stdout.buffer.write(data)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@gdb.command("write-mem")
@click.argument("address")
@click.argument("data")
@click.pass_context
def gdb_write_mem(ctx, address: str, data: str):
    """Write memory to target. DATA is hex string (e.g., 'deadbeef')."""
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb_client:
            data_bytes = bytes.fromhex(data)
            gdb_client.write_memory(address, data_bytes)
            click.echo(f"Wrote {len(data_bytes)} bytes to {address}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@gdb.command("registers")
@click.pass_context
def gdb_registers(ctx):
    """Read CPU registers."""
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb_client:
            regs = gdb_client.read_registers()
            # Format output
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


@gdb.command("break")
@click.argument("address")
@click.pass_context
def gdb_break(ctx, address: str):
    """Set breakpoint at address."""
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb_client:
            if gdb_client.set_breakpoint(address):
                click.echo(f"Breakpoint set at {address}")
            else:
                click.echo(f"Failed to set breakpoint at {address}", err=True)
                sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@gdb.command("step")
@click.pass_context
def gdb_step(ctx):
    """Single-step one instruction."""
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb_client:
            response = gdb_client.step()
            click.echo(f"Stop reason: {response.decode()}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@gdb.command("continue")
@click.pass_context
def gdb_continue(ctx):
    """Continue execution."""
    try:
        with GDBClient(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as gdb_client:
            click.echo("Continuing... (Ctrl+C to interrupt)")
            response = gdb_client.continue_execution()
            click.echo(f"Stop reason: {response.decode()}")
    except KeyboardInterrupt:
        click.echo("\nInterrupted")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# QMP Commands
# =============================================================================


@main.group()
@click.option("--host", default="localhost", help="QMP server host")
@click.option("--port", default=4444, help="QMP server port")
@click.pass_context
def qmp(ctx, host: str, port: int):
    """QMP keyboard commands."""
    ctx.ensure_object(dict)
    ctx.obj["qmp_host"] = host
    ctx.obj["qmp_port"] = port


@qmp.command("key")
@click.argument("keys", nargs=-1, required=True)
@click.option("--hold", default=100, help="Hold time in ms")
@click.pass_context
def qmp_key(ctx, keys: tuple[str, ...], hold: int):
    """
    Send key press(es).

    KEYS are QMP qcode names like 'a', 'ctrl', 'ret', 'f1'.
    Multiple keys are pressed simultaneously (chord).

    Examples:
        dbxdebug qmp key a
        dbxdebug qmp key ctrl c
        dbxdebug qmp key ctrl alt delete
    """
    try:
        with QMPClient(ctx.obj["qmp_host"], ctx.obj["qmp_port"]) as qmp_client:
            qmp_client.send_key(list(keys), hold)
            click.echo(f"Sent: {' + '.join(keys)}")
    except QMPError as e:
        click.echo(f"QMP Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@qmp.command("type")
@click.argument("text")
@click.option("--delay", default=0.05, help="Delay between keys in seconds")
@click.pass_context
def qmp_type(ctx, text: str, delay: float):
    """
    Type text string.

    Handles shift for uppercase and special characters.

    Example:
        dbxdebug qmp type "Hello World!"
    """
    try:
        with QMPClient(ctx.obj["qmp_host"], ctx.obj["qmp_port"]) as qmp_client:
            qmp_client.type_text(text, delay)
            click.echo(f"Typed: {text}")
    except QMPError as e:
        click.echo(f"QMP Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@qmp.command("commands")
@click.pass_context
def qmp_commands(ctx):
    """List available QMP commands."""
    try:
        with QMPClient(ctx.obj["qmp_host"], ctx.obj["qmp_port"]) as qmp_client:
            commands = qmp_client.query_commands()
            for cmd in sorted(commands):
                click.echo(cmd)
    except QMPError as e:
        click.echo(f"QMP Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# Screen Commands
# =============================================================================


@main.group()
@click.option("--host", default="localhost", help="GDB server host")
@click.option("--port", default=2159, help="GDB server port")
@click.pass_context
def screen(ctx, host: str, port: int):
    """Screen capture commands."""
    ctx.ensure_object(dict)
    ctx.obj["gdb_host"] = host
    ctx.obj["gdb_port"] = port


@screen.command("dump")
@click.option("--raw", is_flag=True, help="Output raw video memory (with attributes)")
@click.pass_context
def screen_dump(ctx, raw: bool):
    """Dump current screen contents."""
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
    """Save screen as HTML with colors."""
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


@screen.command("watch")
@click.option("--interval", default=0.5, help="Update interval in seconds")
@click.pass_context
def screen_watch(ctx, interval: float):
    """Watch screen in real-time (Ctrl+C to stop)."""
    import time

    try:
        with DOSVideoTools(ctx.obj["gdb_host"], ctx.obj["gdb_port"]) as video:
            click.echo("Watching screen... (Ctrl+C to stop)\n")
            while True:
                # Clear screen and move cursor to top
                click.echo("\033[2J\033[H", nl=False)

                lines, ticks = video.screen_dump_with_ticks()
                if lines:
                    for line in lines:
                        click.echo(line)
                    if ticks is not None:
                        click.echo(f"\nTicks: {ticks}")
                else:
                    click.echo("Failed to read screen")

                time.sleep(interval)
    except KeyboardInterrupt:
        click.echo("\nStopped")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
