"""Runtime regressions using synthetic frames and fake sockets, never devices."""

import asyncio
import json
import threading

import numpy as np
import pytest

from src.core.async_runtime import BackgroundAsyncLoop
from src.features import fast_udp_sender as udp
from src.features import screen_sync_v2 as sync_module
from src.features.screen_sync_v2 import OptimizedCaptureConfig, OptimizedScreenSync


def test_failed_microphone_start_closes_stream_and_resets_state(monkeypatch):
    from src.features import clap_detector
    from unittest.mock import Mock
    stream = Mock()
    stream.start.side_effect = RuntimeError("permission denied")
    monkeypatch.setattr(clap_detector.sd, "InputStream", lambda **kwargs: stream)
    detector = clap_detector.ClapDetector(lambda: None)
    with pytest.raises(RuntimeError, match="permission denied"):
        detector.start()
    assert not detector.is_running
    assert detector._stream is None
    stream.close.assert_called_once()


@pytest.mark.parametrize("reverse", [False, True])
def test_packet_cache_keeps_rgbw_brightness_separate_from_rgbcw(reverse):
    udp._ENCODE_CACHE.clear()
    requests = [((8, 16, 24, 32), 102), ((8, 16, 24, 32, 40), None)]
    if reverse:
        requests.reverse()
    decoded = {
        len(channels): json.loads(udp.encode_set_pilot_payload(channels, brightness))["params"]
        for channels, brightness in requests
    }
    assert decoded[4]["c"] == 0
    assert decoded[4]["w"] == 32
    assert decoded[4]["dimming"] == 40
    assert decoded[5]["c"] == 32
    assert decoded[5]["w"] == 40
    assert "dimming" not in decoded[5]


def test_shutdown_cancels_tasks_and_runs_finally_without_separate_cancel():
    runner = BackgroundAsyncLoop()
    entered, cleaned = threading.Event(), threading.Event()

    async def pending():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    future = runner.submit(pending())
    assert entered.wait(2)
    runner.shutdown()
    runner.shutdown()
    assert cleaned.is_set()
    assert future.cancelled()
    assert runner._loop.is_closed()
    assert not runner._thread.is_alive()


def test_rejected_coroutine_is_closed_after_shutdown():
    runner = BackgroundAsyncLoop()
    runner.shutdown()
    coro = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="closed"):
        runner.submit(coro)
    assert coro.cr_frame is None


def test_shutdown_from_loop_thread_does_not_join_itself():
    runner = BackgroundAsyncLoop()

    async def stop():
        runner.shutdown()

    runner.submit(stop()).result(timeout=2)
    runner.shutdown()
    assert runner._loop.is_closed()


class FakeSocket:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.packets = []
        self.completed = threading.Event()
        self.closed = False

    def send(self, packet):
        if self.fail:
            self.completed.set()
            raise OSError("synthetic send failure")
        self.packets.append(packet)
        if len(self.packets) == 3:
            self.completed.set()

    def close(self):
        self.closed = True


def test_sender_counts_successful_groups_and_actual_burst_datagrams(monkeypatch):
    socket = FakeSocket()
    monkeypatch.setattr(udp.BulbUDPSocket, "_connect", lambda self: socket)
    sender = udp.BulbUDPSocket("synthetic")
    try:
        assert sender.send(b"color")
        assert socket.completed.wait(2)
    finally:
        sender.stop()
    stats = sender.stats
    assert stats["sent"] == stats["attempted"] == 1
    assert stats["datagrams_sent"] == 3
    assert stats["send_errors"] == 0
    assert not sender.send(b"after-close")
    sender.stop()
    assert socket.closed
    assert not sender._thread.is_alive()


def test_failed_send_group_is_not_reported_as_sent(monkeypatch):
    socket = FakeSocket(fail=True)
    monkeypatch.setattr(udp.BulbUDPSocket, "_connect", lambda self: socket)
    sender = udp.BulbUDPSocket("synthetic")
    try:
        sender.send(b"color")
        assert socket.completed.wait(2)
    finally:
        sender.stop()
    stats = sender.stats
    assert stats["sent"] == stats["datagrams_sent"] == 0
    assert stats["attempted"] == stats["send_errors"] == 1
    assert stats["last_error"] == "synthetic send failure"


