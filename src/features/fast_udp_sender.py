"""Fire-and-forget WiZ UDP sender for low-latency screen sync."""

from __future__ import annotations

import queue
import socket
import threading
import time
from typing import Optional, Sequence

WIZ_PORT = 38899
DEFAULT_MAX_SEND_HZ = 28.0
_SNDBUF = 262144

_ENCODE_CACHE: dict[tuple[int, ...], bytes] = {}
_CACHE_LOCK = threading.Lock()
_MAX_CACHE_SIZE = 4096
_CACHE_EVICT_SIZE = 512

# Round each channel to the nearest multiple of this before caching AND sending.
# 8 -> 32 levels/channel: max 4/255 error (imperceptible for ambient light) while
# collapsing jittered, near-identical frames onto shared cache entries so the
# hot path skips JSON re-encoding far more often. Set to 1 to disable.
_QUANTIZE_STEP = 8

# UDP has no retransmit; on lossy Wi-Fi an occasional setPilot is dropped and
# the bulb keeps the wrong color until the next change. When the color actually
# CHANGES we fire the packet _BURST_REPEATS times ~_BURST_GAP apart (Hue-style
# loss insurance). Unchanged repeats (heartbeat/force-refresh) send once.
_BURST_REPEATS = 3
_BURST_GAP = 0.003

_FMT_5 = (
    b'{"method":"setPilot","params":{"state":%s,'
    b'"r":%d,"g":%d,"b":%d,"c":%d,"w":%d%s}}'
)
_FMT_4 = (
    b'{"method":"setPilot","params":{"state":%s,'
    b'"r":%d,"g":%d,"b":%d,"c":0,"w":%d%s}}'
)
_FMT_DIM = b',"dimming":%d'


def _clamp_channel(value: int) -> int:
    return max(0, min(255, int(value)))


def _quantize_channels(channels: tuple[int, ...]) -> tuple[int, ...]:
    """Round channels to the nearest _QUANTIZE_STEP for cache-friendly reuse.

    The quantized values are what actually get sent, so the cache stays correct
    (no near-color ever reuses another color's packet).
    """
    if _QUANTIZE_STEP <= 1:
        return channels
    step = _QUANTIZE_STEP
    half = step // 2
    quant = tuple(min(255, ((c + half) // step) * step) for c in channels)
    # Never let a lit color collapse to all-zero (that would flip state to off).
    if any(channels) and not any(quant):
        return channels
    return quant


def encode_set_pilot_payload(
    payload: tuple[int, ...],
    brightness: Optional[int] = None,
) -> bytes:
    """Encode an RGBW/RGBWW WiZ setPilot packet with explicit white channels."""

    if len(payload) not in {4, 5}:
        raise ValueError("WiZ payload must be RGBW or RGBWW")

    channels = _quantize_channels(tuple(_clamp_channel(value) for value in payload))
    dimming = None if brightness is None else max(10, min(100, round(int(brightness) / 255 * 100)))
    key = channels if dimming is None else (*channels, dimming)

    cached = _ENCODE_CACHE.get(key)
    if cached is not None:
        return cached

    state = b"true" if any(channels) else b"false"
    dim_part = b"" if dimming is None or not any(channels) else _FMT_DIM % dimming
    if len(channels) == 5:
        encoded = _FMT_5 % (state, channels[0], channels[1], channels[2], channels[3], channels[4], dim_part)
    else:
        encoded = _FMT_4 % (state, channels[0], channels[1], channels[2], channels[3], dim_part)

    with _CACHE_LOCK:
        if len(_ENCODE_CACHE) >= _MAX_CACHE_SIZE:
            for old_key in list(_ENCODE_CACHE)[:_CACHE_EVICT_SIZE]:
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
        self._last_packet_sent: bytes | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"WiZFastUDP-{ip}")
        self._thread.start()

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.004)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SNDBUF)
        except OSError:
            pass
        try:
            # IPTOS_LOWDELAY (0x10): hint routers / Wi-Fi WMM to prioritize these
            # latency-sensitive screen-sync packets. Honored on Linux and most
            # routers; silently ignored on some Windows stacks (harmless).
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x10)
        except OSError:
            pass
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

            # Burst only on a real color change; identical repeats send once.
            repeats = _BURST_REPEATS if packet != self._last_packet_sent else 1
            for attempt in range(repeats):
                # A queued newer colour supersedes retransmissions of this one.
                if attempt and not self._queue.empty():
                    break
                if not self._send_raw(packet):
                    break
                if attempt + 1 < repeats:
                    time.sleep(_BURST_GAP)
            self._last_packet_sent = packet
            self._sent += 1
            self._last_send_at = time.perf_counter()

    def _send_raw(self, packet: bytes) -> bool:
        try:
            self._socket.send(packet)
            return True
        except OSError:
            try:
                self._socket.close()
            except OSError:
                pass
            try:
                self._socket = self._connect()
                self._socket.send(packet)
                return True
            except OSError:
                return False

    @property
    def stats(self) -> dict[str, int | str]:
        return {"ip": self.ip, "sent": self._sent, "dropped": self._dropped}


class FastBulbSenderPool:
    """Thread-safe pool of latest-wins UDP senders."""

    def __init__(
        self,
        max_send_hz: float = DEFAULT_MAX_SEND_HZ,
        prewarm_ips: Optional[Sequence[str]] = None,
    ):
        self.max_send_hz = max_send_hz
        self._senders: dict[str, BulbUDPSocket] = {}
        self._lock = threading.Lock()
        if prewarm_ips:
            for ip in prewarm_ips:
                self._get(ip)

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
            self._get(ip).send(encode_set_pilot_payload(payload, brightness))

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
