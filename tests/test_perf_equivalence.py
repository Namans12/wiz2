"""Speed-ups that must not change a single output: physical-mapper build and
cache, scene-cut histogram, and the letterbox crop-box reuse."""

import numpy as np
import pytest

from src.core import bulb_physical_model as physical
from src.core import color_mapping
from src.core.calibration import BulbCalibrationTable, CalibrationSample
from src.core.color_mapping import BulbColorProfile, BulbGamutMapper, clear_lut_cache
from src.features import screen_sync_v2 as sync_module
from src.features.screen_sync_v2 import (
    CROP_REDETECT_INTERVAL_S,
    MotionDetector,
    OptimizedCaptureConfig,
    OptimizedScreenSync,
    _histogram_32,
)


# ---------------------------------------------------------------------------
# Physical mapper: batched compression and the built-table memo
# ---------------------------------------------------------------------------


def _reference_solve_channel_mix_batch(target_xyz, primaries, triangles=None):
    """The previous implementation: one scalar bisection per out-of-gamut point."""

    if triangles is None:
        triangles = physical._valid_triangles(primaries)
    targets = np.asarray(target_xyz, dtype=np.float64).reshape(-1, 3)
    result, any_valid = physical._bounded_mixes(targets, primaries, triangles)
    for idx in np.where(~any_valid)[0]:
        result[idx] = physical.solve_channel_mix(targets[idx], primaries, triangles)
    return result.reshape(np.asarray(target_xyz).shape[:-1] + (primaries.xyz.shape[0],))


def _rgb_grid(step):
    values = np.arange(0, 256, step)
    return [(int(r), int(g), int(b)) for r in values for g in values for b in values]


def test_batched_compression_matches_scalar_build_exactly(monkeypatch):
    profile = BulbColorProfile(white_channels=2, rgb_channel_current=(9, 8, 6), mixing_model="physical")

    clear_lut_cache()
    new = BulbGamutMapper(profile)

    clear_lut_cache()
    monkeypatch.setattr(physical, "solve_channel_mix_batch", _reference_solve_channel_mix_batch)
    old = BulbGamutMapper(profile)
    monkeypatch.undo()
    clear_lut_cache()

    assert np.array_equal(new._lookup, old._lookup)
    assert np.array_equal(new._gray_lookup, old._gray_lookup)
    for color in _rgb_grid(8):  # 32768 inputs
        assert new.map_rgb(color) == old.map_rgb(color)


@pytest.mark.parametrize(
    "profile",
    [
        BulbColorProfile(white_channels=1, source_color_space="display-p3", mixing_model="physical"),
        BulbColorProfile(white_channels=0, mixing_model="physical"),
    ],
)
def test_batched_compression_matches_scalar_on_other_profiles(profile, monkeypatch):
    clear_lut_cache()
    new = BulbGamutMapper(profile)
    clear_lut_cache()
    monkeypatch.setattr(physical, "solve_channel_mix_batch", _reference_solve_channel_mix_batch)
    old = BulbGamutMapper(profile)
    monkeypatch.undo()
    clear_lut_cache()
    assert np.array_equal(new._lookup, old._lookup)
    assert np.array_equal(new._gray_lookup, old._gray_lookup)


def test_compress_batch_matches_scalar_for_out_of_gamut_targets():
    primaries = physical.PhysicalPrimaries.from_profile(BulbColorProfile(white_channels=2))
    triangles = physical._valid_triangles(primaries)
    rng = np.random.default_rng(3)
    # Far outside the reachable zonotope: wildly bright and over-saturated XYZ.
    targets = rng.uniform(0.0, 6.0, (40, 3))
    batch = physical._compress_toward_gray_batch(targets, primaries, triangles)
    for target, got in zip(targets, batch):
        expected = physical._compress_toward_gray(target, primaries, triangles)
        assert np.array_equal(got, expected)


def test_mapper_cache_returns_identical_tables_without_rebuilding(monkeypatch):
    clear_lut_cache()
    profile = BulbColorProfile(white_channels=2, mixing_model="physical")
    first = BulbGamutMapper(profile)

    builds = []
    real = BulbGamutMapper._build_lookup
    monkeypatch.setattr(BulbGamutMapper, "_build_lookup", lambda self: builds.append(1) or real(self))
    second = BulbGamutMapper(profile)
    assert builds == []
    assert second._lookup is first._lookup and second._gray_lookup is first._gray_lookup
    for color in [(255, 0, 0), (12, 200, 90), (128, 128, 128), (0, 0, 0), (255, 255, 255)]:
        assert second.map_rgb(color) == first.map_rgb(color)
    assert not first._lookup.flags.writeable

    # Identity-only fields do not change the tables, so they still hit.
    BulbGamutMapper(BulbColorProfile(white_channels=2, mixing_model="physical", mac="aa:bb", model_name="x"))
    assert builds == []


