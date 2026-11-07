"""Click-based CLI for WizLight."""

import asyncio
import gc

import click

from ..core.async_runtime import BackgroundAsyncLoop, configure_event_loop_policy
from ..core.bulb_controller import BulbController, apply_preset, PRESETS
from ..core.config import Config


def run_async(ctx, coro):
    """Run async work on the command-scoped event loop."""
    return ctx.obj["runner"].run(coro)


def _cleanup_runtime(controller: BulbController, runner: BackgroundAsyncLoop) -> None:
    """Close bulb transports before stopping the CLI event loop."""
    try:
        runner.cancel_pending(timeout=2.0)
        runner.run(controller.close_async(), timeout=2.0)
    except Exception:
        controller.close()
    finally:
        gc.collect()
        runner.shutdown()


def get_bulb_ips(config: Config, ip: str = None) -> list[str]:
    """Get list of bulb IPs - either specified or all configured."""
    if ip:
        return [ip]
    return [b.ip for b in config.bulbs]


@click.group()
@click.pass_context
def cli(ctx):
    """WizLight - Control your WiZ smart bulbs."""
    configure_event_loop_policy()
    ctx.ensure_object(dict)
    ctx.obj["config"] = Config.load()
    ctx.obj["controller"] = BulbController()
    ctx.obj["runner"] = BackgroundAsyncLoop()
    ctx.call_on_close(lambda: _cleanup_runtime(ctx.obj["controller"], ctx.obj["runner"]))


@cli.command()
@click.option("--broadcast", "-b", default="192.168.1.255", help="Broadcast address for discovery")
@click.option("--save/--no-save", default=True, help="Save discovered bulbs to config")
@click.pass_context
def discover(ctx, broadcast, save):
    """Discover WiZ bulbs on the local network."""
    controller = ctx.obj["controller"]
    config = ctx.obj["config"]
    
    click.echo(f"Discovering bulbs on {broadcast}...")
    bulbs = run_async(ctx, controller.discover(broadcast))
    
    if not bulbs:
        click.echo("No bulbs found. Check your network and broadcast address.")
        return
    
    click.echo(f"Found {len(bulbs)} bulb(s):")
    for i, bulb in enumerate(bulbs, 1):
        click.echo(f"  {i}. IP: {bulb['ip']}, MAC: {bulb['mac']}")
        if save:
            config.add_bulb(bulb['ip'], f"Bulb {i}", bulb['mac'], preserve_name=True)
    
    if save:
        click.echo(f"Saved to config: {config._config_path}")


@cli.command("add-bulb")
@click.argument("ip")
@click.option("--name", "-n", default=None, help="Name for the bulb")
@click.pass_context
def add_bulb(ctx, ip, name):
    """Manually add a bulb by IP address."""
    config = ctx.obj["config"]
    controller = ctx.obj["controller"]
    
    # Test connection
    click.echo(f"Testing connection to {ip}...")
    try:
        state = run_async(ctx, controller.get_state(ip))
        status_str = "ON" if state.is_on else "OFF"
        click.echo(f"  [ok] Bulb responding. Status: {status_str}")
        
        bulb_name = name or f"Bulb {len(config.bulbs) + 1}"
        config.add_bulb(ip, bulb_name)
        click.echo(f"  [ok] Added '{bulb_name}' ({ip}) to config")
    except Exception as e:
        click.echo(f"  [error] Could not connect to bulb: {e}")
        click.echo("    Make sure the bulb is on and connected to WiFi")


@cli.command("remove-bulb")
@click.argument("ip")
@click.pass_context
def remove_bulb(ctx, ip):
    """Remove a bulb from config."""
    config = ctx.obj["config"]
    
    if config.remove_bulb(ip):
        click.echo(f"Removed bulb {ip} from config")
    else:
        click.echo(f"Bulb {ip} not found in config")


@cli.command("prune-bulbs")
@click.pass_context
def prune_bulbs(ctx):
    """Remove stale or duplicate bulbs from config."""
    config = ctx.obj["config"]
    controller = ctx.obj["controller"]

    if not config.bulbs:
        click.echo("No bulbs configured.")
        return

    stale_ips = run_async(ctx, controller.find_stale_bulbs([bulb.ip for bulb in config.bulbs]))
    if not stale_ips:
        click.echo("No stale bulbs found.")
        return

    removed = config.remove_bulbs(stale_ips)
    click.echo(f"Removed {removed} stale bulb(s): {', '.join(stale_ips)}")


@cli.command("list-bulbs")
@click.pass_context
def list_bulbs(ctx):
    """List all configured bulbs."""
    config = ctx.obj["config"]
    
    if not config.bulbs:
        click.echo("No bulbs configured.")
        return
    
    click.echo("Configured bulbs:")
    for bulb in config.bulbs:
        click.echo(f"  - {bulb.name} - {bulb.ip}")


