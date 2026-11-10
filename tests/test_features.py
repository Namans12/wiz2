import asyncio
import json
import time

import numpy as np
import pytest

from src.core.async_runtime import BackgroundAsyncLoop
from src.features.clap_detector import ClapConfig, ClapDetector
from src.features.sync_dispatcher import CoalescingAsyncDispatcher
from src.features.screen_sync import (
    adaptive_color_boost,
    build_bulb_color_map,
    crop_content_area,
    detect_content_bounds,
    effective_screen_sync_mode,
    enhance_color,
    extract_dominant_color,
    extract_dominant_color_kmeans,
    perceptual_color_distance,
    resolve_active_regions,
    smooth_color,
)
from src.features.fast_udp_sender import encode_set_pilot_payload
from src.features.screen_sync_v2 import (
    ColorPredictor,
    GPUCaptureManager,
    MotionDetector,
    OptimizedCaptureConfig,
    OptimizedScreenSync,
    apply_cinematic_palette_hold,
    build_optimized_capture_config,
    extract_cinematic_single_color,
    extract_dominant_auto,
    extract_dominant_histogram,
    extract_dominant_kmeans,
    extract_dominant_weighted,
    should_use_gpu_capture,
    _DxcamRecoveryNoiseFilter,
    _dxcam_recovery_state,
)


def test_extract_dominant_color_returns_average_rgb():
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[..., 0] = 120
    image[..., 1] = 60
    image[..., 2] = 30

    assert extract_dominant_color(image, sample_size=2) == (120, 60, 30)


def test_smooth_color_moves_toward_target():
    assert smooth_color((0, 0, 0), (100, 50, 25), factor=0.5) == (50, 25, 12)


def test_enhance_color_boosts_low_brightness_colors():
    color = enhance_color((10, 6, 4), color_boost=1.2, min_brightness=28)

    assert max(color) >= 28
    assert color[0] > color[1] > color[2]


def test_adaptive_color_boost_stays_near_neutral_for_low_saturation_colors():
    boost = adaptive_color_boost((136, 134, 110), configured_boost=1.62)

    assert 1.0 < boost < 1.12


def test_adaptive_color_boost_preserves_extra_pop_for_vivid_colors():
    boost = adaptive_color_boost((250, 14, 96), configured_boost=1.62)

    assert boost > 1.2


def test_detect_content_bounds_ignores_letterbox_bars():
    image = np.zeros((120, 200, 3), dtype=np.uint8)
    image[20:100, :, 0] = 60
    image[20:100, :, 1] = 120
    image[20:100, :, 2] = 180

    left, top, width, height = detect_content_bounds(image)

    assert left == 0
    assert top == 20
    assert width == 200
    assert height == 80


def test_crop_content_area_falls_back_on_tiny_detected_crop(monkeypatch):
    image = np.zeros((16, 16, 3), dtype=np.uint8)
    monkeypatch.setattr("src.features.screen_sync.detect_content_bounds", lambda _: (3, 3, 0, 0))

    cropped = crop_content_area(image)

    assert cropped.shape == image.shape


def test_extract_dominant_color_kmeans_does_not_mutate_global_rng():
    image = np.full((8, 8, 3), fill_value=90, dtype=np.uint8)

    np.random.seed(123)
    expected = np.random.random(3)
    np.random.seed(123)
    extract_dominant_color_kmeans(image, sample_size=4, k=3)
    actual = np.random.random(3)

    assert np.allclose(actual, expected)


def test_zone_helpers_resolve_regions_and_mode():
    layout = {
        "192.168.1.10": "left",
        "192.168.1.11": "right",
        "192.168.1.12": "unknown",
    }

    active_regions = resolve_active_regions(
        layout,
        ["192.168.1.10", "192.168.1.11", "192.168.1.12"],
    )

    assert active_regions == ("left", "right")
    assert effective_screen_sync_mode("zones", active_regions) == "zones"
    assert effective_screen_sync_mode("zones", ("left",)) == "single"


