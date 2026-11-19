"""CPU-cost fixes: zero-copy WinRT grabs, static-frame reuse, precise sender gaps."""

import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from src.core.frame_pacer import FramePacer
from src.features import fast_udp_sender as udp
from src.features import screen_sync_v2 as sync_module
from src.features.screen_sync_v2 import (
    GPUCaptureManager,
    MotionDetector,
    OptimizedCaptureConfig,
    OptimizedScreenSync,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Duplicator:
    updated = True


class _ReusedBufferCamera:
    """Mimics dxcam: copy=False returns one buffer that later grabs overwrite."""

    def __init__(self, width=1920, height=1080):
        self._duplicator = _Duplicator()
        self.buffer = np.zeros((height, width, 4), dtype=np.uint8)
        self.copy_args = []

    def grab(self, *, new_frame_only=True, copy=True):
        assert new_frame_only is False
        self.copy_args.append(copy)
        return self.buffer.copy() if copy else self.buffer


def test_winrt_grab_skips_the_full_frame_copy_and_result_owns_its_pixels():
    camera = _ReusedBufferCamera()
    camera.buffer[:, :, 2] = 255  # BGRA red
    capture = GPUCaptureManager(backend="winrt")
    capture._camera = camera
    rgb = capture.grab()
    assert camera.copy_args == [False]
    assert tuple(rgb[0, 0]) == (255, 0, 0)
    assert not np.may_share_memory(rgb, camera.buffer)
    camera.buffer[:] = 0  # the next capture overwrites dxcam's buffer
    assert tuple(rgb[0, 0]) == (255, 0, 0)


def test_to_rgb_never_aliases_its_input_even_when_no_downscale_is_needed():
    narrow_rgb = np.full((8, 8, 3), 77, dtype=np.uint8)  # step == 1, contiguous
    out = GPUCaptureManager._to_rgb(narrow_rgb)
    assert not np.may_share_memory(out, narrow_rgb)
    narrow_bgra = np.full((8, 8, 4), 77, dtype=np.uint8)
    assert not np.may_share_memory(GPUCaptureManager._to_rgb(narrow_bgra), narrow_bgra)


def test_unchanged_winrt_frame_returns_the_same_array_without_reprocessing():
    camera = _ReusedBufferCamera(640, 360)
    camera.buffer[:, :, 0] = 200
    capture = GPUCaptureManager(backend="winrt")
    capture._camera = camera
    first = capture.grab()

    camera._duplicator.updated = False  # dxcam replays its cache
    conversions = []
    real_to_rgb = capture._to_rgb
    capture._to_rgb = lambda frame: conversions.append(1) or real_to_rgb(frame)
    for _ in range(4):
        assert capture.grab() is first
    assert conversions == []

    camera._duplicator.updated = True  # a genuinely new frame
    camera.buffer[:, :, 0] = 10
    second = capture.grab()
    assert second is not first
    assert tuple(second[0, 0])[2] == 10
    assert conversions == [1]


def test_frame_the_poll_could_not_convert_is_not_replaced_by_the_stale_one():
    camera = _ReusedBufferCamera(640, 360)
    camera.buffer[:, :, 2] = 255
    capture = GPUCaptureManager(backend="winrt")
    capture._camera = camera
    capture.grab()

    camera.buffer[:, :, 2] = 0
    camera.buffer[:, :, 0] = 255  # new blue frame, consumed by a failing poll
    real_to_rgb = capture._to_rgb
    capture._to_rgb = lambda frame: (_ for _ in ()).throw(ValueError("bad frame"))
    assert capture.poll_change() is None
    capture._to_rgb = real_to_rgb
    camera._duplicator.updated = False  # cache replay of the unserved blue frame
    assert tuple(capture.grab()[0, 0]) == (0, 0, 255)


def test_motion_detector_unchanged_shortcut_matches_a_full_update():
    rng = np.random.default_rng(3)
    frame_a = rng.integers(0, 255, (180, 320, 3), dtype=np.uint8)
    frame_b = rng.integers(0, 255, (180, 320, 3), dtype=np.uint8)
    full, quick = MotionDetector(), MotionDetector()
    sequence = [frame_a, frame_b, frame_b, frame_b, frame_a, frame_a]
    previous = None
    for frame in sequence:
        expected = full.update(frame)
        score = quick.update_unchanged(frame) if frame is previous else None
        if score is None:
            score = quick.update(frame)
        previous = frame
        assert score == expected
        assert quick.last_diff == full.last_diff
        assert quick.last_scene_cut == full.last_scene_cut
    assert MotionDetector().update_unchanged(frame_a) is None


def test_unchanged_frame_reuses_image_analysis_but_smoothing_keeps_converging(monkeypatch):
    engine = OptimizedScreenSync(
        lambda colors: None,
        OptimizedCaptureConfig(mode="single", use_gpu=False, adaptive_fps=False,
                               predictive_smoothing=False, parallel_regions=False),
    )
    image = np.zeros((90, 160, 3), dtype=np.uint8)
    image[:] = (230, 40, 40)
    engine._current_colors["all"] = (20, 20, 20)  # far from the target

    extractions, updates, colours_seen = [], [], []
    real_extract = engine._extract_single_color
    real_update = engine._motion_detector.update
    monkeypatch.setattr(engine, "_extract_single_color", lambda c: extractions.append(1) or real_extract(c))
    monkeypatch.setattr(engine._motion_detector, "update", lambda f: updates.append(1) or real_update(f))
    monkeypatch.setattr(sync_module.time, "sleep", lambda duration: None)

    def capture():
        colours_seen.append(engine._current_colors.get("all"))
        if len(colours_seen) >= 5:
            engine._running = False
        return image

    monkeypatch.setattr(engine, "_capture_frame", capture)
    engine._running = True
    engine._sync_loop()

    assert len(colours_seen) == 5
    assert len(extractions) == 1  # one analysis for five identical frames
    assert len(updates) == 1
    target = engine._last_target_colors["all"]

    def distance(color):
        return sum(abs(a - b) for a, b in zip(color, target))

    # The lerp toward the target still ran on every reused frame.
    assert colours_seen[1] != colours_seen[2]
    assert distance(colours_seen[2]) < distance(colours_seen[1])
    assert distance(colours_seen[4]) <= distance(colours_seen[3]) <= distance(colours_seen[2])


def test_a_new_image_object_is_always_reanalysed(monkeypatch):
    engine = OptimizedScreenSync(
        lambda colors: None,
        OptimizedCaptureConfig(mode="single", use_gpu=False, adaptive_fps=False),
    )
    extractions = []
    real_extract = engine._extract_single_color
    monkeypatch.setattr(engine, "_extract_single_color", lambda c: extractions.append(1) or real_extract(c))
    image = np.full((90, 160, 3), 120, dtype=np.uint8)
    engine._frame_unchanged = True
    engine._extract_colors(image)
    engine._frame_unchanged = True
    engine._extract_colors(image)  # same object, vouched unchanged: reused
    assert len(extractions) == 1
    engine._frame_unchanged = True
    engine._extract_colors(image.copy())  # different object: recomputed
    assert len(extractions) == 2
    engine._extract_colors(image)  # not vouched for: recomputed
    assert len(extractions) == 3


def test_zone_extraction_is_sequential_by_default():
    assert OptimizedCaptureConfig().parallel_regions is False
    engine = OptimizedScreenSync(
        lambda colors: None,
        OptimizedCaptureConfig(mode="zones", active_regions=("left", "right", "top", "bottom"), use_gpu=False),
    )
    engine._executor = None
    assert engine._extract_colors(np.full((90, 160, 3), 128, dtype=np.uint8))


def test_settings_built_config_is_sequential_for_every_zone_count():
    from src.core.config import ScreenSyncConfig

    for zones in (("left", "right"), ("left", "right", "top", "bottom")):
        config = sync_module.build_optimized_capture_config(ScreenSyncConfig(use_gpu=False), zones)
        assert config.parallel_regions is False


def test_importing_screen_sync_does_not_import_dxcam():
    code = (
        "import sys; import src.features.screen_sync_v2 as s; "
        "assert 'dxcam' not in sys.modules, 'dxcam imported eagerly'; "
        "print(isinstance(s.HAS_DXCAM, bool))"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_load_dxcam_imports_once_and_reports_missing_package(monkeypatch):
    sentinel = type("Backend", (), {})
    monkeypatch.setattr(sync_module, "dxcam", sentinel)
    assert sync_module._load_dxcam() is sentinel
    monkeypatch.setattr(sync_module, "dxcam", None)
    monkeypatch.setattr(sync_module, "HAS_DXCAM", True)
    monkeypatch.setitem(sys.modules, "dxcam", None)  # makes `import dxcam` raise ImportError
    assert sync_module._load_dxcam() is None
    assert sync_module.HAS_DXCAM is False


def test_sender_short_waits_are_not_quantised_to_the_windows_timer_tick():
    pacer = FramePacer()
    stop = threading.Event()
    try:
        for seconds, upper_ms in ((0.003, 7.0), (0.036, 40.0)):
            samples = []
            for _ in range(25):
                start = time.perf_counter()
                assert udp._precise_wait(pacer, stop, seconds) is False
                samples.append((time.perf_counter() - start) * 1000.0)
            median = statistics.median(samples)
            # Event.wait() measures ~9.6 ms and ~42 ms for these on Windows.
            assert seconds * 1000.0 * 0.9 <= median < upper_ms, (seconds, median)
    finally:
        pacer.close()


def test_precise_wait_reports_stop_and_long_waits_stay_interruptible():
    pacer = FramePacer()
    stop = threading.Event()
    try:
        timer = threading.Timer(0.02, stop.set)
        timer.start()
        start = time.perf_counter()
        assert udp._precise_wait(pacer, stop, 5.0) is True  # long: Event.wait path
        assert time.perf_counter() - start < 0.5
        assert udp._precise_wait(pacer, stop, 0.003) is True  # already stopped
        assert udp._precise_wait(pacer, threading.Event(), 0.0) is False
    finally:
        pacer.close()


class _FakeSocket:
    def __init__(self):
        self.packets = []
        self.done = threading.Event()

    def send(self, packet):
        self.packets.append(packet)
        if len(self.packets) == 3:
            self.done.set()

    def close(self):
        pass


def test_sender_uses_the_precise_wait_for_burst_and_rate_gaps_and_stops_quickly(monkeypatch):
    waits = []
    real = udp._precise_wait
    monkeypatch.setattr(udp, "_precise_wait", lambda pacer, ev, seconds: waits.append(seconds) or real(pacer, ev, seconds))
    socket = _FakeSocket()
    monkeypatch.setattr(udp.BulbUDPSocket, "_connect", lambda self: socket)
    sender = udp.BulbUDPSocket("synthetic", max_send_hz=100)
    try:
        assert sender.send(b"color")
        assert socket.done.wait(2)
    finally:
        start = time.perf_counter()
        sender.stop()
        stopped_in = time.perf_counter() - start
    assert udp._BURST_GAP in waits
    assert len(waits) >= 3  # rate gap + two burst gaps
    assert stopped_in < 0.05 + 0.1
    assert not sender._thread.is_alive()
