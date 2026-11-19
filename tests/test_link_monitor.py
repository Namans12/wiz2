"""Keep-awake link: delivery confirmation, re-sends and per-bulb brightness."""

import json
import socket
import threading
import time

import pytest

from src.core import network
from src.features import fast_udp_sender as udp


class FakeBulb:
    """Loopback WiZ bulb: answers getPilot with its state and applies setPilot."""

    def __init__(self, drop_set_pilots: int = 0, port: int = 0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        # While monotonic() < silent_until the bulb does not read its socket,
        # so datagrams queue like frames buffered by an AP for a sleeping bulb.
        self.silent_until = 0.0
        self.applied = []  # (monotonic time, r, g, b) per applied setPilot
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.state = {"state": False, "r": 0, "g": 0, "b": 0, "c": 0, "w": 0, "dimming": 100}
        self.drop_set_pilots = drop_set_pilots
        self.set_pilots_seen = 0
        self.registrations = 0
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while self._running:
            if time.monotonic() < self.silent_until:
                time.sleep(0.005)
                continue
            try:
                data, sender = self.sock.recvfrom(4096)
            except OSError:
                continue
            message = json.loads(data)
            method = message.get("method")
            if method == "getPilot":
                reply = {"method": "getPilot", "id": message.get("id"), "result": {"rssi": -50, **self.state}}
            elif method == "setPilot":
                self.set_pilots_seen += 1
                if self.set_pilots_seen <= self.drop_set_pilots:
                    continue
                self.state.update(message["params"])
                params = message["params"]
                self.applied.append((time.monotonic(), params.get("r"), params.get("g"), params.get("b")))
                reply = {"method": "setPilot", "result": {"success": True}}
            elif method == "registration":
                self.registrations += 1
                reply = {"method": "registration", "result": {"mac": "a8bb50aabbcc", "success": True}}
            else:
                continue
            self.sock.sendto(json.dumps(reply).encode(), sender)

    def close(self):
        self._running = False
        self._thread.join(timeout=1)
        self.sock.close()


def _wait(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def bulb(monkeypatch):
    fake = FakeBulb()
    monkeypatch.setattr(udp, "WIZ_PORT", fake.port)
    yield fake
    fake.close()


def test_expected_state_and_matching_cover_on_off_and_dimming():
    on = udp.expected_pilot_state((255, 10, 0, 40), 128)
    assert on == (True, 255, 10, 0, 0, 40, 50)
    assert udp.pilot_state_matches({"state": True, "r": 255, "g": 10, "b": 0, "c": 0, "w": 40, "dimming": 50}, on)
    assert not udp.pilot_state_matches({"state": True, "r": 255, "g": 11, "b": 0, "c": 0, "w": 40, "dimming": 50}, on)
    assert not udp.pilot_state_matches({"state": True, "r": 255, "g": 10, "b": 0, "c": 0, "w": 40, "dimming": 80}, on)
    off = udp.expected_pilot_state((0, 0, 0, 0, 0), 200)
    assert off[0] is False
    # An off bulb still reports its last colour; only the power state matters.
    assert udp.pilot_state_matches({"state": False, "r": 9, "g": 9, "b": 9}, off)
    assert not udp.pilot_state_matches({"state": True, "r": 0, "g": 0, "b": 0}, off)
    assert not udp.pilot_state_matches({"success": False}, off)


def test_link_confirms_update_and_cancels_blind_retries(bulb, monkeypatch):
    monkeypatch.setattr(udp, "_SETTLED_RETRY_DELAYS", (0.15, 0.3))
    pool = udp.FastBulbSenderPool(max_send_hz=1000, prewarm_ips=["127.0.0.1"], keep_awake=True)
    try:
        assert _wait(lambda: pool.link_stats[0]["replies"] > 3)
        pool.send_payload_map({"127.0.0.1": (255, 0, 0, 0)}, 128)
        assert _wait(lambda: pool.link_stats[0]["confirmed"] == 1)
        time.sleep(0.4)  # past both settled retries
        link = pool.link_stats[0]
    finally:
        pool.close()
    assert bulb.state["r"] == 255 and bulb.state["dimming"] == 50
    assert link["reachable"] and link["rssi"] == -50
    assert link["confirm_ms"] is not None and link["resends"] == 0
    # One copy while the link is awake; confirmation stopped the later retries.
    assert bulb.set_pilots_seen == 1


def test_link_resends_an_update_the_bulb_never_received(monkeypatch):
    fake = FakeBulb(drop_set_pilots=1)
    monkeypatch.setattr(udp, "WIZ_PORT", fake.port)
    monkeypatch.setattr(udp, "_SETTLED_RETRY_DELAYS", (5.0,))  # only the link can recover
    pool = udp.FastBulbSenderPool(max_send_hz=1000, prewarm_ips=["127.0.0.1"], keep_awake=True)
    try:
        assert _wait(lambda: pool.link_stats[0]["replies"] > 3)
        pool.send_payload_map({"127.0.0.1": (0, 0, 255, 0)}, 255)
        assert _wait(lambda: pool.link_stats[0]["confirmed"] == 1)
        link = pool.link_stats[0]
    finally:
        pool.close()
        fake.close()
    assert fake.state["b"] == 255
    assert link["resends"] >= 1


def test_confirmed_colour_recovers_after_bulb_state_changes_without_new_frame(bulb, monkeypatch):
    monkeypatch.setattr(udp, "_SETTLED_RETRY_DELAYS", ())
    pool = udp.FastBulbSenderPool(max_send_hz=1000, prewarm_ips=["127.0.0.1"], keep_awake=True)
    try:
        assert _wait(lambda: pool.link_stats[0]["replies"] > 3)
        pool.send_payload_map({"127.0.0.1": (0, 0, 255, 0)}, 128)
        assert _wait(lambda: pool.link_stats[0]["confirmed"] == 1)
        for recovery in (1, 2):
            bulb.state = {**bulb.state, "state": False, "b": 0, "dimming": 100}
            assert _wait(lambda: pool.link_stats[0]["recoveries"] == recovery)
            assert bulb.state["state"] and bulb.state["b"] == 255
            assert bulb.state["dimming"] == 50
        link = pool.link_stats[0]
        assert link["confirmed"] == link["updates"] == 1
        assert link["stale_events"] == 2 and link["state_confirmed"]
    finally:
        pool.close()


def test_confirmation_ignores_transient_mismatch_and_bounds_persistent_retries(monkeypatch):
    # Control the clock and responses without starting a network worker.
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    retries = []
    monitor = udp.BulbLinkMonitor("synthetic", resend=lambda packet: retries.append(packet))
    state = udp.expected_pilot_state((255, 0, 0, 0), 255)
    monitor.expect(b"red", state)
    start = monitor._expected.created_at
    red = {"state": True, "r": 255, "dimming": 100}
    wrong = {"state": False}
    monitor._check(red, start + 0.01)
    monitor._check({"success": False}, start + 0.02)
    monitor._check(wrong, start + 0.03)
    monitor._check(red, start + 0.04)
    assert not retries and monitor.stats["state_confirmed"]
    monitor._check(wrong, start + 0.10)
    for offset in (0.15, 0.20, 0.25, 0.30):
        monitor._check(wrong, start + offset)
    assert len(retries) == udp._CONFIRM_MAX_RESENDS
    for offset in (0.4, 0.6, 1.0, 1.29):
        monitor._check(wrong, start + offset)
    assert len(retries) == udp._CONFIRM_MAX_RESENDS
    monitor._check(wrong, start + 1.31)
    assert len(retries) == udp._CONFIRM_MAX_RESENDS + 1
    monitor._check(red, start + 1.32)
    assert monitor.stats["recoveries"] == 1
    assert monitor.stats["confirmed"] == monitor.stats["updates"] == 1
    monitor.expect(b"blue", udp.expected_pilot_state((0, 0, 255, 0), 255))
    monitor._check(red, time.perf_counter() + 0.1)
    assert retries[-1] == b"blue"


def test_initial_delivery_recovers_after_first_retry_budget_is_exhausted(monkeypatch):
    fake = FakeBulb(drop_set_pilots=6)
    monkeypatch.setattr(udp, "WIZ_PORT", fake.port)
    monkeypatch.setattr(udp, "_SETTLED_RETRY_DELAYS", ())
    monkeypatch.setattr(udp, "_CONFIRM_RETRY_COOLDOWN", 0.15)
    pool = udp.FastBulbSenderPool(max_send_hz=1000, prewarm_ips=["127.0.0.1"], keep_awake=True)
    try:
        assert _wait(lambda: pool.link_stats[0]["replies"] > 3)
        pool.send_payload_map({"127.0.0.1": (0, 255, 0, 0)}, 255)
        assert _wait(lambda: pool.link_stats[0]["confirmed"] == 1)
        assert pool.link_stats[0]["resends"] > udp._CONFIRM_MAX_RESENDS
        assert fake.state["g"] == 255
    finally:
        pool.close()
        fake.close()


def test_resend_never_requeues_a_colour_replaced_by_a_newer_one(monkeypatch):
    class Socket:
        def __init__(self):
            self.packets = []

        def send(self, packet):
            self.packets.append(packet)

        def close(self):
            pass

    sock = Socket()
    monkeypatch.setattr(udp.BulbUDPSocket, "_connect", lambda self: sock)
    monkeypatch.setattr(udp, "_SETTLED_RETRY_DELAYS", ())
    sender = udp.BulbUDPSocket("synthetic", max_send_hz=1000)
    try:
        sender.send(b"old")
        sender.send(b"new")
        assert not sender.resend_if_latest(b"old")
        assert sender.resend_if_latest(b"new")
        assert _wait(lambda: sock.packets.count(b"new") >= 2)
    finally:
        sender.stop()
    assert b"old" not in sock.packets[sock.packets.index(b"new"):]


def test_pool_accepts_per_bulb_brightness(monkeypatch):
    sent = {}

    class Sender:
        def __init__(self, ip, max_send_hz=28):
            self.ip = ip

        def send(self, packet):
            sent[self.ip] = json.loads(packet)["params"]

        def stop(self):
            pass

    monkeypatch.setattr(udp, "BulbUDPSocket", Sender)
    pool = udp.FastBulbSenderPool()
    pool.send_payload_map({"a": (255, 0, 0, 0), "b": (0, 0, 255, 0)}, {"a": 255, "b": 64})
    pool.close()
    assert sent["a"]["dimming"] == 100 and sent["b"]["dimming"] == 25
    assert pool.link_stats == []


def test_discovery_broadcasts_from_each_interface_and_reports_mac(monkeypatch):
    fake = FakeBulb()
    monkeypatch.setattr(network, "WIZ_PORT", fake.port)
    monkeypatch.setattr(network, "local_ipv4_addresses", lambda: ["127.0.0.1"])
    try:
        found = network.discover_wiz_bulbs(["127.0.0.1"], wait_time=0.4)
    finally:
        fake.close()
    assert found == [{"ip": "127.0.0.1", "mac": "a8bb50aabbcc"}]
    assert fake.registrations >= 1


def test_local_addresses_skip_loopback_and_link_local(monkeypatch):
    monkeypatch.setattr(network.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, (ip, 0)) for ip in ("127.0.0.1", "169.254.3.4", "10.0.0.7")],
    )
    addresses = network.local_ipv4_addresses()
    assert "10.0.0.7" in addresses
    assert not any(address.startswith(("127.", "169.254.")) for address in addresses)