def test_build_bulb_color_map_routes_zone_colors():
    bulb_colors = build_bulb_color_map(
        bulb_ips=["192.168.1.10", "192.168.1.11"],
        colors_by_target={
            "left": (255, 0, 0),
            "right": (0, 0, 255),
        },
        mode="zones",
        bulb_layout={
            "192.168.1.10": "left",
            "192.168.1.11": "right",
        },
    )

    assert bulb_colors == {
        "192.168.1.10": (255, 0, 0),
        "192.168.1.11": (0, 0, 255),
    }


def test_double_clap_requires_two_claps(monkeypatch):
    detector = ClapDetector(lambda: None, ClapConfig(double_clap=True, double_clap_window=0.5))
    triggers: list[str] = []
    monkeypatch.setattr(detector, "_trigger_callback", lambda: triggers.append("triggered"))

    detector._handle_clap(1.0)
    detector._handle_clap(1.4)

    assert triggers == ["triggered"]


def test_single_clap_mode_respects_cooldown(monkeypatch):
    detector = ClapDetector(lambda: None, ClapConfig(double_clap=False, cooldown=1.0))
    triggers: list[str] = []
    monkeypatch.setattr(detector, "_trigger_callback", lambda: triggers.append("triggered"))

    detector._handle_clap(1.0)
    detector._handle_clap(1.5)
    detector._handle_clap(2.2)

    assert triggers == ["triggered", "triggered"]


def test_audio_callback_detects_a_short_transient_clap(monkeypatch):
    detector = ClapDetector(
        lambda: None,
        ClapConfig(
            threshold=0.05,
            rms_threshold=0.01,
            min_peak_to_rms=2.5,
            double_clap=False,
            min_duration=0.005,
            max_duration=0.1,
            sample_rate=44100,
            block_size=512,
        ),
    )
    triggers: list[str] = []
    monkeypatch.setattr(detector, "_trigger_callback", lambda: triggers.append("triggered"))
    monkeypatch.setattr("src.features.clap_detector.time.time", lambda: 1.0)

    clap_block = np.zeros((512, 1), dtype=np.float32)
    clap_block[0, 0] = 0.5
    detector._audio_callback(clap_block, 512, None, None)

    monkeypatch.setattr("src.features.clap_detector.time.time", lambda: 1.03)
    detector._audio_callback(np.zeros((512, 1), dtype=np.float32), 512, None, None)

    assert triggers == ["triggered"]


def test_coalescing_dispatcher_keeps_latest_update_only():
    runner = BackgroundAsyncLoop()
    calls: list[int] = []

    async def apply_update(value: int):
        calls.append(value)
        await asyncio.sleep(0.04)

    try:
        dispatcher = CoalescingAsyncDispatcher(runner.submit, apply_update)
        dispatcher.submit(1)
        dispatcher.submit(2)
        dispatcher.submit(3)

        deadline = time.time() + 2.0
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.01)

        assert calls == [1, 3]
        dispatcher.close()
    finally:
        runner.shutdown()


def test_fast_udp_payload_explicitly_disables_white_channels_for_screen_rgb():
    packet = encode_set_pilot_payload((255, 8, 0, 0))
    data = json.loads(packet.decode("utf-8"))

    assert data["method"] == "setPilot"
    assert data["params"] == {
        "state": True,
        "r": 255,
        "g": 8,
        "b": 0,
        "c": 0,
        "w": 0,
    }


def test_fast_udp_payload_handles_rgbww_and_off_state():
    packet = encode_set_pilot_payload((0, 0, 0, 0, 0))
    data = json.loads(packet.decode("utf-8"))

    assert data["params"]["state"] is False
    assert data["params"]["c"] == 0
    assert data["params"]["w"] == 0


# ============== Screen Sync V2 Tests ==============

def test_extract_dominant_histogram_finds_peak_color():
    """Histogram extraction should find the most common color."""
    image = np.zeros((32, 32, 3), dtype=np.uint8)
    image[:, :, 0] = 200  # Red channel
    image[:, :, 1] = 100  # Green channel
    image[:, :, 2] = 50   # Blue channel
    
    r, g, b = extract_dominant_histogram(image, sample_size=16, bins=16)
    # Should be close to the actual values (within bin range)
    assert 180 <= r <= 220
    assert 80 <= g <= 120
    assert 30 <= b <= 70