@pytest.mark.parametrize(
    "changed",
    [
        {"mixing_model": "heuristic"},
        {"source_color_space": "display-p3"},
        {"white_channels": 1},
        {"white_channel_current": (24, 20)},
        {"rgb_channel_current": (12, 8, 6)},
        {"cct_warm_kelvin": 2400.0},
        {"cool_mix_bias": 0.1},
        {"white_to_color_ratio": 30},
    ],
)
def test_mapper_cache_rebuilds_when_a_key_input_changes(changed, monkeypatch):
    clear_lut_cache()
    base = dict(white_channels=2, mixing_model="physical")
    BulbGamutMapper(BulbColorProfile(**base))
    builds = []
    real = BulbGamutMapper._build_lookup
    monkeypatch.setattr(BulbGamutMapper, "_build_lookup", lambda self: builds.append(1) or real(self))
    BulbGamutMapper(BulbColorProfile(**{**base, **changed}))
    assert builds == [1]


def test_mapper_cache_keys_calibration_and_tone_by_content(monkeypatch):
    clear_lut_cache()
    samples = [
        CalibrationSample(target_rgb=(i * 50, 40, 200 - i * 30), measured_rgb=(i * 45, 50, 190 - i * 28))
        for i in range(5)
    ]

    def table(strength=0.45):
        return BulbCalibrationTable(key="k", samples=list(samples), strength=strength)

    profile = BulbColorProfile(white_channels=1)
    a = BulbGamutMapper(profile, calibration=table())
    builds = []
    real = BulbGamutMapper._build_lookup
    monkeypatch.setattr(BulbGamutMapper, "_build_lookup", lambda self: builds.append(1) or real(self))
    same = BulbGamutMapper(profile, calibration=table())  # equal content, new object
    assert builds == [] and same._lookup is a._lookup
    other = BulbGamutMapper(profile, calibration=table(strength=0.9))
    assert builds == [1] and other._lookup is not a._lookup
    BulbGamutMapper(profile, calibration=table(), tone_lut=table())
    assert builds == [1, 1]
    BulbGamutMapper(profile)  # no calibration: distinct from the calibrated tables
    assert builds == [1, 1, 1]


def test_mapper_cache_is_bounded():
    clear_lut_cache()
    for kelvin in range(2000, 2000 + color_mapping._LUT_CACHE_MAX + 4):
        BulbGamutMapper(BulbColorProfile(white_channels=0, cct_warm_kelvin=float(kelvin)))
    assert len(color_mapping._lut_cache) == color_mapping._LUT_CACHE_MAX
    clear_lut_cache()


# ---------------------------------------------------------------------------
# Scene-cut luma histogram
# ---------------------------------------------------------------------------


def _luma(small):
    return (
        small[:, :, 0] * 0.2126
        + small[:, :, 1] * 0.7152
        + small[:, :, 2] * 0.0722
    )


def _assert_same_histogram(luma):
    expected = np.histogram(luma, bins=32, range=(0.0, 255.0))[0]
    got = _histogram_32(luma, 32, 255.0)
    assert np.array_equal(got, expected)


def test_histogram_matches_numpy_on_random_frames():
    rng = np.random.default_rng(11)
    for _ in range(40):
        frame = rng.integers(0, 256, (180, 320, 3), dtype=np.uint8).astype(np.float32)
        _assert_same_histogram(_luma(frame))
    # Smooth gradients and narrow ranges put many samples near boundaries.
    ramp = np.tile(np.linspace(0, 255, 320, dtype=np.float32), (180, 1))
    _assert_same_histogram(ramp)
    _assert_same_histogram(np.round(ramp / 8.0) * 8.0)


@pytest.mark.parametrize("value", [0, 255])
def test_histogram_matches_numpy_on_flat_frames(value):
    frame = np.full((180, 320, 3), value, dtype=np.float32)
    _assert_same_histogram(_luma(frame))