def test_link_keeps_reply_outage_history_after_connection_recovers(bulb, monkeypatch):
    monitor = udp.BulbLinkMonitor("127.0.0.1")
    try:
        assert _wait(lambda: monitor.stats["replies"] > 3)
        with monitor._lock:
            monitor._last_reply_at -= 1.25
        assert _wait(lambda: monitor.stats["reply_gaps_over_1s"] == 1)
        assert monitor.stats["max_reply_gap_ms"] >= 1250
        monkeypatch.setattr(udp, "_LINK_IDLE_PAUSE", 0)
        assert _wait(lambda: monitor.stats["paused"])
        assert monitor.stats["current_reply_gap_ms"] is None
        assert monitor.stats["reply_gaps_over_1s"] == 1
    finally:
        monitor.stop()


def test_colour_sent_during_silence_is_queued_and_applied_when_bulb_returns(bulb):
    pool = udp.FastBulbSenderPool(max_send_hz=1000, prewarm_ips=["127.0.0.1"], keep_awake=True)
    try:
        assert _wait(lambda: pool.link_stats[0]["replies"] > 3)
        silence = 2.5
        returns_at = time.monotonic() + silence
        bulb.silent_until = returns_at
        time.sleep(0.1)
        pool.send_payload_map({"127.0.0.1": (255, 0, 0, 0)}, 255)
        time.sleep(0.6)
        pool.send_payload_map({"127.0.0.1": (0, 0, 255, 0)}, 255)
        assert _wait(lambda: bulb.state["b"] == 255, timeout=silence + 2.0)
        link = pool.link_stats[0]
    finally:
        pool.close()
    first_blue = next(stamp for stamp, _, _, b in bulb.applied if b == 255)
    delay = first_blue - returns_at
    print(f"applied {delay * 1000:.0f} ms after the bulb returned; "
          f"timeout resends {link['timeout_resends']}")
    assert delay <= 0.25
    colours = [(r, b) for _, r, _, b in bulb.applied]
    blue_at = colours.index((0, 255))
    assert all(c != (255, 0) for c in colours[blue_at:]), "stale colour applied after the newer one"
    assert bulb.state["r"] == 0 and bulb.state["b"] == 255
    # Rate-limited (one per 250 ms) and bounded, yet clearly more than the
    # sender's own ~0.45 s of blind retries.
    assert 3 <= link["timeout_resends"] <= silence / udp._OUTAGE_RESEND_INTERVAL + 2
    assert link["timeout_resends"] <= udp._OUTAGE_MAX_RESENDS