@cli.command()
@click.pass_context
def status(ctx):
    """Show status of all configured bulbs."""
    controller = ctx.obj["controller"]
    config = ctx.obj["config"]
    
    if not config.bulbs:
        click.echo("No bulbs configured. Run 'wizlight discover' first.")
        return

    async def get_states():
        return await asyncio.gather(
            *(controller.get_state(bulb.ip) for bulb in config.bulbs),
            return_exceptions=True,
        )

    for bulb_config, result in zip(config.bulbs, run_async(ctx, get_states())):
        if isinstance(result, Exception):
            click.echo(f"{bulb_config.name} ({bulb_config.ip}): ERROR - {result}")
            continue

        status_str = "ON" if result.is_on else "OFF"
        extras = []
        if result.brightness is not None:
            extras.append(f"brightness={result.brightness}")
        if result.rgb is not None:
            extras.append(f"rgb={result.rgb}")
        if result.color_temp is not None:
            extras.append(f"temp={result.color_temp}K")

        extra_str = f" ({', '.join(extras)})" if extras else ""
        click.echo(f"{bulb_config.name} ({bulb_config.ip}): {status_str}{extra_str}")


@cli.command()
@click.option("--ip", "-i", help="Specific bulb IP (default: all bulbs)")
@click.option("--brightness", "-b", type=int, help="Brightness level (0-255)")
@click.pass_context
def on(ctx, ip, brightness):
    """Turn on bulb(s)."""
    controller = ctx.obj["controller"]
    config = ctx.obj["config"]
    ips = get_bulb_ips(config, ip)
    
    if not ips:
        click.echo("No bulbs configured.")
        return
    
    run_async(ctx, controller.turn_on_all(ips, brightness))
    click.echo(f"Turned on {len(ips)} bulb(s)")


@cli.command()
@click.option("--ip", "-i", help="Specific bulb IP (default: all bulbs)")
@click.pass_context
def off(ctx, ip):
    """Turn off bulb(s)."""
    controller = ctx.obj["controller"]
    config = ctx.obj["config"]
    ips = get_bulb_ips(config, ip)
    
    if not ips:
        click.echo("No bulbs configured.")
        return
    
    run_async(ctx, controller.turn_off_all(ips))
    click.echo(f"Turned off {len(ips)} bulb(s)")


@cli.command()
@click.option("--ip", "-i", help="Specific bulb IP (default: all bulbs)")
@click.pass_context
def toggle(ctx, ip):
    """Toggle bulb(s) on/off."""
    controller = ctx.obj["controller"]
    config = ctx.obj["config"]
    ips = get_bulb_ips(config, ip)
    
    if not ips:
        click.echo("No bulbs configured.")
        return
    
    run_async(ctx, controller.toggle_all(ips))
    click.echo(f"Toggled {len(ips)} bulb(s)")


@cli.command()
@click.argument("level", type=int)
@click.option("--ip", "-i", help="Specific bulb IP (default: all bulbs)")
@click.pass_context
def brightness(ctx, level, ip):
    """Set brightness (0-255)."""
    controller = ctx.obj["controller"]
    config = ctx.obj["config"]
    ips = get_bulb_ips(config, ip)
    
    if not ips:
        click.echo("No bulbs configured.")
        return

    run_async(ctx, controller.turn_on_all(ips, level))
    click.echo(f"Set brightness to {level} on {len(ips)} bulb(s)")


@cli.command()
@click.argument("r", type=int)
@click.argument("g", type=int)
@click.argument("b", type=int)
@click.option("--ip", "-i", help="Specific bulb IP (default: all bulbs)")
@click.option("--brightness", "-B", type=int, help="Brightness level (0-255)")
@click.pass_context
def color(ctx, r, g, b, ip, brightness):
    """Set RGB color (0-255 for each)."""
    controller = ctx.obj["controller"]
    config = ctx.obj["config"]
    ips = get_bulb_ips(config, ip)
    
    if not ips:
        click.echo("No bulbs configured.")
        return
    
    run_async(ctx, controller.set_rgb_all(ips, r, g, b, brightness))
    click.echo(f"Set color to RGB({r}, {g}, {b}) on {len(ips)} bulb(s)")


@cli.command()
@click.argument("kelvin", type=int)
@click.option("--ip", "-i", help="Specific bulb IP (default: all bulbs)")
@click.option("--brightness", "-b", type=int, help="Brightness level (0-255)")
@click.pass_context
def temp(ctx, kelvin, ip, brightness):
    """Set color temperature in Kelvin (2200-6500)."""
    controller = ctx.obj["controller"]
    config = ctx.obj["config"]
    ips = get_bulb_ips(config, ip)
    
    if not ips:
        click.echo("No bulbs configured.")
        return
    
    run_async(ctx, controller.set_color_temp_all(ips, kelvin, brightness))
    click.echo(f"Set color temperature to {kelvin}K on {len(ips)} bulb(s)")


@cli.command()
@click.argument("name", required=False)
@click.option("--ip", "-i", help="Specific bulb IP (default: all bulbs)")
@click.pass_context
def preset(ctx, name, ip):
    """Apply a color preset. Run without arguments to list presets."""
    if not name:
        click.echo("Available presets:")
        for preset_name, settings in PRESETS.items():
            click.echo(f"  {preset_name}: {settings}")
        return
    
    controller = ctx.obj["controller"]
    config = ctx.obj["config"]
    ips = get_bulb_ips(config, ip)
    
    if not ips:
        click.echo("No bulbs configured.")
        return
    
    success = run_async(ctx, apply_preset(controller, ips, name))
    if success:
        click.echo(f"Applied preset '{name}' to {len(ips)} bulb(s)")
    else:
        click.echo(f"Unknown preset '{name}'. Run 'wizlight preset' to see available presets.")


@cli.command()
@click.pass_context
def config_path(ctx):
    """Show configuration file path."""
    config = ctx.obj["config"]
    click.echo(config._config_path)


if __name__ == "__main__":
    cli()
