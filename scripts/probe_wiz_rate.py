"""Empirically probe a WiZ bulb's real update-rate ceiling and dimming latency.

Why this exists
---------------
Community figures for the WiZ setPilot rate cap range from 20 to 30 Hz and none
are lab-measured; nothing at all is published on whether `dimming`-only changes
apply faster or slower than RGB changes. This script measures both against YOUR
bulb so the sender cap (`fast_udp_sender.DEFAULT_MAX_SEND_HZ`) and any future
dimming-split work rest on data instead of folklore.

What it measures
----------------
1. **Rate ramp** — for each candidate rate, fires alternating red/blue setPilot
   packets for N seconds and counts the bulb's UDP replies on the same socket.
   Reply ratio collapsing (or reply latency ballooning) marks the firmware
   ceiling. Watch the bulb too: visible stutter usually appears one step past
   the knee.
2. **Dimming vs RGB latency** — sends isolated dimming-only vs rgb-only updates
   (2 Hz, cold) and compares reply round-trip times.

Usage
-----
    python scripts/probe_wiz_rate.py --ip 192.168.1.23
    python scripts/probe_wiz_rate.py --ip 192.168.1.23 --rates 10,20,25,28,32,40 --seconds 6

The bulb is restored to a soft warm white at the end.
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import time

WIZ_PORT = 38899


def _packet(**params) -> bytes:
    return json.dumps({"method": "setPilot", "params": params}).encode()


def _drain(sock: socket.socket) -> int:
    """Consume any queued replies; return how many were read."""
    count = 0
    while True:
        try:
            sock.recv(2048)
            count += 1
        except (socket.timeout, BlockingIOError):
            return count


def probe_rate(ip: str, rate_hz: float, seconds: float) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.0005)
    sock.connect((ip, WIZ_PORT))
    red = _packet(state=True, r=255, g=0, b=0, c=0, w=0)
    blue = _packet(state=True, r=0, g=0, b=255, c=0, w=0)

    interval = 1.0 / rate_hz
    sent = 0
    replies = 0
    deadline = time.perf_counter() + seconds
    next_send = time.perf_counter()
    toggle = False
    while time.perf_counter() < deadline:
        now = time.perf_counter()
        if now >= next_send:
            sock.send(blue if toggle else red)
            toggle = not toggle
            sent += 1
            next_send += interval
        replies += _drain(sock)
        time.sleep(0.0002)
    # Grace window for trailing replies.
    end = time.perf_counter() + 0.5
    sock.settimeout(0.05)
    while time.perf_counter() < end:
        replies += _drain(sock)
    sock.close()
    return {
        "rate_hz": rate_hz,
        "sent": sent,
        "replies": replies,
        "reply_ratio": round(replies / max(1, sent), 3),
    }


def probe_reply_latency(ip: str, mode: str, samples: int = 20) -> dict:
    """Round-trip time for isolated updates. mode: 'rgb' or 'dimming'."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    sock.connect((ip, WIZ_PORT))
    rtts = []
    lost = 0
    for i in range(samples):
        if mode == "rgb":
            pkt = _packet(state=True, r=255 if i % 2 else 40, g=60, b=40, c=0, w=0)
        else:
            pkt = _packet(state=True, dimming=100 if i % 2 else 20)
        t0 = time.perf_counter()
        sock.send(pkt)
        try:
            sock.recv(2048)
            rtts.append((time.perf_counter() - t0) * 1000.0)
        except socket.timeout:
            lost += 1
        time.sleep(0.5)  # cold sends — isolate from rate effects
    sock.close()
    return {
        "mode": mode,
        "samples": samples,
        "lost": lost,
        "median_ms": round(statistics.median(rtts), 2) if rtts else None,
        "p90_ms": round(sorted(rtts)[int(len(rtts) * 0.9)], 2) if rtts else None,
    }


def restore(ip: str) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect((ip, WIZ_PORT))
    sock.send(_packet(state=True, r=0, g=0, b=0, c=0, w=120, dimming=60))
    sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", required=True, help="Bulb IP address")
    parser.add_argument("--rates", default="10,20,25,28,32,40",
                        help="Comma-separated send rates in Hz")
    parser.add_argument("--seconds", type=float, default=5.0,
                        help="Duration per rate step")
    parser.add_argument("--skip-dimming", action="store_true",
                        help="Skip the dimming-vs-RGB latency comparison")
    args = parser.parse_args()

    rates = [float(r) for r in args.rates.split(",") if r.strip()]
    print(f"Probing {args.ip} — watch the bulb for visible stutter per step.\n")
    print(f"{'rate Hz':>8} | {'sent':>5} | {'replies':>7} | {'ratio':>6}")
    print("-" * 38)
    results = []
    try:
        for rate in rates:
            row = probe_rate(args.ip, rate, args.seconds)
            results.append(row)
            print(f"{row['rate_hz']:>8.0f} | {row['sent']:>5} | "
                  f"{row['replies']:>7} | {row['reply_ratio']:>6.2f}")
            time.sleep(1.0)

        knee = None
        for row in results:
            if row["reply_ratio"] < 0.85:
                knee = row["rate_hz"]
                break
        print()
        if knee:
            print(f"Reply ratio drops below 85% at ~{knee:.0f} Hz — set the "
                  f"sender cap at or below the previous step.")
        else:
            print("No reply collapse observed in the tested range — the cap "
                  "may sit above the highest rate tested (or replies aren't "
                  "the limiting factor; trust your eyes on stutter).")

        if not args.skip_dimming:
            print("\nDimming vs RGB reply latency (cold sends):")
            for mode in ("rgb", "dimming"):
                stats = probe_reply_latency(args.ip, mode)
                print(f"  {mode:>7}: median={stats['median_ms']} ms  "
                      f"p90={stats['p90_ms']} ms  lost={stats['lost']}/{stats['samples']}")
    finally:
        restore(args.ip)
        print("\nBulb restored to soft warm white.")


if __name__ == "__main__":
    main()
