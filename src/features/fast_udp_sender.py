"""Fire-and-forget WiZ UDP sender for low-latency screen sync."""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from collections import deque
from typing import Callable, Mapping, Optional, Sequence

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

# ── Keep-awake link ─────────────────────────────────────────────────────────
# Measured on a WiZ ESP25 bulb (2026-11-13, read-only status queries): the
# bulb's radio listens roughly once every 205 ms (two Wi-Fi beacon periods),
# so a datagram normally waits 0-205 ms in the router before the bulb sees it,
# and about 5% of those queued datagrams got no answer. For about 5 ms after
# the bulb transmits a reply it is still listening: a query sent in that
# window was answered in ~6 ms. Chaining queries reply-to-reply held that
# state for a 5-minute soak. BulbLinkMonitor runs that chain during sync so a
# colour update lands while the bulb is listening, and uses the replies (which
# report the colour the bulb is showing) as the delivery confirmation UDP
# itself does not provide.
_LINK_QUERY = b'{"method":"getPilot","id":%d,"params":{}}'
_LINK_REPLY_TIMEOUT = 0.3
# A reply this recent means the bulb is still in its listening window.
_LINK_HOT_WINDOW = 0.04
_LINK_FAST_RTT_MS = 25.0
# Stop the chain when nothing has been sent for this long (paused video); the
# next colour update restarts it.
_LINK_IDLE_PAUSE = 120.0
# Re-send an update the bulb does not report showing, at most this many times.
_CONFIRM_RESEND_AFTER = 0.035
_CONFIRM_MAX_RESENDS = 4


def _clamp_channel(value: int) -> int:
    return max(0, min(255, int(value)))


def _normalize_payload(
    payload: tuple[int, ...],
    brightness: Optional[int],
) -> tuple[tuple[int, ...], Optional[int]]:
    if len(payload) not in {4, 5}:
        raise ValueError("WiZ payload must be RGBW or RGBWW")

    # Preserve the mapper's channel values: cache reuse must never change hue.
    channels = tuple(_clamp_channel(value) for value in payload)
    dimming = None if brightness is None else max(10, min(100, round(int(brightness) / 255 * 100)))
    return channels, dimming


def expected_pilot_state(
    payload: tuple[int, ...],
    brightness: Optional[int] = None,
) -> tuple:
    """The state a bulb should report after accepting this payload.

    Returns ``(state, r, g, b, c, w, dimming)``; an all-zero payload turns the
    bulb off, after which only ``state`` is meaningful.
    """

    channels, dimming = _normalize_payload(payload, brightness)
    if not any(channels):
        return (False, 0, 0, 0, 0, 0, None)
    if len(channels) == 5:
        r, g, b, c, w = channels
    else:
        r, g, b, w = channels
        c = 0
    return (True, r, g, b, c, w, dimming)


def pilot_state_matches(result: Mapping, expected: tuple) -> bool:
    """Whether a getPilot result shows the state from expected_pilot_state()."""

    state, r, g, b, c, w, dimming = expected
    if bool(result.get("state")) != state:
        return False
    if not state:
        return True
    try:
        reported = tuple(int(result.get(name, 0)) for name in ("r", "g", "b", "c", "w"))
        if reported != (r, g, b, c, w):
            return False
        return dimming is None or abs(int(result.get("dimming", dimming)) - dimming) <= 1
    except (TypeError, ValueError):
        return False


def encode_set_pilot_payload(
    payload: tuple[int, ...],
    brightness: Optional[int] = None,
) -> bytes:
    """Encode an RGBW/RGBWW WiZ setPilot packet with explicit white channels."""

    channels, dimming = _normalize_payload(payload, brightness)
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
    _RESEND = object()

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
        self._latest_packet: bytes | None = None
        self._confirmed_packet: bytes | None = None
        # Set by the pool when a BulbLinkMonitor holds this bulb awake.
        self.link_hot: Optional[Callable[[], bool]] = None
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
            if packet != self._latest_packet:
                self._confirmed_packet = None
            self._latest_packet = packet
            self._queue.put(packet)
            return True

    def resend_if_latest(self, packet: bytes) -> bool:
        """Send one more copy of ``packet`` unless a newer update replaced it.

        Used by the link monitor when the bulb does not report the state it
        was sent. Checked under the same lock as send(), so a stale colour can
        never be re-queued behind a newer one.
        """
        with self._state_lock:
            if not self._running or packet != self._latest_packet:
                return False
            self._queue.put((self._RESEND, packet))
            return True

    def mark_confirmed(self, packet: bytes) -> None:
        """The bulb reports this update; its remaining blind retries can stop."""
        with self._state_lock:
            if packet == self._latest_packet:
                self._confirmed_packet = packet

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
                        if retry_packet == self._confirmed_packet:
                            # The bulb already reports this state.
                            retry_deadlines.clear()
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
                if isinstance(item, tuple) and item and item[0] is self._RESEND:
                    # One extra copy of the current update; pending settled
                    # retries stay scheduled as the fallback.
                    with self._state_lock:
                        if self._running and item[1] == self._latest_packet and self._queue.empty():
                            if self._send_raw(item[1]):
                                self._datagrams_sent += 1
                                self._last_error = None
                            else:
                                self._send_errors += 1
                    continue
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
                    changed = packet != self._last_packet_sent
                    # A burst only insures against loss while the datagram
                    # waits for the bulb to listen. With the link held awake
                    # the bulb confirms what it shows, so one copy is enough.
                    hot = self.link_hot is not None and self.link_hot()
                    repeats = _BURST_REPEATS if changed and not hot else 1
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
                    if changed or not sent_any:
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


