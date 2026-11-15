"""Opt-in two-bulb state/brightness validation; restores both lamps afterward.

Measures dispatch to *reported state*, not optical latency or color accuracy.
The same 50 ms observer queries run in both A/B modes and can affect Wi-Fi
sleep behavior. Use a fixed camera for unperturbed screen-to-light latency.
"""

from __future__ import annotations

import argparse
import copy
import json
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.async_runtime import BackgroundAsyncLoop
from src.core.bulb_controller import BulbController
from src.core.config import Config
from src.features.fast_udp_sender import FastBulbSenderPool, expected_pilot_state, pilot_state_matches
from src.features.screen_sync_v2 import OptimizedCaptureConfig, OptimizedScreenSync
from src.features.sync_transport import brightness_for_bulbs, prepare_screen_sync_mapping


class PilotClient:
    def __init__(self, ip, port=38899):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect((ip, port))
        self.sequence = 0

    def ask(self, method="getPilot", params=None, timeout=0.6):
        self.sequence += 1
        sequence = self.sequence
        self.sock.send(json.dumps({"method": method, "id": sequence, "params": params or {}}).encode())
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.sock.settimeout(max(0.001, deadline - time.perf_counter()))
            try:
                reply = json.loads(self.sock.recv(4096))
            except (OSError, ValueError):
                return None
            if isinstance(reply, dict) and reply.get("id") == sequence and reply.get("method") == method:
                return reply.get("result")
        return None

    def observe(self, expected, started, timeout=2.5):
        deadline = started + timeout
        last = None
        while time.perf_counter() < deadline:
            before = time.perf_counter()
            last = self.ask(timeout=min(0.3, max(0.001, deadline - before)))
            if isinstance(last, dict) and pilot_state_matches(last, expected):
                return {"matched": True, "ms": (time.perf_counter() - started) * 1000, "state": last}
            time.sleep(max(0, 0.05 - (time.perf_counter() - before)))
        return {"matched": False, "ms": None, "state": last}

    def restore(self, prior):
        if not prior["state"]:
            params = {"state": False}
        elif prior.get("sceneId"):
            params = {"state": True, "sceneId": prior["sceneId"], "dimming": prior.get("dimming", 100)}
        elif prior.get("temp") and not any(prior.get(k, 0) for k in ("r", "g", "b")):
            params = {"state": True, "temp": prior["temp"], "dimming": prior.get("dimming", 100)}
        else:
            params = {k: prior[k] for k in ("state", "r", "g", "b", "c", "w", "dimming") if k in prior}
        for _ in range(4):
            self.ask("setPilot", params)
            actual = self.ask()
            if isinstance(actual, dict) and all(actual.get(k, 0) == v for k, v in params.items()):
                return True
        return False


