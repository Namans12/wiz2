import json
import socket
import time

from scripts.benchmark_screen_sync import benchmark
from scripts.fake_wiz_bulb_udp import FakeWizBulbUDP, validate_screen_sync_packet
from scripts.measure_control_effects import measure
from src.features.fast_udp_sender import encode_set_pilot_payload


def test_fake_udp_bulb_receives_and_validates_screen_sync_packet():
    receiver = FakeWizBulbUDP(port=0)
    receiver.start()
    try:
        packet = encode_set_pilot_payload((255, 0, 0, 0))
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(packet, receiver.address)
        deadline = time.time() + 2.0
        while not receiver.records and time.time() < deadline:
            time.sleep(0.01)
    finally:
        receiver.stop()

    assert len(receiver.records) == 1
    assert receiver.errors == []
    assert receiver.summary()["packetCount"] == 1


def test_fake_udp_validation_rejects_white_channel_bleed():
    payload = json.loads(encode_set_pilot_payload((255, 0, 0, 32)).decode("utf-8"))

    errors = validate_screen_sync_packet(payload)

    assert any("warm white" in error for error in errors)


def test_screen_sync_benchmark_reports_stage_timings():
    report = benchmark(iterations=3, width=96, height=64, sample_size=24)

    assert report["iterations"] == 3
    assert report["stages"]["processingTotalMs"]["meanMs"] > 0
    assert "extractMs" in report["stages"]


def test_control_effect_measurement_passes_visible_controls():
    report = measure()

    assert report["summary"]["passed"] is True
    assert report["summary"]["visibleControlsChecked"] == 4
    assert report["checks"]["colorPunch"]["metricDelta"] > 0