def test_histogram_matches_numpy_on_bin_edges_and_neighbours():
    edges = np.linspace(0.0, 255.0, 33, dtype=np.float32)
    candidates = [edges, np.nextafter(edges, np.float32(-1)), np.nextafter(edges, np.float32(300))]
    candidates.append(np.float32(np.arange(33) * 255.0 / 32.0))  # float64-derived edges
    values = np.concatenate(candidates).astype(np.float32)
    values = values[(values >= 0.0) & (values <= 255.0)]
    _assert_same_histogram(np.tile(values, 7).reshape(-1, 7).astype(np.float32))
    for edge in values:
        _assert_same_histogram(np.full((4, 4), edge, dtype=np.float32))


def test_histogram_falls_back_to_numpy_semantics_out_of_range():
    for bad in (255.1, -0.5, np.nan):
        luma = np.full((10, 10), 100.0, dtype=np.float32)
        luma[3, 3] = bad
        _assert_same_histogram(luma)


def test_motion_detector_histogram_is_unchanged_end_to_end():
    rng = np.random.default_rng(5)
    frames = [
        rng.integers(0, 256, (90, 320, 3), dtype=np.uint8),
        np.zeros((90, 320, 3), dtype=np.uint8),
        np.full((90, 320, 3), 255, dtype=np.uint8),
        rng.integers(0, 256, (90, 320, 3), dtype=np.uint8),
    ]
    detector = MotionDetector()
    reference_prev = None
    for frame in frames:
        detector.update(frame)
        hist = np.histogram(_luma(frame.astype(np.float32)), bins=32, range=(0.0, 255.0))[0]
        hist = hist.astype(np.float64) / max(1.0, float(hist.sum()))
        assert np.array_equal(detector._prev_hist, hist)
        reference_prev = hist
    assert reference_prev is not None


# ---------------------------------------------------------------------------
# Letterbox crop-box reuse
# ---------------------------------------------------------------------------


def _letterboxed(height=240, width=320, bar=30):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[bar : height - bar] = (200, 120, 60)
    return image


def _crop_engine(monkeypatch):
    engine = OptimizedScreenSync(
        lambda colors: None,
        OptimizedCaptureConfig(mode="single", use_gpu=False, ignore_letterbox=True),
    )
    clock = [1000.0]
    monkeypatch.setattr(sync_module.time, "monotonic", lambda: clock[0])
    detections = []
    real = sync_module.detect_content_bounds
    monkeypatch.setattr(
        sync_module, "detect_content_bounds", lambda image: detections.append(image.shape[:2]) or real(image)
    )
    return engine, clock, detections


def test_crop_matches_uncached_result_and_reuses_box_within_interval(monkeypatch):
    engine, clock, detections = _crop_engine(monkeypatch)
    image = _letterboxed()
    cropped = engine._crop_content(image)
    assert cropped.shape[:2] == (180, 320)
    for _ in range(5):
        clock[0] += CROP_REDETECT_INTERVAL_S / 20.0
        assert engine._crop_content(image).shape[:2] == (180, 320)
    assert len(detections) == 1


def test_crop_redetects_after_the_interval(monkeypatch):
    engine, clock, detections = _crop_engine(monkeypatch)
    engine._crop_content(_letterboxed(bar=30))
    clock[0] += CROP_REDETECT_INTERVAL_S + 0.01
    cropped = engine._crop_content(_letterboxed(bar=50))
    assert len(detections) == 2
    assert cropped.shape[:2] == (140, 320)


def test_crop_redetects_immediately_on_scene_cut(monkeypatch):
    engine, clock, detections = _crop_engine(monkeypatch)
    engine._crop_content(_letterboxed(bar=30))
    clock[0] += 0.05
    assert engine._crop_content(_letterboxed(bar=50)).shape[:2] == (180, 320)  # stale until a cut
    engine._motion_detector.last_scene_cut = True
    cropped = engine._crop_content(_letterboxed(bar=50))
    assert len(detections) == 2
    assert cropped.shape[:2] == (140, 320)


def test_crop_redetects_immediately_on_frame_size_change(monkeypatch):
    engine, clock, detections = _crop_engine(monkeypatch)
    engine._crop_content(_letterboxed(240, 320, 30))
    clock[0] += 0.05
    cropped = engine._crop_content(_letterboxed(480, 640, 60))
    assert len(detections) == 2
    assert cropped.shape[:2] == (360, 640)


def test_crop_disabled_returns_frame_and_start_resets_state(monkeypatch):
    engine, clock, detections = _crop_engine(monkeypatch)
    engine.config.ignore_letterbox = False
    image = _letterboxed()
    assert engine._crop_content(image) is image
    assert detections == []
    engine.config.ignore_letterbox = True
    engine._crop_content(image)
    assert engine._crop_state is not None