def test_closed_port_icmp_reset_does_not_stall_the_chain(monkeypatch):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens: sends draw ICMP port-unreachable
    monkeypatch.setattr(udp, "WIZ_PORT", port)
    monitor = udp.BulbLinkMonitor("127.0.0.1")
    fake = None
    try:
        time.sleep(1.0)
        before = monitor.stats
        # The old 0.5 s close/reopen stall allowed about 2 queries per second.
        assert before["queries"] >= 7
        assert before["reconnects"] == 0
        assert before["replies"] == 0
        fake = FakeBulb(port=port)
        started = time.monotonic()
        assert _wait(lambda: monitor.stats["replies"] >= 1, timeout=1.0)
        recovered_in = time.monotonic() - started
        print(f"{before['queries']} queries/s against a closed port "
              f"({before['socket_resets']} resets); first reply {recovered_in * 1000:.0f} ms after the bulb appeared")
        assert recovered_in <= 0.35
        assert monitor.stats["reconnects"] == 0
    finally:
        monitor.stop()
        if fake is not None:
            fake.close()


def test_persistent_socket_errors_back_off_and_reset_on_success(monkeypatch):
    attempts = []

    class BrokenSocket:
        def settimeout(self, value):
            pass

        def send(self, packet):
            raise OSError("network unreachable")

        def close(self):
            pass

    def connect(self):
        attempts.append(time.monotonic())
        return BrokenSocket()

    monkeypatch.setattr(udp.BulbLinkMonitor, "_connect", connect)
    monitor = udp.BulbLinkMonitor("synthetic")
    try:
        assert _wait(lambda: len(attempts) >= 6, timeout=3.0)
    finally:
        monitor.stop()
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    assert gaps[0] < 0.2 and gaps[1] >= gaps[0] * 1.5  # starts short, doubles
    assert max(gaps) <= udp._LINK_ERROR_BACKOFF_MAX + 0.15


