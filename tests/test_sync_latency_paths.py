"""Scene-change responsiveness: early wake, brightness snap, jump snap, zone dimming."""

import numpy as np
import pytest

from src.features import screen_sync_v2 as sync_module
from src.features.screen_sync_v2 import GPUCaptureManager, OptimizedCaptureConfig, OptimizedScreenSync
from src.features.sync_transport import brightness_for_bulbs


def _engine(**overrides):
    config = OptimizedCaptureConfig(use_gpu=False, adaptive_fps=False, **overrides)
    return OptimizedScreenSync(lambda colors: None, config)


def test_fixed_capture_honors_60_fps_without_changing_legacy_capture_limit():
    engine = _engine(fps=60, max_fps=60)
    assert engine.config.fps == engine._current_fps == 60
    assert OptimizedCaptureConfig(fps=500).fps == 60
    assert sync_module.CaptureConfig(fps=60).fps == 30


def test_follow_brightness_snaps_on_a_scene_cut_and_smooths_otherwise():
    engine = _engine(brightness_mode="follow")
    engine._frame_dt = 1 / 30
    engine._finalize_output_brightness(60.0, True, 255)
    assert engine.output_brightness_for("all") == 60

    # Small step without a cut: smoothed, so it has not reached the target.
    engine._finalize_output_brightness(100.0, True, 255)
    assert 60 < engine.output_brightness_for("all") < 100

    # Same step size on a detected cut: applied at once.
    engine._frame_scene_cut = True
    engine._finalize_output_brightness(140.0, True, 255)
    assert engine.output_brightness_for("all") == 140

    # A large step is a scene change even without the cut flag.
    engine._frame_scene_cut = False
    engine._finalize_output_brightness(250.0, True, 255)
    assert engine.output_brightness_for("all") == 250


def test_brightness_smoothing_depends_on_elapsed_time_not_frame_count():
    slow, fast = _engine(brightness_mode="follow"), _engine(brightness_mode="follow")
    for engine, dt in ((slow, 1 / 15), (fast, 1 / 30)):
        engine._frame_dt = dt
        engine._finalize_output_brightness(60.0, True, 255)
    # Two fast frames cover the same time as one slow frame.
    slow._finalize_output_brightness(120.0, True, 255)
    fast._finalize_output_brightness(120.0, True, 255)
    fast._finalize_output_brightness(120.0, True, 255)
    assert abs(slow._smoothed_brightness["all"] - fast._smoothed_brightness["all"]) < 0.5


def _run_two_frames(monkeypatch, frame_diff):
    updates = []
    engine = OptimizedScreenSync(
        lambda colors: record(colors),
        OptimizedCaptureConfig(use_gpu=False, adaptive_fps=False, smoothing=0.6, predictive_smoothing=False),
    )
    frames = iter([{"all": (255, 20, 0)}, {"all": (0, 40, 255)}])

    def record(colors):
        updates.append(dict(colors))
        if len(updates) == 2:
            engine._running = False

    def update(_frame):
        engine._motion_detector.last_diff = frame_diff
        engine._motion_detector.last_scene_cut = False
        return 0.0

    monkeypatch.setattr(engine, "_capture_frame", lambda: np.zeros((8, 8, 3), dtype=np.uint8))
    monkeypatch.setattr(engine, "_extract_colors", lambda _frame: next(frames))
    monkeypatch.setattr(engine._motion_detector, "update", update)
    monkeypatch.setattr(sync_module.time, "sleep", lambda duration: None)
    engine._running = True
    engine._sync_loop()
    return updates, engine.debug_snapshot


def test_large_colour_jump_is_followed_at_once_without_a_detected_cut(monkeypatch):
    updates, snapshot = _run_two_frames(monkeypatch, frame_diff=0.05)
    assert updates[1]["all"] == (0, 40, 255)
    assert snapshot["jump_snaps"] == 1


def test_colour_jump_on_an_unchanged_frame_stays_smoothed(monkeypatch):
    # No real frame change: an unstable extraction must not make the lamp jump.
    updates, snapshot = _run_two_frames(monkeypatch, frame_diff=0.0)
    assert updates[1]["all"] != (0, 40, 255)
    assert snapshot["jump_snaps"] == 0


def test_poll_change_reports_new_frames_and_grab_serves_them_without_recapturing():
    red = np.zeros((64, 64, 4), dtype=np.uint8)
    red[:, :, 2] = 255  # BGRA
    blue = np.zeros((64, 64, 4), dtype=np.uint8)
    blue[:, :, 0] = 255

    class Camera:
        def __init__(self):
            self.frames = [red, None, blue]
            self.calls = 0

        def grab(self):
            self.calls += 1
            return self.frames.pop(0) if self.frames else None

    capture = GPUCaptureManager()
    capture._camera = Camera()
    assert tuple(capture.grab()[0, 0]) == (255, 0, 0)
    assert capture.poll_change() is None  # screen unchanged
    change = capture.poll_change()
    assert change is not None and change > sync_module._EARLY_WAKE_CHANGE
    calls = capture._camera.calls
    assert tuple(capture.grab()[0, 0]) == (0, 0, 255)
    assert capture._camera.calls == calls  # served the polled frame


