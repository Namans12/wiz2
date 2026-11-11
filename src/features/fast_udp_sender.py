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

_ENCODE_CACHE: dict[tuple[tuple[int, ...], Optional[int]], bytes] = {}
_CACHE_LOCK = threading.Lock()
_MAX_CACHE_SIZE = 4096
_CACHE_EVICT_SIZE = 512

# UDP has no retransmit; on lossy Wi-Fi an occasional setPilot is dropped and
# the bulb keeps the wrong color until the next change. When the color actually
# CHANGES we fire the packet _BURST_REPEATS times ~_BURST_GAP apart (Hue-style
# loss insurance). Unchanged repeats (heartbeat/force-refresh) send once.
_BURST_REPEATS = 3
_BURST_GAP = 0.003
# A short burst can be lost as a group during Wi-Fi contention. Repeat a
# settled update later, but cancel these retries as soon as a newer one queues.
# The earlier retry is an additional recovery opportunity; the later retry
# still covers longer loss bursts. Each adds one datagram per settled change.
_SETTLED_RETRY_DELAYS = (0.06, 0.15, 0.45)

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


def encode_set_pilot_payload(
    payload: tuple[int, ...],
    brightness: Optional[int] = None,
) -> bytes:
    """Encode an RGBW/RGBWW WiZ setPilot packet with explicit white channels."""

    if len(payload) not in {4, 5}:
        raise ValueError("WiZ payload must be RGBW or RGBWW")

    # Preserve the mapper's channel values: cache reuse must never change hue.
    channels = tuple(_clamp_channel(value) for value in payload)
    dimming = None if brightness is None else max(10, min(100, round(int(brightness) / 255 * 100)))
    key = (channels, dimming)

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
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._socket = self._connect()
        self._running = True
        self._sent = 0
        self._dropped = 0
        self._attempted = 0
        self._datagrams_sent = 0
        self._send_errors = 0
        self._last_error: Optional[str] = None
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
        try:
            sock.connect((self.ip, WIZ_PORT))
        except OSError:
            sock.close()
            raise
        return sock

    def send(self, packet: bytes) -> bool:
        """Queue a packet, or return False once shutdown has begun."""
        with self._state_lock:
            if not self._running:
                return False
            self._drain_latest(packet)
            self._queue.put(packet)
            return True

    def _drain_latest(self, packet: bytes) -> bytes:
        """Drain pending updates while holding _state_lock."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return packet
            if isinstance(item, bytes):
                packet = item
                self._dropped += 1

    def stop(self) -> None:
        with self._state_lock:
            if self._running:
                self._running = False
                self._stop_event.set()
                self._queue.put(self._SENTINEL)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        retry_packet: bytes | None = None
        retry_deadlines: list[float] = []
        retry_group_counted = False
        try:
            while not self._stop_event.is_set():
                timeout = max(0.0, retry_deadlines[0] - time.perf_counter()) if retry_deadlines else 1.0
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    if not retry_deadlines or retry_packet is None:
                        continue
                    with self._state_lock:
                        if not self._running:
                            return
                        # Queueing and this check share the same lock, so a
                        # newer color cannot be followed by a stale retry.
                        if not self._queue.empty():
                            continue
                        success = self._send_raw(retry_packet)
                        if success:
                            self._datagrams_sent += 1
                            self._last_error = None
                            if not retry_group_counted:
                                self._last_packet_sent = retry_packet
                                self._sent += 1
                                retry_group_counted = True
                        else:
                            self._send_errors += 1
                    retry_deadlines.pop(0)
                    continue
                if item is self._SENTINEL:
                    return
                if not isinstance(item, bytes):
                    continue
                retry_packet = None
                retry_deadlines.clear()
                retry_group_counted = False
                wait_time = self._min_interval - (time.perf_counter() - self._last_send_at)
                if self._stop_event.wait(max(0.0, wait_time)):
                    return
                with self._state_lock:
                    if not self._running:
                        return
                    # Recheck AFTER the rate wait so a newer update wins.
                    packet = self._drain_latest(item)
                    self._attempted += 1
                    repeats = _BURST_REPEATS if packet != self._last_packet_sent else 1
                sent_any = False
                for attempt in range(repeats):
                    with self._state_lock:
                        if not self._running:
                            break
                        # Do not repeat a stale colour once a newer update is
                        # queued. The next loop iteration will send that update.
                        if attempt and not self._queue.empty():
                            break
                        success = self._send_raw(packet)
                        if success:
                            self._datagrams_sent += 1
                            self._last_error = None
                        else:
                            self._send_errors += 1
                    if not success:
                        break
                    sent_any = True
                    if attempt + 1 < repeats and self._stop_event.wait(_BURST_GAP):
                        break
                with self._state_lock:
                    if sent_any:
                        self._last_packet_sent = packet
                        self._sent += 1
                    self._last_send_at = time.perf_counter()
                    # Also recover from local socket failures. A successful
                    # local send is not a delivery acknowledgement, while a
                    # failed one should not wait for the 1.5 s scene refresh.
                    if repeats > 1 or not sent_any:
                        retry_packet = packet
                        retry_deadlines = [self._last_send_at + delay for delay in _SETTLED_RETRY_DELAYS]
                        retry_group_counted = sent_any
        finally:
            try:
                self._socket.close()
            except OSError:
                pass

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
            except OSError as exc:
                self._last_error = str(exc)
                return False

    @property
    def stats(self) -> dict[str, int | str | None]:
        """Successful socket sends, not acknowledgements from the bulb.

        The legacy sent count remains update groups; datagrams_sent includes
        burst and settled-retry copies. Send errors count failed send
        operations, including retries; failed groups do not count as sent.
        """
        with self._state_lock:
            return {
                "ip": self.ip, "sent": self._sent, "dropped": self._dropped,
                "attempted": self._attempted, "datagrams_sent": self._datagrams_sent,
                "send_errors": self._send_errors, "last_error": self._last_error,
            }


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
        self._closed = False
        if prewarm_ips:
            try:
                for ip in prewarm_ips:
                    self._get(ip)
            except Exception:
                self.close()
                raise

    def _get(self, ip: str) -> Optional[BulbUDPSocket]:
        with self._lock:
            if self._closed:
                return None
            sender = self._senders.get(ip)
            if sender is None:
                sender = BulbUDPSocket(ip, max_send_hz=self.max_send_hz)
                self._senders[ip] = sender
            return sender

    def send_payload_map(
        self,
        payloads_by_ip: dict[str, tuple[int, ...]],
        brightness: Optional[int] = None,
    ) -> None:
        for ip, payload in payloads_by_ip.items():
            sender = self._get(ip)
            if sender is not None:
                sender.send(encode_set_pilot_payload(payload, brightness))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            senders = list(self._senders.values())
            self._senders.clear()
        for sender in senders:
            sender.stop()

    @property
    def stats(self) -> list[dict[str, int | str | None]]:
        with self._lock:
            return [sender.stats for sender in self._senders.values()]