def test_extract_dominant_kmeans_clusters_dominant():
    """K-means should identify dominant cluster color."""
    image = np.zeros((32, 32, 3), dtype=np.uint8)
    image[:, :, 0] = 150
    image[:, :, 1] = 75
    image[:, :, 2] = 200
    
    r, g, b = extract_dominant_kmeans(image, sample_size=16, k=3)
    # Should be close to uniform color
    assert 130 <= r <= 170
    assert 55 <= g <= 95
    assert 180 <= b <= 220


def test_extract_dominant_kmeans_v2_does_not_mutate_global_rng():
    image = np.full((8, 8, 3), fill_value=120, dtype=np.uint8)

    np.random.seed(456)
    expected = np.random.random(3)
    np.random.seed(456)
    extract_dominant_kmeans(image, sample_size=4, k=3)
    actual = np.random.random(3)

    assert np.allclose(actual, expected)


def test_extract_dominant_weighted_handles_uniform_image():
    """Weighted extraction matches original algorithm behavior."""
    image = np.full((16, 16, 3), fill_value=128, dtype=np.uint8)
    
    color = extract_dominant_weighted(image, sample_size=8, edge_weight=1.0)
    # Allow for minor rounding differences
    assert all(abs(c - 128) <= 1 for c in color)


def test_perceptual_color_distance_weights_green_more_than_blue():
    """Perceptual delta should treat green shifts as more visible than blue shifts."""

    base = (120, 120, 120)
    green_shift = (120, 132, 120)
    blue_shift = (120, 120, 132)

    assert perceptual_color_distance(base, green_shift) > perceptual_color_distance(base, blue_shift)


def test_extract_dominant_auto_preserves_vivid_hues():
    """Auto extraction should keep strong hues from being washed out."""
    image = np.zeros((32, 32, 3), dtype=np.uint8)
    image[:, :24, 0] = 230
    image[:, :24, 1] = 20
    image[:, :24, 2] = 30
    image[:, 24:, :] = 20

    r, g, b = extract_dominant_auto(image, sample_size=16, edge_weight=1.2)

    assert r > 150
    assert g < 90
    assert b < 90


def test_extract_dominant_auto_preserves_warm_low_saturation_scene():
    """Auto extraction should not wash warm scenes back toward gray."""
    image = np.full((32, 32, 3), fill_value=(90, 60, 35), dtype=np.uint8)
    image[:, 20:, :] = (120, 80, 40)

    r, g, b = extract_dominant_auto(image, sample_size=16, edge_weight=1.2)

    assert r > g > b
    assert r >= 85
    assert b <= 55


def test_extract_dominant_auto_avoids_muddy_brown_in_cool_scene():
    """Auto extraction should stay close to a dominant cool palette instead of averaging to brown."""

    image = np.full((36, 36, 3), fill_value=(24, 28, 34), dtype=np.uint8)
    image[:, :24, :] = (28, 120, 210)
    image[:, 24:30, :] = (60, 170, 200)
    image[10:26, 26:, :] = (185, 118, 42)

    r, g, b = extract_dominant_auto(image, sample_size=24, edge_weight=1.25)

    assert b > g > r
    assert b >= 140
    assert r <= 110


def test_extract_cinematic_single_color_biases_toward_accent_palette():
    """Single-bulb cinematic extraction should lean into visible accent colors."""
    image = np.full((48, 48, 3), fill_value=(45, 55, 70), dtype=np.uint8)
    image[:, :14, 0] = 220
    image[:, :14, 1] = 90
    image[:, :14, 2] = 30

    r, g, b = extract_cinematic_single_color(image, sample_size=24, edge_weight=1.3)

    assert r > g > b
    assert r >= 90
    assert b <= 70


def test_extract_cinematic_single_color_keeps_cool_palette_with_small_warm_patch():
    """Single-bulb mode should not collapse a mostly cool frame into warm brown."""

    image = np.full((48, 48, 3), fill_value=(22, 34, 54), dtype=np.uint8)
    image[:, :30, :] = (24, 110, 205)
    image[:, 30:40, :] = (36, 148, 214)
    image[12:30, 34:48, :] = (200, 124, 58)

    r, g, b = extract_cinematic_single_color(image, sample_size=24, edge_weight=1.3)

    assert b > g > r
    assert b >= 145
    assert r <= 115