def test_windows_graphics_capture_session_is_created_without_the_yellow_border(monkeypatch):
    seen = {}

    class Camera:
        def grab(self, **kwargs):
            return None

        def release(self):
            pass

    def create(**kwargs):
        seen["border"] = sync_module.os.environ.get("DXCAM_WINRT_BORDER_REQUIRED")
        return Camera()

    monkeypatch.delenv("DXCAM_WINRT_BORDER_REQUIRED", raising=False)
    monkeypatch.setattr(sync_module, "HAS_DXCAM", True)
    monkeypatch.setattr(sync_module, "dxcam", type("Backend", (), {"create": staticmethod(create)}))
    capture = GPUCaptureManager(backend="winrt")
    assert capture.start()
    capture.stop()
    assert seen["border"] == "0"


def test_winrt_polled_frame_cannot_revert_to_an_older_backend_cache():
    red = np.zeros((64, 64, 4), dtype=np.uint8)
    red[:, :, 2] = 255
    blue = np.zeros_like(red)
    blue[:, :, 0] = 255

    class Camera:
        # DXCam only updates its fallback cache on new_frame_only=False.
        # A True poll can consume a newer frame while leaving that cache old.
        def __init__(self):
            self.frames = [red, blue]
            self.cached = None

        def grab(self, *, new_frame_only=True):
            if self.frames:
                frame = self.frames.pop(0)
                if not new_frame_only:
                    self.cached = frame
                return frame
            return None if new_frame_only else self.cached

    capture = GPUCaptureManager(backend="winrt")
    capture._camera = Camera()
    assert tuple(capture.grab()[0, 0]) == (255, 0, 0)
    assert capture.poll_change() > sync_module._EARLY_WAKE_CHANGE
    assert tuple(capture.grab()[0, 0]) == (0, 0, 255)
    for _ in range(3):
        assert tuple(capture.grab()[0, 0]) == (0, 0, 255)
    assert capture.poll_change() in (None, 0.0)


def test_wait_returns_early_when_the_polled_frame_changed():
    engine = OptimizedScreenSync(
        lambda colors: None,
        OptimizedCaptureConfig(use_gpu=False, adaptive_fps=True, min_fps=4, max_fps=30),
    )

    class Gpu:
        is_active = True
        poll_waits_for_frame = False
        polls = 0

        def poll_change(self):
            self.polls += 1
            return 0.5 if self.polls == 2 else None

    class Pacer:
        slept = 0.0

        def sleep(self, seconds):
            self.slept += seconds

    engine._gpu_capture, engine._frame_pacer, engine._running = Gpu(), Pacer(), True
    engine._wait_for_next_frame(0.001, 0.25)
    assert engine._gpu_capture.polls == 2
    assert engine._frame_pacer.slept < 0.1  # nowhere near the 250 ms interval
    assert engine.debug_snapshot["early_wakes"] == 1


@pytest.mark.parametrize("poll_delay", [0.0, 0.01])
def test_winrt_empty_polls_are_paced_without_double_wait(monkeypatch, poll_delay):
    engine = _engine()
    engine.config.adaptive_fps = True
    engine.config.max_fps = 30
    clock = [10.0]
    sleeps = []

    class Gpu:
        is_active = True
        poll_waits_for_frame = True
        polls = 0

        def poll_change(self):
            self.polls += 1
            assert self.polls < 70, "No-frame capture must not busy-spin"
            clock[0] += poll_delay
            return None

    class Pacer:
        def sleep(self, seconds):
            assert seconds > 0
            sleeps.append(seconds)
            clock[0] += seconds

    monkeypatch.setattr(sync_module.time, "perf_counter", lambda: clock[0])
    engine._gpu_capture, engine._frame_pacer, engine._running = Gpu(), Pacer(), True
    engine._wait_for_next_frame(0.001, 0.25)
    assert 10.249 <= clock[0] <= 10.26
    if poll_delay:
        assert len(sleeps) == 1  # only the initial frame-rate cap; polls waited already
    else:
        assert len(sleeps) > 1


def test_each_zone_dims_with_its_own_part_of_the_picture():
    engine = OptimizedScreenSync(
        lambda colors: None,
        OptimizedCaptureConfig(
            mode="zones", active_regions=("left", "right"), use_gpu=False, parallel_regions=False,
            brightness_mode="follow", ignore_letterbox=False,
        ),
    )
    image = np.zeros((90, 160, 3), dtype=np.uint8)
    image[:, :80] = (0, 0, 60)     # dim blue left
    image[:, 80:] = (255, 40, 0)   # bright red right
    colors = engine._extract_colors(image)
    assert set(colors) == {"left", "right"}
    left, right = engine.output_brightness_for("left"), engine.output_brightness_for("right")
    assert left < right - 60
    assert engine.output_brightness == right

    levels = brightness_for_bulbs(engine, ["a", "b"], "zones", {"a": "left", "b": "right"})
    assert levels == {"a": left, "b": right}
    assert brightness_for_bulbs(engine, ["a", "b"], "single", {}) == engine.output_brightness


def test_changing_opposite_zone_does_not_shift_tint():
    engine = _engine(mode="zones", active_regions=("left", "right"),
                     parallel_regions=False, ignore_letterbox=False)
    frame = np.full((90, 160, 3), (160, 157, 155), dtype=np.uint8)
    original = engine._extract_colors(frame)
    frame[:, 80:] = (0, 0, 255)
    changed = engine._extract_colors(frame)
    assert original["left"] == changed["left"]
    assert original["right"] != changed["right"]
