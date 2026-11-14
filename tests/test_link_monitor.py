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

    def __init__(self, drop_set_pilots: int = 0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
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