def test_extract_cinematic_single_color_does_not_overcommit_on_balanced_multicolor_frame():
    """Balanced multicolor scenes should stay near the overall ambient light, not a single accent."""

    image = np.zeros((48, 48, 3), dtype=np.uint8)
    image[:, :16, :] = (255, 0, 0)
    image[:, 16:32, :] = (0, 255, 0)
    image[:, 32:, :] = (0, 0, 255)

    r, g, b = extract_cinematic_single_color(image, sample_size=24, edge_weight=1.3)

    assert max(r, g, b) - min(r, g, b) <= 70


def test_extract_dominant_auto_does_not_overcommit_on_balanced_multicolor_frame():
    """Balanced multicolor scenes in auto mode should avoid random accent bias."""

    image = np.zeros((48, 48, 3), dtype=np.uint8)
    image[:, :16, :] = (255, 0, 0)
    image[:, 16:32, :] = (0, 255, 0)
    image[:, 32:, :] = (0, 0, 255)

    r, g, b = extract_dominant_auto(image, sample_size=24, edge_weight=1.3)

    assert max(r, g, b) - min(r, g, b) <= 70


def test_apply_cinematic_palette_hold_retains_previous_hue_in_calm_scene():
    """Cinematic hold should preserve ambience when the next frame desaturates gently."""
    target = (86, 84, 82)
    previous = (160, 60, 28)

    held = apply_cinematic_palette_hold(target, previous, motion_score=0.01)

    assert held[0] > target[0]
    assert held[1] < held[0]
    assert held[2] < held[1]


def test_apply_cinematic_palette_hold_does_not_lag_on_fast_motion():
    """High-motion scenes should use the fresh target color without cinematic hold."""
    target = (86, 84, 82)
    previous = (160, 60, 28)

    held = apply_cinematic_palette_hold(target, previous, motion_score=0.08)

    assert held == target


def test_motion_detector_detects_change():
    """Motion detector should report high motion for different frames."""
    detector = MotionDetector(threshold=0.01)
    
    # First frame - baseline
    frame1 = np.zeros((64, 64, 3), dtype=np.uint8)
    detector.update(frame1)
    
    # Second frame - different
    frame2 = np.full((64, 64, 3), fill_value=200, dtype=np.uint8)
    score2 = detector.update(frame2)
    
    # Score should be high for dramatic change
    assert score2 > 0.5
    assert detector.is_high_motion()


def test_motion_detector_detects_static():
    """Motion detector should report low motion for same frames."""
    detector = MotionDetector(threshold=0.01)
    
    frame = np.full((64, 64, 3), fill_value=100, dtype=np.uint8)
    
    # Feed same frame multiple times
    for _ in range(5):
        detector.update(frame)
    
    assert not detector.is_high_motion()


def test_motion_detector_detects_equal_luminance_color_cut():
    detector = MotionDetector()
    red = np.full((64, 64, 3), (255, 0, 0), dtype=np.uint8)
    green = np.full((64, 64, 3), (0, 76, 0), dtype=np.uint8)
    detector.update(red)
    detector.update(green)
    assert detector.last_scene_cut


def test_motion_detector_does_not_call_color_preserving_pan_a_cut():
    detector = MotionDetector()
    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    frame[:, :32] = (255, 0, 0)
    frame[:, 32:] = (0, 76, 0)
    detector.update(frame)
    detector.update(np.roll(frame, 32, axis=1))
    assert not detector.last_scene_cut


def test_gpu_capture_manager_preserves_dxcam_rgb_channel_order():
    class FakeCamera:
        def grab(self):
            frame = np.zeros((2, 2, 3), dtype=np.uint8)
            frame[:, :, 0] = 255
            return frame

    capture = GPUCaptureManager()
    capture._camera = FakeCamera()

    frame = capture.grab()

    assert frame is not None
    assert tuple(frame[0, 0]) == (255, 0, 0)