def summary(rows):
    values = [r["ms"] for r in rows if r["matched"]]
    return {"count": len(rows), "matched": len(values), **{
        key: float(np.percentile(values, percentile)) if values else None
        for key, percentile in (("median_ms", 50), ("p95_ms", 95), ("max_ms", 100))
    }}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--soak-seconds", type=float, default=120)
    args = parser.parse_args()
    if len(set(args.ip)) != 2 or len(args.ip) != 2 or not 0 <= args.soak_seconds <= 3600:
        parser.error("Provide two distinct --ip addresses and soak duration 0–3600 seconds")
    ips = args.ip
    clients = {ip: PilotClient(ip) for ip in ips}
    prior, pool = {}, None
    runner, controller = BackgroundAsyncLoop(), BulbController()
    report = {"scope": __doc__, "segments": [], "restored": {}}
    try:
        for ip, client in clients.items():
            for _ in range(4):
                state = client.ask()
                if isinstance(state, dict) and isinstance(state.get("state"), bool):
                    prior[ip] = state
                    break
        if len(prior) != 2:
            raise RuntimeError("Both bulbs must answer before any changes are made")
        settings = copy.deepcopy(Config.load().screen_sync)
        runner.run(prepare_screen_sync_mapping(controller, ips, settings), timeout=20)
        report["mixing_model"] = settings.mixing_model
        engine = OptimizedScreenSync(lambda _: None, OptimizedCaptureConfig(
            mode="zones", active_regions=("left", "right"), use_gpu=False,
            parallel_regions=False, brightness_mode="follow", ignore_letterbox=False,
        ))
        layout = dict(zip(ips, ("left", "right")))
        palette = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 255, 255),
                   (255, 0, 255), (255, 255, 0), (255, 255, 255), (128, 128, 128),
                   (24, 24, 24), (255, 120, 20), (100, 30, 220), (0, 0, 0)]

        def frame_payload(index):
            color = np.array(palette[index % len(palette)])
            left, right = (color, color // 4) if index % 2 else (color // 4, color)
            frame = np.empty((90, 160, 3), dtype=np.uint8)
            frame[:, :80], frame[:, 80:] = left, right
            engine._frame_scene_cut = True
            colors = engine._extract_colors(frame)
            if not colors:
                return {}, {}  # Follow mode deliberately holds brief black frames.
            mapped = controller.map_screen_sync_payloads_cached({ip: colors[layout[ip]] for ip in ips})
            if len(mapped) != 2:
                raise RuntimeError("Both color mappers must be ready")
            return mapped, brightness_for_bulbs(engine, ips, "zones", layout)

        with ThreadPoolExecutor(max_workers=2) as executor:
            for kind in "ABBA":
                pool = FastBulbSenderPool(prewarm_ips=ips, keep_awake=kind == "B")
                time.sleep(1.0)
                segment = {"kind": kind, "cases": []}
                report["segments"].append(segment)
                for index in range(len(palette)):
                    frame_started = time.perf_counter()
                    payloads, levels = frame_payload(index)
                    while not payloads and time.perf_counter() - frame_started < 1.2:
                        time.sleep(0.05)
                        payloads, levels = frame_payload(index)
                    if not payloads:
                        raise RuntimeError("Frame did not produce output after the black-frame hold")
                    started = time.perf_counter()
                    pool.send_payload_map(payloads, levels)
                    jobs = {ip: executor.submit(clients[ip].observe,
                            expected_pilot_state(payloads[ip], levels[ip]), started) for ip in ips}
                    results = {ip: job.result() for ip, job in jobs.items()}
                    segment["cases"].append({"rgb": palette[index], "brightness": levels,
                                             "frame_preparation_ms": (started - frame_started) * 1000,
                                             "results": results})
                    time.sleep(max(0, 0.8 - (time.perf_counter() - started)))
                segment["link"] = pool.link_stats
                segment["sender"] = pool.stats
                pool.close()
                pool = None
                print(kind, {ip: summary([case["results"][ip] for case in segment["cases"]]) for ip in ips}, flush=True)
                time.sleep(1.0)

            pool = FastBulbSenderPool(prewarm_ips=ips, keep_awake=True)
            payloads, levels = frame_payload(2)
            pool.send_payload_map(payloads, levels)
            for ip in ips:
                result = clients[ip].observe(expected_pilot_state(payloads[ip], levels[ip]), time.perf_counter())
                if not result["matched"]:
                    raise RuntimeError("Cannot establish initial state for recovery test")
            time.sleep(0.1)
            # Simulate an external state change. No new frame/update is sent.
            for ip in ips:
                clients[ip].ask("setPilot", {"state": False})
            started = time.perf_counter()
            jobs = {ip: executor.submit(clients[ip].observe,
                    expected_pilot_state(payloads[ip], levels[ip]), started) for ip in ips}
            report["recovery"] = {ip: job.result() for ip, job in jobs.items()}
            report["recovery_link"] = pool.link_stats
            soak_start, cpu_start = time.perf_counter(), time.process_time()
            count = 0
            report["soak_samples"] = []
            while time.perf_counter() - soak_start < args.soak_seconds:
                before = time.perf_counter()
                # Alternate fast cuts (5 Hz) and static scenes (10 s).
                elapsed = before - soak_start
                index = int(elapsed * 5) if int(elapsed) % 30 < 20 else 6
                payloads, levels = frame_payload(index)
                pool.send_payload_map(payloads, levels)
                count += 1
                if count % 100 == 0:
                    report["soak_samples"].append({"seconds": elapsed, "links": pool.link_stats})
                time.sleep(max(0, 0.05 - (time.perf_counter() - before)))
            report["soak"] = {"seconds": time.perf_counter() - soak_start,
                              "process_cpu_seconds": time.process_time() - cpu_start,
                              "updates": count, "links": pool.link_stats, "senders": pool.stats}
            print("soak", report["soak"], flush=True)
    finally:
        if pool is not None:
            pool.close()
        for ip, state in prior.items():
            report["restored"][ip] = clients[ip].restore(state)
        for client in clients.values():
            client.sock.close()
        try:
            runner.run(controller.close_async(), timeout=3)
        finally:
            runner.shutdown()
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print("report:", args.out, "restored:", report["restored"], flush=True)


if __name__ == "__main__":
    main()