class _Expectation:
    __slots__ = ("packet", "state", "created_at", "last_attempt_at", "resends", "confirmed")

    def __init__(self, packet: bytes, state: tuple, now: float):
        self.packet = packet
        self.state = state
        self.created_at = now
        self.last_attempt_at = now
        self.resends = 0
        self.confirmed = False


class BulbLinkMonitor:
    """Hold one bulb's radio awake and confirm the colour it is showing.

    Runs a reply-clocked chain of read-only ``getPilot`` queries: the next
    query leaves the moment the previous reply arrives, which is inside the
    few milliseconds the bulb keeps listening after it transmits. While the
    chain holds, colour updates from the sender are processed in a few
    milliseconds instead of waiting for the bulb's next listen interval.

    Each reply also reports the bulb's current channels, so the monitor knows
    whether the latest update arrived: it asks the sender for one more copy
    when it did not, and cancels the sender's blind retries when it did.
    """

    def __init__(
        self,
        ip: str,
        resend: Optional[Callable[[bytes], bool]] = None,
        confirmed: Optional[Callable[[bytes], None]] = None,
    ):
        self.ip = ip
        self._resend = resend
        self._confirmed = confirmed
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._expected: Optional[_Expectation] = None
        self._last_activity = time.perf_counter()
        self._last_reply_at = 0.0
        self._rtts: deque = deque(maxlen=240)
        self._confirm_ms: deque = deque(maxlen=60)
        self._queries = 0
        self._replies = 0
        self._timeouts = 0
        self._updates = 0
        self._confirmed_count = 0
        self._resends = 0
        self._rssi: Optional[int] = None
        self._paused = False
        self._last_error: Optional[str] = None
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"WiZLink-{ip}")
        self._thread.start()

    def expect(self, packet: bytes, state: tuple) -> None:
        """Register the update the sender is about to deliver."""
        now = time.perf_counter()
        with self._lock:
            self._last_activity = now
            current = self._expected
            if current is None or current.packet != packet:
                self._expected = _Expectation(packet, state, now)
                self._updates += 1
        self._wake_event.set()

    def is_hot(self) -> bool:
        """True while the bulb answered recently enough to still be listening."""
        return time.perf_counter() - self._last_reply_at < _LINK_HOT_WINDOW

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(_LINK_REPLY_TIMEOUT)
        try:
            sock.connect((self.ip, WIZ_PORT))
        except OSError:
            sock.close()
            raise
        return sock

    def _await_reply(self, sock: socket.socket, sequence: int) -> Optional[dict]:
        deadline = time.perf_counter() + _LINK_REPLY_TIMEOUT
        while not self._stop_event.is_set():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            try:
                data = sock.recv(2048)
            except TimeoutError:
                return None
            try:
                reply = json.loads(data)
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get("id") == sequence:
                result = reply.get("result")
                return result if isinstance(result, dict) else {}
        return None

    def _run(self) -> None:
        sock: Optional[socket.socket] = None
        sequence = 0
        try:
            while not self._stop_event.is_set():
                if time.perf_counter() - self._last_activity > _LINK_IDLE_PAUSE:
                    self._paused = True
                    self._wake_event.clear()
                    if time.perf_counter() - self._last_activity > _LINK_IDLE_PAUSE:
                        self._wake_event.wait(0.5)
                    continue
                self._paused = False
                try:
                    if sock is None:
                        sock = self._connect()
                    sequence = sequence % 1_000_000 + 1
                    sent_at = time.perf_counter()
                    sock.send(_LINK_QUERY % sequence)
                    result = self._await_reply(sock, sequence)
                except OSError as exc:
                    self._last_error = str(exc)
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass
                        sock = None
                    with self._lock:
                        self._queries += 1
                        self._timeouts += 1
                    if self._stop_event.wait(0.5):
                        return
                    continue
                now = time.perf_counter()
                with self._lock:
                    self._queries += 1
                    if result is None:
                        self._timeouts += 1
                    else:
                        self._replies += 1
                        self._last_reply_at = now
                        self._last_error = None
                        self._rtts.append((now - sent_at) * 1000.0)
                        rssi = result.get("rssi")
                        if isinstance(rssi, int):
                            self._rssi = rssi
                if result:
                    self._check(result, now)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _check(self, result: dict, now: float) -> None:
        action = None
        with self._lock:
            expected = self._expected
            if expected is None or expected.confirmed:
                return
            if pilot_state_matches(result, expected.state):
                expected.confirmed = True
                self._confirmed_count += 1
                self._confirm_ms.append((now - expected.created_at) * 1000.0)
                action = self._confirmed
            elif (
                expected.resends < _CONFIRM_MAX_RESENDS
                and now - expected.last_attempt_at >= _CONFIRM_RESEND_AFTER
            ):
                expected.resends += 1
                expected.last_attempt_at = now
                self._resends += 1
                action = self._resend
            packet = expected.packet
        # Callbacks take the sender's lock; never call them holding ours.
        if action is not None:
            action(packet)

    @property
    def stats(self) -> dict[str, object]:
        """Round-trip and confirmation figures for diagnostics."""
        now = time.perf_counter()
        with self._lock:
            rtts = sorted(self._rtts)
            confirms = sorted(self._confirm_ms)
            return {
                "ip": self.ip,
                "reachable": bool(self._last_reply_at) and now - self._last_reply_at < 1.5,
                "paused": self._paused,
                "queries": self._queries,
                "replies": self._replies,
                "timeouts": self._timeouts,
                "rtt_ms": rtts[len(rtts) // 2] if rtts else None,
                "rtt_p95_ms": rtts[min(len(rtts) - 1, int(len(rtts) * 0.95))] if rtts else None,
                "awake_pct": (
                    100.0 * sum(1 for value in rtts if value < _LINK_FAST_RTT_MS) / len(rtts)
                ) if rtts else None,
                "rssi": self._rssi,
                "updates": self._updates,
                "confirmed": self._confirmed_count,
                "resends": self._resends,
                "confirm_ms": confirms[len(confirms) // 2] if confirms else None,
                "confirm_p95_ms": (
                    confirms[min(len(confirms) - 1, int(len(confirms) * 0.95))] if confirms else None
                ),
                "last_error": self._last_error,
            }


class FastBulbSenderPool:
    """Thread-safe pool of latest-wins UDP senders."""

    def __init__(
        self,
        max_send_hz: float = DEFAULT_MAX_SEND_HZ,
        prewarm_ips: Optional[Sequence[str]] = None,
        keep_awake: bool = False,
    ):
        self.max_send_hz = max_send_hz
        self.keep_awake = bool(keep_awake)
        self._senders: dict[str, BulbUDPSocket] = {}
        self._monitors: dict[str, BulbLinkMonitor] = {}
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
                if self.keep_awake:
                    monitor = BulbLinkMonitor(
                        ip,
                        resend=sender.resend_if_latest,
                        confirmed=sender.mark_confirmed,
                    )
                    sender.link_hot = monitor.is_hot
                    self._monitors[ip] = monitor
            return sender

    def send_payload_map(
        self,
        payloads_by_ip: dict[str, tuple[int, ...]],
        brightness: Optional[int] | Mapping[str, int] = None,
    ) -> None:
        """Queue one payload per bulb; brightness may be shared or per bulb."""
        for ip, payload in payloads_by_ip.items():
            sender = self._get(ip)
            if sender is None:
                continue
            level = brightness.get(ip) if isinstance(brightness, Mapping) else brightness
            packet = encode_set_pilot_payload(payload, level)
            monitor = self._monitors.get(ip)
            if monitor is not None:
                # Register first: the monitor may only re-send what the
                # sender still considers the latest update.
                monitor.expect(packet, expected_pilot_state(payload, level))
            sender.send(packet)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            senders = list(self._senders.values())
            monitors = list(self._monitors.values())
            self._senders.clear()
            self._monitors.clear()
        for monitor in monitors:
            monitor.stop()
        for sender in senders:
            sender.stop()

    @property
    def link_stats(self) -> list[dict[str, object]]:
        """Per-bulb link figures; empty unless keep_awake is enabled."""
        with self._lock:
            monitors = list(self._monitors.values())
        return [monitor.stats for monitor in monitors]

    @property
    def stats(self) -> list[dict[str, int | str | None]]:
        with self._lock:
            return [sender.stats for sender in self._senders.values()]