def test_gpu_capture_manager_serves_cached_frame_on_static_screen():
    """Non-blocking contract: dxcam grab() returns None while the screen is
    unchanged; the manager must serve the cached last frame instead of
    stalling the sync loop (the static-desktop freeze regression)."""

    class FakeCamera:
        def __init__(self):
            self.calls = 0

        def grab(self):
            self.calls += 1
            if self.calls == 1:
                frame = np.zeros((2, 2, 3), dtype=np.uint8)
                frame[:, :, 1] = 200
                return frame
            return None  # screen unchanged

    capture = GPUCaptureManager()
    capture._camera = FakeCamera()

    first = capture.grab()
    second = capture.grab()
    third = capture.grab()

    assert first is not None
    assert second is not None and tuple(second[0, 0]) == (0, 200, 0)
    assert third is not None and tuple(third[0, 0]) == (0, 200, 0)


def test_gpu_capture_manager_releases_camera_on_stop():
    class FakeCamera:
        def __init__(self):
            self.released = False

        def release(self):
            self.released = True

    capture = GPUCaptureManager()
    camera = FakeCamera()
    capture._camera = camera

    capture.stop()

    assert camera.released is True
    assert capture._camera is None


def test_dxcam_recovery_noise_filter_suppresses_expected_warning():
    import logging

    before, _ = _dxcam_recovery_state()
    record = logging.LogRecord(
        name="dxcam.core.dxgi_duplicator",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="Desktop duplication access loss/system transition detected (HRESULT=0x887A0026).",
        args=(),
        exc_info=None,
    )

    assert _DxcamRecoveryNoiseFilter().filter(record) is False

    after, message = _dxcam_recovery_state()
    assert after == before + 1
    assert "Desktop duplication access loss" in message


def test_color_predictor_extrapolates_trend():
    """Color predictor should extrapolate based on history."""
    predictor = ColorPredictor(history_size=3, weight=0.5)
    
    # Add increasing brightness samples
    predictor.add_sample("all", (100, 100, 100))
    predictor.add_sample("all", (110, 110, 110))
    predictor.add_sample("all", (120, 120, 120))
    
    # Predict should be higher than current
    predicted = predictor.predict("all", (120, 120, 120))
    assert all(p >= 120 for p in predicted)


def test_color_predictor_handles_missing_key():
    """Predictor returns current color for unknown keys."""
    predictor = ColorPredictor()
    
    current = (50, 100, 150)
    predicted = predictor.predict("unknown", current)
    
    assert predicted == current


def test_optimized_config_validates_ranges():
    """OptimizedCaptureConfig should clamp values to valid ranges."""
    config = OptimizedCaptureConfig(
        min_fps=1,  # Too low
        max_fps=100,  # Too high
        color_algorithm="invalid",
        prediction_frames=100,  # Too high
    )
    
    assert config.min_fps == 4
    assert config.max_fps == 60
    assert config.color_algorithm == "weighted"
    assert config.prediction_frames == 10


def test_build_optimized_capture_config_uses_persisted_screen_settings():
    """Factory should preserve advanced screen-sync settings."""

    class Settings:
        mode = "zones"
        monitor = 2
        fps = 24
        smoothing = 0.35
        sample_size = 64
        ignore_letterbox = True
        edge_weight = 1.4
        color_boost = 1.2
        min_brightness = 30
        min_color_delta = 10
        use_gpu = False
        adaptive_fps = True
        min_fps = 10
        max_fps = 24
        color_algorithm = "auto"
        predictive_smoothing = True

    config = build_optimized_capture_config(Settings(), ("left", "right"))

    assert config.mode == "zones"
    assert config.monitor == 2
    assert config.max_fps == 24
    assert config.min_fps == 10
    assert config.color_algorithm == "auto"
    assert config.use_gpu is False
    assert config.active_regions == ("left", "right")


