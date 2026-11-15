"""Loopback checks for the opt-in live test's measurement and cleanup helpers."""

import time

from scripts.validate_live_link import PilotClient, summary
from src.features.fast_udp_sender import expected_pilot_state
from tests.test_link_monitor import FakeBulb


def test_live_observer_checks_dimming_and_restores_prior_state():
    bulb = FakeBulb()
    client = PilotClient("127.0.0.1", bulb.port)
    try:
        prior = client.ask()
        assert prior["state"] is False
        client.ask("setPilot", {"state": True, "r": 255, "dimming": 25})
        expected = expected_pilot_state((255, 0, 0, 0), 64)
        result = client.observe(expected, time.perf_counter())
        assert result["matched"]
        wrong = client.observe(expected_pilot_state((255, 0, 0, 0), 255), time.perf_counter(), timeout=0.1)
        assert not wrong["matched"]
        assert client.restore(prior)
        assert bulb.state["state"] is False
    finally:
        client.sock.close()
        bulb.close()


def test_restore_does_not_report_success_when_bulb_ignores_commands():
    bulb = FakeBulb(drop_set_pilots=20)
    client = PilotClient("127.0.0.1", bulb.port)
    try:
        assert not client.restore({"state": True, "r": 255, "dimming": 100})
    finally:
        client.sock.close()
        bulb.close()


def test_latency_summary_keeps_unmatched_cases_in_denominator():
    result = summary([{"matched": True, "ms": 20}, {"matched": False, "ms": None}])
    assert result["count"] == 2 and result["matched"] == 1
    assert result["median_ms"] == 20
    assert summary([])["median_ms"] is None
