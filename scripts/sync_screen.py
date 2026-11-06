#!/usr/bin/env python3
"""Quick script to run screen sync."""

import signal
import sys
import time
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.async_runtime import BackgroundAsyncLoop
from src.core.bulb_controller import BulbController
from src.core.config import Config
from src.features.screen_sync import build_bulb_color_map, effective_screen_sync_mode, resolve_active_regions
from src.features.screen_sync_v2 import OptimizedScreenSync, build_optimized_capture_config
from src.features.fast_udp_sender import FastBulbSenderPool
from src.features.sync_transport import ScreenSyncTransport, prepare_screen_sync_mapping


def main():
    config = Config.load()
    if not config.bulbs:
        print("No bulbs configured. Run 'wizlight discover' first.")
        return
    controller = BulbController()
    runner = BackgroundAsyncLoop()
    screen_sync = None
    transport = None
    old_handler = None
    running = True

    def signal_handler(sig, frame):
        nonlocal running
        running = False

    try:
        configured_ips = [bulb.ip for bulb in config.bulbs]
        ips = runner.run(controller.refresh_screen_sync_targets(configured_ips), timeout=6.0)
        if not ips:
            print("No reachable bulbs available for screen sync.")
            return
        settings = config.screen_sync
        runner.run(prepare_screen_sync_mapping(controller, ips, settings), timeout=8.0)
        active_regions = resolve_active_regions(settings.bulb_layout, ips)
        mode = effective_screen_sync_mode(settings.mode, active_regions)
        layout = dict(settings.bulb_layout) if mode == "zones" else {}
        transport = ScreenSyncTransport(
            controller, runner.submit,
            FastBulbSenderPool(max_send_hz=min(28, max(12, settings.max_fps)), prewarm_ips=ips),
        )

        def on_color_change(colors_by_target):
            if screen_sync is None:
                return
            colors = build_bulb_color_map(ips, colors_by_target, mode, layout)
            transport.send(colors, screen_sync.output_brightness)

        screen_sync = OptimizedScreenSync(
            on_color_change=on_color_change,
            config=build_optimized_capture_config(settings, active_regions),
        )
        old_handler = signal.signal(signal.SIGINT, signal_handler)
        screen_sync.start()
        print(f"Screen sync: {mode}, {len(ips)} bulb(s), {screen_sync.capture_method.upper()}")
        print("Press Ctrl+C to stop.")
        while running and screen_sync.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if screen_sync is not None:
                screen_sync.stop()
        finally:
            try:
                if transport is not None:
                    transport.close()
            finally:
                try:
                    runner.cancel_pending(timeout=2.0)
                    runner.run(controller.close_async(), timeout=2.0)
                finally:
                    runner.shutdown()
                    if old_handler is not None:
                        signal.signal(signal.SIGINT, old_handler)
        print("Screen sync stopped.")


if __name__ == "__main__":
    main()