def test_silence_slows_the_chain_and_first_reply_restores_it(monkeypatch):
    fake = FakeBulb()
    monkeypatch.setattr(udp, "WIZ_PORT", fake.port)
    monkeypatch.setattr(udp, "_LINK_REPLY_TIMEOUT", 0.05)
    monkeypatch.setattr(udp, "_LINK_SLOW_AFTER", 0.4)
    monkeypatch.setattr(udp, "_LINK_SLOW_INTERVAL", 0.5)
    fake.silent_until = time.monotonic() + 2.4
    monitor = udp.BulbLinkMonitor("127.0.0.1")
    try:
        time.sleep(0.3)
        fast = monitor.stats["queries"]
        time.sleep(0.5)
        mid = monitor.stats["queries"]
        time.sleep(1.0)
        slow = monitor.stats["queries"] - mid
        silent = monitor.stats["silent_for_s"]
        print(f"fast {fast / 0.3:.0f} queries/s, silent {slow / 1.0:.1f} queries/s")
        assert fast >= 4 and slow <= 4 and silent >= 1.5
        assert _wait(lambda: monitor.stats["replies"] > 20, timeout=2.5)
        assert monitor.stats["silent_for_s"] < 0.5
    finally:
        monitor.stop()
        fake.close()


def test_silence_is_measured_from_resume_not_from_a_pre_pause_reply(bulb, monkeypatch):
    monkeypatch.setattr(udp, "_LINK_REPLY_TIMEOUT", 0.05)
    monkeypatch.setattr(udp, "_LINK_SLOW_AFTER", 5.0)
    monitor = udp.BulbLinkMonitor("127.0.0.1")
    try:
        assert _wait(lambda: monitor.stats["replies"] > 3)
        monkeypatch.setattr(udp, "_LINK_IDLE_PAUSE", 0)
        assert _wait(lambda: monitor.stats["paused"])
        with monitor._lock:
            monitor._last_reply_at = time.perf_counter() - 200.0  # stale pre-pause reply
        bulb.silent_until = time.monotonic() + 1.0  # first queries after resume time out
        monkeypatch.setattr(udp, "_LINK_IDLE_PAUSE", 120.0)
        monitor.expect(b"x", udp.expected_pilot_state((1, 2, 3, 4), 255))
        before = monitor.stats["queries"]
        time.sleep(0.4)
        stats = monitor.stats
    finally:
        monitor.stop()
    assert stats["silent_for_s"] < 1.0
    assert stats["queries"] - before >= 4  # still 20/s, not dropped to 1/s

