"""
fast_udp_sender.py
==================
Fire-and-forget UDP sender optimised for real-time screen sync.

Design goals
------------
* Zero async overhead – raw socket.sendto() on a background thread.
* One persistent OS socket per bulb (no per-packet socket creation).
* Latest-wins command queue: stale packets are dropped automatically.
* Pre-encoded JSON bytes cached per (r,g,b,dimming) tuple.
* Thread-safe, designed to be called from any thread at any rate.

WiZ 9W E27 protocol notes (from reverse-engineering)
-----------------------------------------------------
* UDP port 38899, JSON payload, UTF-8.
* For screen sync always send:
    {"method":"setPilot","params":{"state":true,"r":R,"g":G,"b":B,"c":0,"w":0,"dimming":100}}
  Setting c=0 / w=0 is CRITICAL – omitting them lets the bulb infer
  white-channel values that wash out saturated colours.
* dimming range: 10–100 (10 = minimum, 100 = maximum).
  Values below 10 turn the bulb off; never send dimming < 10.
* The bulb silently discards packets while its firmware is busy
  (typically < 2 ms).  Sending at > 30 Hz can cause visible stuttering;
  25–30 Hz is the sweet spot for this model.
* No acknowledgement is needed for screen sync – skip response waiting
  entirely.  The ~5 ms round-trip is the biggest latency killer.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from typing import Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Packet helpers
# ---------------------------------------------------------------------------

_ENCODE_CACHE: Dict[Tuple[int, int, int, int], bytes] = {}
_CACHE_LOCK = threading.Lock()
_MAX_CACHE = 4096  # evict when exceeded


def _encode_packet(r: int, g: int, b: int, dimming: int = 100) -> bytes:
    """Return a pre-encoded setPilot packet, using an LRU-style cache."""
    key = (r, g, b, dimming)
    with _CACHE_LOCK:
        cached = _ENCODE_CACHE.get(key)
        if cached is not None:
            return cached
        if len(_ENCODE_CACHE) >= _MAX_CACHE:
            # Evict the oldest 512 entries
            for k in list(_ENCODE_CACHE.keys())[:512]:
                del _ENCODE_CACHE[k]
    # Build minimal packet – c=0 w=0 prevents white-channel bleed
    payload = json.dumps(
        {
            "method": "setPilot",
            "params": {
                "state": True,
                "r": int(r),
                "g": int(g),
                "b": int(b),
                "c": 0,
                "w": 0,
                "dimming": int(max(10, min(100, dimming))),
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    with _CACHE_LOCK:
        _ENCODE_CACHE[key] = payload
    return payload


def _encode_rgbww_packet(
    r: int, g: int, b: int, c: int, w: int, dimming: int = 100
) -> bytes:
    """Encode a full RGBWW packet (used for white/neutral colours)."""
    return json.dumps(
        {
            "method": "setPilot",
            "params": {
                "state": True,
                "r": int(r),
                "g": int(g),
                "b": int(b),
                "c": int(c),
                "w": int(w),
                "dimming": int(max(10, min(100, dimming))),
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Per-bulb sender
# ---------------------------------------------------------------------------

_WIZ_PORT = 38899
_SEND_TIMEOUT_S = 0.004  # 4 ms socket timeout


class BulbUDPSocket:
    """
    Persistent UDP socket bound to one bulb IP.

    A single background thread drains the latest-wins queue and sends
    the packet.  Stale commands are automatically discarded – only the
    most recent colour is ever sent.
    """

    _SENTINEL = object()

    def __init__(self, ip: str, port: int = _WIZ_PORT):
        self._ip = ip
        self._port = port
        self._addr = (ip, port)

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(_SEND_TIMEOUT_S)
        # Pre-connect so sendto becomes just send (saves syscall overhead)
        self._sock.connect(self._addr)

        # Latest-wins: only one item lives in the queue at a time
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"BulbSender-{ip}"
        )
        self._running = True
        self._thread.start()

        # Stats
        self._sent = 0
        self._dropped = 0
        self._last_send_ns: Optional[int] = None

    # ------------------------------------------------------------------
    def send(self, packet: bytes) -> None:
        """Enqueue a packet.  Any previously queued packet is discarded."""
        # Drain the queue (latest-wins)
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._dropped += 1
            except queue.Empty:
                break
        self._queue.put(packet)

    def send_rgb(self, r: int, g: int, b: int, dimming: int = 100) -> None:
        self.send(_encode_packet(r, g, b, dimming))

    def send_rgbww(
        self, r: int, g: int, b: int, c: int, w: int, dimming: int = 100
    ) -> None:
        self.send(_encode_rgbww_packet(r, g, b, c, w, dimming))

    def stop(self) -> None:
        self._running = False
        self._queue.put(self._SENTINEL)
        self._thread.join(timeout=1.0)
        try:
            self._sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while self._running:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is self._SENTINEL:
                break
            if not isinstance(item, bytes):
                continue

            # Drain again: only send the very latest
            latest = item
            while not self._queue.empty():
                try:
                    nxt = self._queue.get_nowait()
                    if nxt is self._SENTINEL:
                        return
                    if isinstance(nxt, bytes):
                        self._dropped += 1
                        latest = nxt
                except queue.Empty:
                    break

            try:
                self._sock.send(latest)
                self._sent += 1
                self._last_send_ns = time.perf_counter_ns()
            except OSError:
                # Socket error – recreate silently
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._sock.settimeout(_SEND_TIMEOUT_S)
                try:
                    self._sock.connect(self._addr)
                    self._sock.send(latest)
                    self._sent += 1
                except OSError:
                    pass

    # ------------------------------------------------------------------
    @property
    def stats(self) -> dict:
        return {
            "ip": self._ip,
            "sent": self._sent,
            "dropped": self._dropped,
        }


# ---------------------------------------------------------------------------
# Pool – manages one BulbUDPSocket per bulb IP
# ---------------------------------------------------------------------------


class FastBulbSenderPool:
    """
    Manages a pool of BulbUDPSocket instances, one per bulb IP.

    Thread-safe.  Sockets are created lazily on first use.

    Usage::

        pool = FastBulbSenderPool()

        # Fire-and-forget – returns immediately
        pool.send_rgb("192.168.1.10", 255, 100, 50)

        # Send to multiple bulbs (in parallel – one send per socket thread)
        pool.send_rgb_map({"192.168.1.10": (255, 100, 50),
                           "192.168.1.11": (50,  200, 255)})

        pool.close()
    """

    def __init__(self):
        self._sockets: Dict[str, BulbUDPSocket] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _get(self, ip: str) -> BulbUDPSocket:
        with self._lock:
            if ip not in self._sockets:
                self._sockets[ip] = BulbUDPSocket(ip)
            return self._sockets[ip]

    # ------------------------------------------------------------------
    def send_rgb(self, ip: str, r: int, g: int, b: int, dimming: int = 100) -> None:
        """Send a colour to one bulb, fire-and-forget."""
        self._get(ip).send_rgb(r, g, b, dimming)

    def send_rgbww(
        self,
        ip: str,
        r: int,
        g: int,
        b: int,
        c: int,
        w: int,
        dimming: int = 100,
    ) -> None:
        self._get(ip).send_rgbww(r, g, b, c, w, dimming)

    def send_rgb_map(
        self,
        colors: Dict[str, Tuple[int, int, int]],
        dimming: int = 100,
    ) -> None:
        """Send one colour per bulb from a {ip: (r,g,b)} dict."""
        for ip, (r, g, b) in colors.items():
            self._get(ip).send_rgb(r, g, b, dimming)

    def send_rgbww_map(
        self,
        colors: Dict[str, Tuple[int, int, int, int, int]],
        dimming: int = 100,
    ) -> None:
        """Send full RGBWW per bulb from a {ip: (r,g,b,c,w)} dict."""
        for ip, (r, g, b, c, w) in colors.items():
            self._get(ip).send_rgbww(r, g, b, c, w, dimming)

    # ------------------------------------------------------------------
    def remove(self, ip: str) -> None:
        with self._lock:
            sock = self._sockets.pop(ip, None)
        if sock:
            sock.stop()

    def close(self) -> None:
        with self._lock:
            sockets = list(self._sockets.values())
            self._sockets.clear()
        for sock in sockets:
            sock.stop()

    @property
    def stats(self) -> list:
        with self._lock:
            return [s.stats for s in self._sockets.values()]

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
