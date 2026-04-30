"""Fire-and-forget WiZ UDP sender for low-latency screen sync."""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from typing import Optional

WIZ_PORT = 38899
DEFAULT_MAX_SEND_HZ = 28.0

_ENCODE_CACHE: dict[tuple[int, ...], bytes] = {}
_CACHE_LOCK = threading.Lock()
_MAX_CACHE_SIZE = 4096


def _clamp_channel(value: int) -> int:
    return max(0, min(255, int(value)))


def encode_set_pilot_payload(
    payload: tuple[int, ...],
    brightness: Optional[int] = None,
) -> bytes:
    """Encode an RGBW/RGBWW WiZ setPilot packet with explicit white channels."""

    if len(payload) not in {4, 5}:
        raise ValueError("WiZ payload must be RGBW or RGBWW")

    channels = tuple(_clamp_channel(value) for value in payload)
    dimming = None if brightness is None else max(10, min(100, round(int(brightness) / 255 * 100)))
    key = channels if dimming is None else (*channels, dimming)
    with _CACHE_LOCK:
        cached = _ENCODE_CACHE.get(key)
        if cached is not None:
            return cached

    params = {
        "state": any(channels),
        "r": channels[0],
        "g": channels[1],
        "b": channels[2],
    }
    if len(channels) == 5:
        params["c"] = channels[3]
        params["w"] = channels[4]
    else:
        params["c"] = 0
        params["w"] = channels[3]
    if dimming is not None and params["state"]:
        params["dimming"] = dimming

    encoded = json.dumps(
        {"method": "setPilot", "params": params},
        separators=(",", ":"),
    ).encode("utf-8")

    with _CACHE_LOCK:
        if len(_ENCODE_CACHE) >= _MAX_CACHE_SIZE:
            for old_key in list(_ENCODE_CACHE)[:512]:
                _ENCODE_CACHE.pop(old_key, None)
        _ENCODE_CACHE[key] = encoded
    return encoded


class BulbUDPSocket:
    """Persistent latest-wins sender for one WiZ bulb."""

    _SENTINEL = object()

    def __init__(self, ip: str, max_send_hz: float = DEFAULT_MAX_SEND_HZ):
        self.ip = ip
        self.max_send_hz = max(1.0, float(max_send_hz))
        self._min_interval = 1.0 / self.max_send_hz
        self._queue: queue.SimpleQueue[bytes | object] = queue.SimpleQueue()
        self._socket = self._connect()
        self._running = True
        self._sent = 0
        self._dropped = 0
        self._last_send_at = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"WiZFastUDP-{ip}")
        self._thread.start()

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.004)
        sock.connect((self.ip, WIZ_PORT))
        return sock

    def send(self, packet: bytes) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._dropped += 1
            except queue.Empty:
                break
        self._queue.put(packet)

    def stop(self) -> None:
        self._running = False
        self._queue.put(self._SENTINEL)
        self._thread.join(timeout=1.0)
        try:
            self._socket.close()
        except OSError:
            pass

    def _run(self) -> None:
        while self._running:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is self._SENTINEL:
                return
            if not isinstance(item, bytes):
                continue

            packet = item
            while not self._queue.empty():
                try:
                    next_item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if next_item is self._SENTINEL:
                    return
                if isinstance(next_item, bytes):
                    packet = next_item
                    self._dropped += 1

            now = time.perf_counter()
            wait_time = self._min_interval - (now - self._last_send_at)
            if wait_time > 0:
                time.sleep(wait_time)

            try:
                self._socket.send(packet)
            except OSError:
                try:
                    self._socket.close()
                except OSError:
                    pass
                try:
                    self._socket = self._connect()
                    self._socket.send(packet)
                except OSError:
                    continue
            self._sent += 1
            self._last_send_at = time.perf_counter()

    @property
    def stats(self) -> dict[str, int | str]:
        return {"ip": self.ip, "sent": self._sent, "dropped": self._dropped}


class FastBulbSenderPool:
    """Thread-safe pool of latest-wins UDP senders."""

    def __init__(self, max_send_hz: float = DEFAULT_MAX_SEND_HZ):
        self.max_send_hz = max_send_hz
        self._senders: dict[str, BulbUDPSocket] = {}
        self._lock = threading.Lock()

    def _get(self, ip: str) -> BulbUDPSocket:
        with self._lock:
            sender = self._senders.get(ip)
            if sender is None:
                sender = BulbUDPSocket(ip, max_send_hz=self.max_send_hz)
                self._senders[ip] = sender
            return sender

    def send_payload(self, ip: str, payload: tuple[int, ...], brightness: Optional[int] = None) -> None:
        self._get(ip).send(encode_set_pilot_payload(payload, brightness))

    def send_payload_map(
        self,
        payloads_by_ip: dict[str, tuple[int, ...]],
        brightness: Optional[int] = None,
    ) -> None:
        for ip, payload in payloads_by_ip.items():
            self.send_payload(ip, payload, brightness)

    def close(self) -> None:
        with self._lock:
            senders = list(self._senders.values())
            self._senders.clear()
        for sender in senders:
            sender.stop()

    @property
    def stats(self) -> list[dict[str, int | str]]:
        with self._lock:
            return [sender.stats for sender in self._senders.values()]
