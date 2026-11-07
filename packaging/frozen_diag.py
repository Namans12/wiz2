"""Frozen-environment diagnostic: run the sync stack inside a PyInstaller EXE.

Purpose: the GUI works from source but reportedly not as a frozen EXE. This
console build exercises every stage (dxcam, mss, engine, UDP, bulb reply) and
prints exactly which one dies when frozen. Build via packaging/FrozenDiag.spec.
"""

from __future__ import annotations

import argparse
import ipaddress
import socket
import sys
import time
import traceback


def stage(name):
    print(f"\n=== {name} ===", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Allow screen capture and bulb queries')
    parser.add_argument('--bulb', help='Explicit IPv4 address of the bulb to query')
    args = parser.parse_args()
    if not args.execute:
        parser.print_help()
        return 0
    if not args.bulb:
        parser.error('--execute requires --bulb')
    try:
        bulb = str(ipaddress.IPv4Address(args.bulb))
    except ipaddress.AddressValueError:
        parser.error('--bulb must be an IPv4 address')
    failures = 0
    print("frozen:", getattr(sys, "frozen", False), "| exe:", sys.executable, flush=True)

    stage("1. imports")
    try:
        from src.features import screen_sync_v2 as v2
        print("screen_sync_v2 import OK | HAS_DXCAM =", v2.HAS_DXCAM, flush=True)
    except Exception:
        failures += 1
        traceback.print_exc()
        return 1

    stage("2. dxcam direct (BGRA mode, no cv2)")
    cam = None
    try:
        if v2.HAS_DXCAM:
            import dxcam
            cam = dxcam.create(device_idx=0, output_idx=0, output_color="BGRA")
            print("create:", "OK" if cam is not None else "returned None", flush=True)
            if cam is not None:
                f = cam.grab()
                print("grab:", None if f is None else f.shape, flush=True)
        else:
            print("dxcam not importable in frozen app", flush=True)
    except Exception:
        failures += 1
        traceback.print_exc()
    finally:
        if cam is not None:
            try:
                cam.release()
            except Exception:
                pass

    stage("3. mss direct")
    try:
        import mss
        with mss.mss() as s:
            print("monitors:", s.monitors, flush=True)
            shot = s.grab(s.monitors[1] if len(s.monitors) > 1 else s.monitors[0])
            print("grab:", shot.size, flush=True)
    except Exception:
        failures += 1
        traceback.print_exc()

    stage("4. engine 3s with persisted config")
    sync = None
    try:
        from src.core.config import Config
        cfg = Config.load()
        sync = v2.OptimizedScreenSync(
            on_color_change=lambda m: None,
            config=v2.build_optimized_capture_config(cfg.screen_sync, active_regions=()),
        )
        sync.start()
        time.sleep(3.0)
        snap = sync.debug_snapshot
        if not snap.get("frames_processed", 0):
            raise RuntimeError("Engine did not process any frames")
        for key in (
            "capture_method", "capture_status", "frames_processed", "frameTimeMs",
            "targetColors", "updates_sent", "last_error",
        ):
            print(f"{key} = {snap.get(key)}", flush=True)
    except Exception:
        failures += 1
        traceback.print_exc()

    finally:
        if sync is not None:
            sync.stop()

    stage("5. bulb reachability (raw getPilot)")
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2.0)
        s.connect((bulb, 38899))
        s.send(b'{"method":"getPilot","params":{}}')
        reply = s.recv(2048)
        print("reply:", reply.decode()[:160], flush=True)
    except Exception:
        failures += 1
        traceback.print_exc()

    finally:
        if s is not None:
            s.close()

    stage("6. pywizlight preflight (what the GUI uses)")
    try:
        import asyncio
        from src.core.bulb_controller import BulbController

        async def preflight():
            controller = BulbController()
            try:
                return await controller.resolve_screen_sync_targets([bulb])
            finally:
                await controller.close_async()

        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        result = asyncio.run(preflight())
        print("resolve_screen_sync_targets ->", result, flush=True)
        if not result:
            raise RuntimeError("Preflight found no reachable bulb")
    except Exception:
        failures += 1
        traceback.print_exc()

    print("\nDIAG COMPLETE", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