def test_build_optimized_capture_config_tunes_single_auto_for_cinematic_sync():
    class Settings:
        mode = "single"
        monitor = 1
        fps = 24
        smoothing = 0.3
        sample_size = 48
        ignore_letterbox = True
        edge_weight = 1.4
        color_boost = 1.25
        min_brightness = 30
        min_color_delta = 10
        use_gpu = True
        adaptive_fps = True
        min_fps = 8
        max_fps = 24
        color_algorithm = "auto"
        predictive_smoothing = True

    config = build_optimized_capture_config(Settings(), ())

    assert config.mode == "single"
    assert config.sample_size >= 60
    assert config.smoothing <= 0.18
    assert config.color_boost <= 1.14
    assert config.min_color_delta <= 6
    assert config.max_fps >= 26
    assert config.min_fps >= 14


def test_gpu_capture_uses_selected_specific_monitor(monkeypatch):
    monkeypatch.setattr("src.features.screen_sync_v2.HAS_DXCAM", True)

    primary = OptimizedCaptureConfig(monitor=1, use_gpu=True)
    extended = OptimizedCaptureConfig(monitor=2, use_gpu=True)
    combined = OptimizedCaptureConfig(monitor=0, use_gpu=True)
    region = OptimizedCaptureConfig(monitor=1, use_gpu=True, region=(0, 0, 100, 100))

    assert should_use_gpu_capture(primary) is True
    assert should_use_gpu_capture(extended) is True
    assert should_use_gpu_capture(combined) is False
    assert should_use_gpu_capture(region) is False


def test_optimized_screen_sync_debug_snapshot_reports_runtime_stats():
    """Debug snapshot should expose derived cadence and color state."""
    sync = OptimizedScreenSync(lambda _: None, OptimizedCaptureConfig())
    assert sync.debug_snapshot["capture_time_ms"] is None
    assert sync.debug_snapshot["timing_samples"] == 0

    with sync._stats_lock:
        sync._capture_method = "dxcam"
        sync._current_fps = 22
        sync._motion_score = 0.02
        sync._smoothing_factor = 0.18
        sync._prediction_strength = 0.11
        sync._effective_color_boost = 1.12
        sync._send_delta_threshold_value = 6
        sync._current_colors = {"all": (10, 20, 30)}
        sync._last_target_colors = {"all": (12, 22, 32)}
        sync._send_intervals.extend([0.05, 0.1])
        sync._capture_times.extend([0.001, 0.003])
        sync._processing_times.extend([0.004, 0.006])
        sync._updates_sent = 4
        sync._frames_processed = 8

    snapshot = sync.debug_snapshot

    assert snapshot["capture_method"] == "dxcam"
    assert snapshot["capture_status"] == "Ready"
    assert snapshot["requestedFps"] == sync.config.max_fps
    assert snapshot["current_fps"] == 22
    assert snapshot["currentFps"] == 22
    assert snapshot["frameTimeMs"] == snapshot["average_frame_time_ms"]
    assert snapshot["timing_samples"] == 2
    assert snapshot["capture_time_ms"] == pytest.approx(2.0)
    assert snapshot["capture_time_p95_ms"] == pytest.approx(2.9)
    assert snapshot["processing_time_ms"] == pytest.approx(5.0)
    assert snapshot["processing_time_p95_ms"] == pytest.approx(5.9)
    assert snapshot["smoothingFactor"] == 0.18
    assert snapshot["effectiveColorBoost"] == 1.12
    assert snapshot["minBrightness"] == sync.config.min_brightness
    assert snapshot["deltaThreshold"] == 6
    assert snapshot["captureBackend"] == "dxcam"
    assert snapshot["target_colors"]["all"] == (12, 22, 32)
    assert snapshot["targetColors"]["all"] == (12, 22, 32)
    assert snapshot["current_colors"]["all"] == (10, 20, 30)
    assert snapshot["outputColors"]["all"] == (10, 20, 30)
    assert snapshot["send_interval_ms"] == pytest.approx(75.0)
    assert round(snapshot["send_rate_hz"], 3) == round(1 / 0.075, 3)
    assert round(snapshot["sendRateHz"], 3) == round(1 / 0.075, 3)
    assert snapshot["updates_sent"] == 4
    assert snapshot["frames_processed"] == 8


# ---------------------------------------------------------------------------
# Brightness modes: full-drive normalization + dimming-channel brightness
# ---------------------------------------------------------------------------