def test_closed_pool_cannot_recreate_sender_when_producer_races_close(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    created = []

    class Sender:
        def __init__(self, *args, **kwargs):
            created.append(self)
            entered.set()
            assert release.wait(2)

        def send(self, packet):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(udp, "BulbUDPSocket", Sender)
    pool = udp.FastBulbSenderPool()
    producer = threading.Thread(target=pool.send_payload_map, args=({"synthetic": (8, 0, 0, 0)},))
    producer.start()
    assert entered.wait(2)
    closer = threading.Thread(target=pool.close)
    closer.start()
    release.set()
    producer.join(2)
    closer.join(2)
    assert not producer.is_alive() and not closer.is_alive()
    pool.send_payload_map({"synthetic": (16, 0, 0, 0)})
    pool.close()
    assert len(created) == 1
    assert pool.stats == []


def test_brightness_change_updates_steady_zone_when_other_zone_changes_hue(monkeypatch):
    updates = []
    engine = OptimizedScreenSync(
        lambda colors: record(colors),
        OptimizedCaptureConfig(mode="zones", active_regions=("left", "right"),
                               use_gpu=False, parallel_regions=False, adaptive_fps=False),
    )
    frames = iter([
        ({"left": (255, 0, 0), "right": (0, 0, 255)}, 100),
        ({"left": (0, 255, 0), "right": (0, 0, 255)}, 150),
    ])

    def extract(_frame):
        colors, engine._output_brightness = next(frames)
        return colors

    def record(colors):
        updates.append((dict(colors), engine.output_brightness))
        if len(updates) == 2:
            engine._running = False

    monkeypatch.setattr(engine, "_capture_frame", lambda: np.zeros((8, 8, 3), dtype=np.uint8))
    monkeypatch.setattr(engine, "_extract_colors", extract)
    monkeypatch.setattr(engine._motion_detector, "update", lambda frame: 0.0)
    monkeypatch.setattr(sync_module.time, "sleep", lambda duration: None)
    engine._running = True
    engine._sync_loop()
    assert updates[1][1] == 150
    assert updates[1][0]["right"] == (0, 0, 255)
    assert set(updates[1][0]) == {"left", "right"}
    snapshot = engine.debug_snapshot
    assert snapshot["timing_samples"] == 2
    assert snapshot["capture_time_ms"] >= 0
    assert snapshot["processing_time_ms"] >= 0


@pytest.mark.parametrize("mode", ["follow", "constant"])
def test_true_black_holds_until_deadline_and_constant_never_turns_off(monkeypatch, mode):
    engine = OptimizedScreenSync(lambda colors: None, OptimizedCaptureConfig(
        use_gpu=False, parallel_regions=False, brightness_mode=mode))
    now = [10.0]
    monkeypatch.setattr(sync_module.time, "perf_counter", lambda: now[0])
    black = np.zeros((16, 16, 3), dtype=np.uint8)
    assert engine._extract_colors(black) == {}
    now[0] += 0.69
    assert engine._extract_colors(black) == {}
    now[0] += 0.02
    assert engine._extract_colors(black) == ({"all": (0, 0, 0)} if mode == "follow" else {})


def test_gpu_failure_never_replays_cached_pixels_and_releases_after_three_errors():
    class Camera:
        released = False

        def grab(self):
            raise OSError("acquisition failed")

        def release(self):
            self.released = True

    gpu = sync_module.GPUCaptureManager()
    camera = Camera()
    gpu._camera = camera
    gpu._last_frame = np.ones((8, 8, 3), dtype=np.uint8)
    for _ in range(3):
        assert gpu.grab() is None
    assert not gpu.is_active
    assert camera.released
    assert gpu.last_error == "acquisition failed"
def test_mss_is_closed_on_its_own_capture_thread():
    import threading
    from src.features.screen_sync_v2 import OptimizedCaptureConfig, OptimizedScreenSync

    engine = OptimizedScreenSync(lambda _: None, OptimizedCaptureConfig(use_gpu=False))
    started = threading.Event()
    stopped = threading.Event()
    ownership = {}

    class ThreadBoundCapture:
        def __init__(self):
            ownership["created"] = threading.get_ident()

        def close(self):
            ownership["closed"] = threading.get_ident()

    def work():
        engine._mss_instance = ThreadBoundCapture()
        started.set()
        stopped.wait(3)

    engine._sync_loop_inner = work
    engine._running = True
    engine._thread = threading.Thread(target=engine._sync_loop)
    engine._thread.start()
    assert started.wait(2)
    stopped.set()
    engine.stop()
    assert ownership["created"] == ownership["closed"]
    assert engine._mss_instance is None


@pytest.mark.parametrize("cpu_brightness,expected", [(0, "dxcam"), (180, "mss")])
def test_black_gpu_falls_back_only_if_cpu_sees_bright_screen(monkeypatch, cpu_brightness, expected):
    engine = OptimizedScreenSync(lambda _: None, OptimizedCaptureConfig(use_gpu=True))
    gpu = type("FakeGPU", (), {"is_active": True, "last_error": None,
                               "grab": lambda self: np.zeros((24, 24, 3), dtype=np.uint8)})()
    engine._gpu_capture = gpu
    engine._running = True
    monkeypatch.setattr(engine, "_capture_mss_frame", lambda: np.full((24, 24, 3), cpu_brightness, dtype=np.uint8))
    now = [10.0]
    monkeypatch.setattr(sync_module.time, "perf_counter", lambda: now[0])
    engine._capture_frame()
    now[0] += 1.1
    output = engine._capture_frame()
    assert engine._capture_method == expected
    assert int(output.mean()) == (cpu_brightness if expected == "mss" else 0)
    if expected == "mss":
        assert engine._rejected_gpu_capture is gpu
        assert "GPU returned black" in engine._capture_status
        gpu.stop = lambda: setattr(gpu, "released", True)
        now[0] += 0.1
        engine._capture_frame()
        assert "GPU returned black" in engine._capture_status
        engine.stop()
        assert gpu.released
    else:
        assert engine._gpu_capture is gpu