def _frame(rgb, shape=(120, 200)):
    frame = np.zeros((*shape, 3), dtype=np.uint8)
    frame[:] = rgb
    return frame


def test_brightness_mode_config_back_compat():
    from src.core.config import ScreenSyncConfig

    # Old configs carry only brightness_follow; the mode must derive from it.
    assert ScreenSyncConfig(brightness_follow=True).brightness_mode == "follow"
    assert ScreenSyncConfig(brightness_follow=False).brightness_mode == "constant"
    # Explicit mode wins and re-derives the legacy flag.
    cfg = ScreenSyncConfig(brightness_follow=True, brightness_mode="constant")
    assert cfg.brightness_mode == "constant"
    assert cfg.brightness_follow is False
    assert ScreenSyncConfig(sync_brightness=999).sync_brightness == 255


def test_normalize_to_full_drive():
    from src.features.screen_sync_v2 import _normalize_to_full_drive

    # Half-intensity red reaches full drive with hue/sat preserved.
    assert _normalize_to_full_drive((128, 0, 0)) == (255, 0, 0)
    assert _normalize_to_full_drive((100, 60, 20)) == (255, 153, 51)
    # Near-black noise is NOT amplified (black/off path owns it).
    assert _normalize_to_full_drive((4, 4, 6)) == (4, 4, 6)
    assert _normalize_to_full_drive((0, 0, 0)) == (0, 0, 0)


def test_extract_colors_normalizes_and_sets_dimming_follow():
    engine = OptimizedScreenSync(
        lambda _: None,
        OptimizedCaptureConfig(brightness_mode="follow", min_brightness=20),
    )
    colors = engine._extract_colors(_frame((128, 0, 0)))
    assert colors["all"][0] >= 250  # red normalized to full drive
    assert colors["all"][1] <= 20 and colors["all"][2] <= 20
    # Full-range power curve (no early-saturation knee): value 0.5 lands in
    # the middle of the range, not pinned to full dimming.
    assert 140 <= engine.output_brightness <= 190

    # A brighter scene must dim MORE than a dimmer one across the whole
    # range (the old knee made everything above value=0.32 identical).
    bright_engine = OptimizedScreenSync(
        lambda _: None,
        OptimizedCaptureConfig(brightness_mode="follow", min_brightness=20),
    )
    bright_engine._extract_colors(_frame((255, 255, 255)))
    assert bright_engine.output_brightness == 255
    assert bright_engine.output_brightness > engine.output_brightness


def test_extract_colors_constant_mode_pins_brightness_and_holds_on_black():
    engine = OptimizedScreenSync(
        lambda _: None,
        OptimizedCaptureConfig(brightness_mode="constant", sync_brightness=180),
    )
    colors = engine._extract_colors(_frame((0, 160, 160)))
    assert colors, "colorful frame must produce an update"
    assert engine.output_brightness == 180

    # Persistent black: constant mode HOLDS (returns no update) instead of off.
    engine._black_since = None
    first = engine._extract_colors(_frame((0, 0, 0)))
    engine._black_since -= 1.0  # simulate persistence beyond 0.7s
    held = engine._extract_colors(_frame((0, 0, 0)))
    assert held == {}
    assert engine.output_brightness == 180
    assert first == {} or all(max(c) < 24 or c == (0, 0, 0) for c in first.values())


def test_extract_colors_follow_black_turns_off_and_dark_tapers():
    engine = OptimizedScreenSync(
        lambda _: None,
        OptimizedCaptureConfig(brightness_mode="follow", min_brightness=20),
    )
    engine._extract_colors(_frame((0, 0, 0)))
    engine._black_since -= 1.0
    off = engine._extract_colors(_frame((0, 0, 0)))
    assert off == {"all": (0, 0, 0)}

    # Near-black (above the black threshold): output brightness tapers to the
    # floor instead of flashing the normalized color at scene-floor dimming.
    engine2 = OptimizedScreenSync(
        lambda _: None,
        OptimizedCaptureConfig(brightness_mode="follow", min_brightness=20),
    )
    engine2._extract_colors(_frame((5, 5, 7)))
    assert engine2.output_brightness <= 40
