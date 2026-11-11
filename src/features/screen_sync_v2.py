"""Optimized screen capture and color extraction with GPU acceleration and adaptive FPS."""

import gc
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Optional, Sequence

import numpy as np

# Try GPU capture first, fallback to mss
try:
    import dxcam
    HAS_DXCAM = True
except ImportError:
    HAS_DXCAM = False

import mss

from .screen_sync import (
    CaptureConfig,
    adaptive_color_boost,
    crop_content_area,
    crop_relative_region,
    enhance_color,
    perceptual_color_distance,
    effective_screen_sync_mode,
    _resolve_monitor,
)
from ..core.color_science import (
    OKLAB_DELTA_SCALE,
    SRGB8_TO_LINEAR,
    lerp_oklab8,
    oklab_distance8,
    srgb_encode,
)


def _linear_mean_srgb8(
    pixels: np.ndarray,
    weights: Optional[np.ndarray] = None,
) -> tuple[int, int, int]:
    """Weighted mean of gamma-encoded pixels, accumulated in linear light.

    Averaging gamma-encoded values under-weights bright/saturated content and
    darkens mixes; light adds linearly. Scene-analysis *weights* stay in gamma
    space (the cinematic tuning was built there) — only the accumulation is
    physical. ``pixels`` holds integral 0-255 values (any float/int dtype).
    """

    flat = pixels.reshape(-1, 3)
    if flat.shape[0] == 0:
        return (0, 0, 0)
    linear = SRGB8_TO_LINEAR[np.clip(flat.astype(np.int32), 0, 255)]
    if weights is None:
        mean = linear.mean(axis=0)
    else:
        w = weights.reshape(-1, 1).astype(np.float64)
        total = max(float(w.sum()), 1e-6)
        mean = (linear * w).sum(axis=0) / total
    encoded = srgb_encode(np.clip(mean, 0.0, 1.0))
    return tuple(int(np.clip(round(float(v) * 255.0), 0, 255)) for v in encoded)

_DXCAM_RECOVERY_LOCK = threading.Lock()
_DXCAM_RECOVERY_EVENTS = 0
_DXCAM_RECOVERY_LAST_MESSAGE = ""


class _DxcamRecoveryNoiseFilter(logging.Filter):
    """Hide expected DXGI recovery warnings while keeping a status counter."""

    _NOISY_MARKERS = (
        "Desktop duplication access loss/system transition detected",
        "Output change/access loss detected",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if any(marker in message for marker in self._NOISY_MARKERS):
            global _DXCAM_RECOVERY_EVENTS, _DXCAM_RECOVERY_LAST_MESSAGE
            with _DXCAM_RECOVERY_LOCK:
                _DXCAM_RECOVERY_EVENTS += 1
                _DXCAM_RECOVERY_LAST_MESSAGE = message
            return False
        return True


def _install_dxcam_noise_filter() -> None:
    if not HAS_DXCAM:
        return

    noise_filter = _DxcamRecoveryNoiseFilter()
    for logger_name in ("dxcam.dxcam", "dxcam.core.dxgi_duplicator"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(existing, _DxcamRecoveryNoiseFilter) for existing in logger.filters):
            logger.addFilter(noise_filter)


def _dxcam_recovery_state() -> tuple[int, str]:
    with _DXCAM_RECOVERY_LOCK:
        return _DXCAM_RECOVERY_EVENTS, _DXCAM_RECOVERY_LAST_MESSAGE


_install_dxcam_noise_filter()


@dataclass
class OptimizedCaptureConfig(CaptureConfig):
    """Extended configuration with optimization settings."""
    
    smoothing: float = 0.25
    
    # GPU capture settings
    use_gpu: bool = True
    gpu_device: int = 0
    
    # Adaptive FPS settings
    adaptive_fps: bool = True
    min_fps: int = 8
    max_fps: int = 30
    motion_threshold: float = 0.015  # Frame diff threshold for motion detection
    
    # Color algorithm
    color_algorithm: str = "auto"  # "auto", "weighted", "kmeans", "histogram"
    
    # Predictive smoothing
    predictive_smoothing: bool = True
    prediction_frames: int = 3
    prediction_weight: float = 0.3
    
    # Parallel processing
    parallel_regions: bool = True

    # Content-adaptive behavior
    sync_profile: str = "auto"       # "auto" | "cinema" | "action"
    brightness_follow: bool = True   # legacy alias of brightness_mode=="follow"
    # "follow": bulb dimming tracks scene brightness (black -> off).
    # "constant": dimming pinned at sync_brightness; black holds last color.
    brightness_mode: str = "follow"
    sync_brightness: int = 180

    def __post_init__(self) -> None:
        super().__post_init__()
        self.smoothing = max(0.05, min(0.85, float(self.smoothing)))
        self.min_fps = max(4, min(30, int(self.min_fps)))
        self.max_fps = max(self.min_fps, min(60, int(self.max_fps)))
        self.motion_threshold = max(0.001, min(0.1, float(self.motion_threshold)))
        self.color_algorithm = self.color_algorithm if self.color_algorithm in {"auto", "weighted", "kmeans", "histogram"} else "weighted"
        self.prediction_frames = max(2, min(10, int(self.prediction_frames)))
        self.prediction_weight = max(0.0, min(0.8, float(self.prediction_weight)))
        self.use_gpu = bool(self.use_gpu)
        self.adaptive_fps = bool(self.adaptive_fps)
        self.predictive_smoothing = bool(self.predictive_smoothing)
        self.parallel_regions = bool(self.parallel_regions)
        profile = str(self.sync_profile).lower()
        self.sync_profile = profile if profile in {"auto", "cinema", "action"} else "auto"
        mode = str(self.brightness_mode).lower()
        self.brightness_mode = mode if mode in {"follow", "constant"} else ("follow" if self.brightness_follow else "constant")
        self.brightness_follow = self.brightness_mode == "follow"
        self.sync_brightness = max(30, min(255, int(self.sync_brightness)))


def blend_colors(
    base: tuple[int, int, int],
    accent: tuple[int, int, int],
    accent_weight: float,
) -> tuple[int, int, int]:
    """Blend two RGB colors with a controllable accent weight."""

    accent_weight = max(0.0, min(1.0, float(accent_weight)))
    base_weight = 1.0 - accent_weight
    return tuple(
        int(np.clip(base[channel] * base_weight + accent[channel] * accent_weight, 0, 255))
        for channel in range(3)
    )


def _sample_even_rgb(image: np.ndarray, sample_size: int) -> np.ndarray:
    """Sample the centers of an even grid spanning the entire image."""
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        return np.zeros((sample_size, sample_size, 3), dtype=image.dtype)
    rows = ((2 * np.arange(sample_size) + 1) * height) // (2 * sample_size)
    cols = ((2 * np.arange(sample_size) + 1) * width) // (2 * sample_size)
    return image[rows[:, None], cols[None, :], :3]


def _sample_pixels(
    image: np.ndarray,
    sample_size: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Downsample once and return pixel arrays for shared color analysis.

    The hot path needs a stable color sample spanning the whole image, not a
    high-quality resize. An even grid avoids large image allocations and does
    not omit the lower or right parts when dimensions are not grid multiples.
    """

    sampled = _sample_even_rgb(image, sample_size)
    pixels = sampled.astype(np.float32, copy=False)
    return pixels, pixels / 255.0


def _scene_channels(normalized: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return saturation and luma maps for a normalized RGB sample."""

    saturation = normalized.max(axis=2) - normalized.min(axis=2)
    luma = (
        normalized[:, :, 0] * 0.2126
        + normalized[:, :, 1] * 0.7152
        + normalized[:, :, 2] * 0.0722
    )
    return saturation, luma


# Scene-level content adaptation: below this average saturation a scene is
# treated as (partially) neutral/B&W and the bulb is pulled toward true white;
# scene luma below the knee dims the bulb proportionally when brightness-follow
# is enabled.
NEUTRAL_SATURATION_THRESHOLD = 0.06
_TRUE_BLACK_LUMA = 0.015
# Power-law brightness curve for follow-mode dimming, spanning the FULL scene
# value range 0..1 (no early-saturation knee). A prior version normalized by
# a 0.32 knee then clamped to 1.0 -- since typical video content averages
# 0.3-0.6 scene value, that pinned dimming at ~100% for most of a video's
# runtime, defeating "follow video brightness". gamma<1 still lifts shadows
# a bit (a dim-but-not-black scene stays visible) without an artificial cap.
_BRIGHTNESS_FOLLOW_GAMMA = 0.7


def _scene_stats(image: np.ndarray, sample_size: int = 24) -> tuple[float, float, float]:
    """Return (avg saturation, avg luma, avg value) of a cheap stride sample.

    Value = mean of the per-pixel max channel. Brightness-follow uses VALUE,
    not luma: luma's 0.07 blue / 0.21 red weights make a full-intensity pure
    blue or red read as a "dark scene" and get wrongly dimmed. Value treats a
    saturated full-brightness color as bright (which it is), while genuinely
    dark scenes still read low. Luma is still returned for true-black detection.
    """

    _, normalized = _sample_pixels(image, sample_size)
    saturation, luma = _scene_channels(normalized)
    value = normalized.max(axis=2)
    return float(saturation.mean()), float(luma.mean()), float(value.mean())


def _neutral_strength(avg_saturation: float) -> float:
    """0 = colorful scene, 1 = fully neutral/B&W scene (smoothstepped)."""

    t = max(0.0, min(1.0, (NEUTRAL_SATURATION_THRESHOLD - avg_saturation) / NEUTRAL_SATURATION_THRESHOLD))
    return t * t * (3.0 - 2.0 * t)


def _brightness_factor(avg_value: float) -> float:
    """Scene value (avg max-channel) → output brightness factor, full 0..1 span."""

    return max(0.0, min(1.0, avg_value)) ** _BRIGHTNESS_FOLLOW_GAMMA


def _apply_content_adaptation(
    color: tuple[int, int, int],
    neutral: float,
) -> tuple[int, int, int]:
    """Desaturate neutral scenes toward true gray.

    Neutral scenes must land on the bulb's white channels (the WiZ mapper
    routes low-saturation colors to CW/WW), so B&W film reads as clean white
    instead of an amplified color cast. Scene brightness is no longer encoded
    here — it rides the setPilot ``dimming`` field so hue is always rendered
    at full LED drive (see _normalize_to_full_drive).
    """

    r, g, b = color
    if neutral > 0.0:
        gray = int(round(r * 0.2126 + g * 0.7152 + b * 0.0722))
        r = int(round(r + (gray - r) * neutral))
        g = int(round(g + (gray - g) * neutral))
        b = int(round(b + (gray - b) * neutral))
    return (
        max(0, min(255, r)),
        max(0, min(255, g)),
        max(0, min(255, b)),
    )


def _normalize_to_full_drive(
    color: tuple[int, int, int],
    min_peak: int = 8,
) -> tuple[int, int, int]:
    """Scale a color so its max channel reaches 255 (hue/sat preserved).

    Real video frames rarely average anywhere near channel 255, so the old
    pipeline drove the bulb's LEDs at 30-60% where hue rendering is muddy and
    dim — the single biggest "colors don't map" complaint. Brightness belongs
    in the dimming field, not the RGB magnitude. Colors darker than
    ``min_peak`` are left untouched (their hue is quantization noise; the
    black/off path owns them).
    """

    peak = max(color)
    if peak < min_peak or peak >= 255:
        return color
    scale = 255.0 / peak
    return (
        min(255, int(round(color[0] * scale))),
        min(255, int(round(color[1] * scale))),
        min(255, int(round(color[2] * scale))),
    )


def _edge_mask(height: int, width: int, edge_weight: float) -> np.ndarray:
    """Bias sampling toward the screen perimeter for ambient lighting."""

    mask = np.ones((height, width), dtype=np.float32)
    if edge_weight <= 1.0:
        return mask

    border = max(1, min(height, width) // 6)
    mask[:border, :] *= edge_weight
    mask[-border:, :] *= edge_weight
    mask[:, :border] *= edge_weight
    mask[:, -border:] *= edge_weight
    return mask


def _ambient_weights(
    saturation: np.ndarray,
    luma: np.ndarray,
    edge_weight: float,
) -> np.ndarray:
    """Build fast ambient weights that emphasize vivid edge content."""

    weights = 0.15 + saturation * 2.2 + luma * 0.5
    weights *= _edge_mask(*saturation.shape, edge_weight)
    return weights


def _weighted_color(
    pixels: np.ndarray,
    weights: np.ndarray,
) -> tuple[int, int, int]:
    """Compute a weighted RGB average from sampled pixels (linear light)."""

    return _linear_mean_srgb8(pixels[:, :, :3], weights)


def _extract_vibrant_accent_from_sample(
    pixels: np.ndarray,
    saturation: np.ndarray,
    luma: np.ndarray,
) -> tuple[int, int, int]:
    """Extract a vivid accent directly from a shared low-resolution sample."""

    energy = saturation * 0.8 + luma * 0.2
    threshold = float(np.quantile(energy, 0.8))
    mask = energy >= threshold
    if not np.any(mask):
        return _linear_mean_srgb8(pixels[:, :, :3])
    return _linear_mean_srgb8(pixels[mask][:, :3])


def _extract_palette_anchor_from_sample(
    pixels: np.ndarray,
    saturation: np.ndarray,
    luma: np.ndarray,
) -> tuple[int, int, int]:
    """Extract a tighter palette anchor from the most color-rich pixels."""

    energy = saturation * 0.9 + np.clip(0.65 - np.abs(luma - 0.42), 0.0, 0.65) * 0.1
    threshold = float(np.quantile(energy, 0.9))
    mask = (energy >= threshold) & (luma > 0.05)
    if not np.any(mask):
        return _extract_vibrant_accent_from_sample(pixels, saturation, luma)
    return _linear_mean_srgb8(pixels[mask][:, :3])


def _palette_candidates(
    pixels: np.ndarray,
    weights: np.ndarray,
    top_k: int = 6,
    bin_size: int = 32,
) -> list[tuple[tuple[int, int, int], float]]:
    """Build a small weighted palette from quantized sampled pixels."""

    flat_pixels = pixels.reshape(-1, 3)
    flat_weights = weights.reshape(-1).astype(np.float64)
    if flat_pixels.size == 0:
        return []

    levels = max(2, min(16, (255 // max(8, bin_size)) + 1))
    quantized = np.clip((flat_pixels / float(bin_size)).astype(np.int32), 0, levels - 1)
    indices = quantized[:, 0] * levels * levels + quantized[:, 1] * levels + quantized[:, 2]
    palette_size = levels ** 3

    counts = np.bincount(indices, weights=flat_weights, minlength=palette_size)
    nonzero = int(np.count_nonzero(counts))
    if nonzero == 0:
        return []

    candidate_count = min(top_k, nonzero)
    candidate_indices = np.argpartition(counts, -candidate_count)[-candidate_count:]
    candidate_indices = candidate_indices[np.argsort(counts[candidate_indices])[::-1]]

    sum_r = np.bincount(indices, weights=flat_pixels[:, 0] * flat_weights, minlength=palette_size)
    sum_g = np.bincount(indices, weights=flat_pixels[:, 1] * flat_weights, minlength=palette_size)
    sum_b = np.bincount(indices, weights=flat_pixels[:, 2] * flat_weights, minlength=palette_size)

    candidates: list[tuple[tuple[int, int, int], float]] = []
    for idx in candidate_indices:
        weight = float(counts[idx])
        if weight <= 0.0:
            continue
        color = (
            int(np.clip(sum_r[idx] / weight, 0, 255)),
            int(np.clip(sum_g[idx] / weight, 0, 255)),
            int(np.clip(sum_b[idx] / weight, 0, 255)),
        )
        candidates.append((color, weight))
    return candidates


def _select_palette_match(
    reference: tuple[int, int, int],
    candidates: Sequence[tuple[tuple[int, int, int], float]],
    prefer_vivid: bool = False,
) -> tuple[int, int, int]:
    """Choose the closest palette entry using perceptual distance and prominence."""

    if not candidates:
        return reference

    max_weight = max(weight for _, weight in candidates) or 1.0
    reference_luma = color_luma(reference)
    best_color = reference
    best_score: Optional[float] = None

    for color, weight in candidates:
        prominence = weight / max_weight
        saturation = color_saturation(color)
        luma = color_luma(color)
        score = perceptual_color_distance(color, reference)
        score -= prominence * 52.0
        score -= saturation * (78.0 if prefer_vivid else 42.0)
        score += abs(luma - reference_luma) * 24.0
        if reference_luma > 0.08 and luma < 0.02:
            score += 18.0
        if best_score is None or score < best_score:
            best_score = score
            best_color = color

    return best_color


def _palette_balance_score(
    candidates: Sequence[tuple[tuple[int, int, int], float]],
) -> float:
    """Estimate whether a frame contains several equally strong, distinct colors."""

    if len(candidates) < 3:
        return 0.0

    top = list(candidates[:3])
    weights = np.array([weight for _, weight in top], dtype=np.float32)
    if float(weights[0]) <= 0.0:
        return 0.0

    prominence = float(np.mean(weights[1:] / weights[0]))
    separation = float(
        np.mean(
            [
                perceptual_color_distance(top[i][0], top[j][0])
                for i in range(len(top))
                for j in range(i + 1, len(top))
            ]
        )
    )
    return min(1.0, prominence * min(1.0, separation / 170.0))


def _mixed_color_balance(normalized: np.ndarray, weights: np.ndarray) -> float:
    """Identify two substantial, distinct primary-color populations."""
    maximum = normalized.max(axis=2)
    minimum = normalized.min(axis=2)
    dominant = normalized.argmax(axis=2)
    # A magenta pixel, for example, should not count as a separate red or
    # blue population merely because one channel wins a near tie.
    second = np.partition(normalized, 1, axis=2)[:, :, 1]
    strong = (maximum - minimum >= 0.35) & (maximum - second >= 0.15)
    totals = np.bincount(dominant[strong], weights=weights[strong], minlength=3)
    leaders = np.sort(totals)[::-1]
    if leaders[0] <= 0 or (leaders[0] + leaders[1]) < float(weights.sum()) * 0.45:
        return 0.0
    return _smoothstep_scalar((leaders[1] / leaders[0] - 0.25) / 0.30)


def _smoothstep_scalar(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)




def color_saturation(color: tuple[int, int, int]) -> float:
    """Approximate saturation for an RGB tuple."""

    values = np.array(color, dtype=np.float32) / 255.0
    return float(values.max() - values.min())


def color_luma(color: tuple[int, int, int]) -> float:
    """Approximate perceived brightness for an RGB tuple."""

    values = np.array(color, dtype=np.float32) / 255.0
    return float(values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722)


def extract_dominant_auto(
    image: np.ndarray,
    sample_size: int = 48,
    edge_weight: float = 1.35,
) -> tuple[int, int, int]:
    """Blend ambient weighting with a fast perceptual palette match."""

    analysis_size = max(16, min(sample_size, 32))
    pixels, normalized = _sample_pixels(image, analysis_size)
    saturation, luma = _scene_channels(normalized)
    weights = _ambient_weights(saturation, luma, edge_weight)

    weighted = _weighted_color(pixels, weights)
    accent = _extract_vibrant_accent_from_sample(pixels, saturation, luma)

    vivid_ratio = float(((saturation > 0.35) & (luma > 0.08)).mean())
    scene_energy = min(1.0, float(vivid_ratio * 2.2 + saturation.mean() * 1.8))

    accent_weight = 0.10 + scene_energy * 0.22
    if scene_energy < 0.35:
        accent_weight *= 0.55
    reference = blend_colors(weighted, accent, accent_weight)

    palette_weights = weights * (0.65 + saturation * 1.35)
    candidates = _palette_candidates(pixels, palette_weights, top_k=6, bin_size=32)
    balance_score = _palette_balance_score(candidates)
    weighted_saturation = color_saturation(weighted)
    if balance_score > 0.32 and weighted_saturation < 0.22:
        neutral_pull = min(0.88, balance_score * (0.55 + max(0.0, 0.22 - weighted_saturation) * 2.4))
        reference = blend_colors(reference, weighted, neutral_pull)
    matched = _select_palette_match(reference, candidates, prefer_vivid=scene_energy > 0.3)

    palette_weight = 0.12 + scene_energy * 0.28
    if scene_energy < 0.2:
        palette_weight *= 0.45
    if balance_score > 0.32 and weighted_saturation < 0.22:
        palette_weight *= max(0.15, 1.0 - balance_score * 1.25)
    two_color_balance = _mixed_color_balance(normalized, weights)
    reference = blend_colors(reference, weighted, two_color_balance)
    palette_weight *= 1.0 - two_color_balance
    return blend_colors(reference, matched, palette_weight)


def extract_cinematic_single_color(
    image: np.ndarray,
    sample_size: int = 48,
    edge_weight: float = 1.35,
) -> tuple[int, int, int]:
    """Single-bulb extractor biased toward richer cinematic ambience."""

    analysis_size = max(16, min(sample_size, 32))
    pixels, normalized = _sample_pixels(image, analysis_size)
    saturation, luma = _scene_channels(normalized)
    weights = _ambient_weights(saturation, luma, edge_weight)

    weighted = _weighted_color(pixels, weights)
    sat_mean = float(saturation.mean())
    vivid_ratio = float(((saturation > 0.32) & (luma > 0.08)).mean())
    darkness = float(luma.mean())

    if darkness < 0.03 and sat_mean < 0.04:
        return weighted

    accent = _extract_vibrant_accent_from_sample(pixels, saturation, luma)
    palette_anchor = _extract_palette_anchor_from_sample(pixels, saturation, luma)

    accent_weight = 0.18 + min(0.30, sat_mean * 0.5 + vivid_ratio * 0.22)
    if sat_mean < 0.08 and darkness > 0.18:
        accent_weight *= 0.8

    reference = blend_colors(weighted, accent, accent_weight)
    reference = blend_colors(reference, palette_anchor, 0.08 + min(0.18, vivid_ratio * 0.24))

    palette_weights = weights * (0.7 + saturation * 1.8 + np.clip(0.6 - np.abs(luma - 0.42), 0.0, 0.6) * 0.3)
    candidates = _palette_candidates(pixels, palette_weights, top_k=7, bin_size=32)
    balance_score = _palette_balance_score(candidates)
    weighted_saturation = color_saturation(weighted)
    if balance_score > 0.3 and weighted_saturation < 0.22:
        neutral_pull = min(0.9, balance_score * (0.6 + max(0.0, 0.22 - weighted_saturation) * 2.6))
        reference = blend_colors(reference, weighted, neutral_pull)
    matched = _select_palette_match(reference, candidates, prefer_vivid=True)

    palette_weight = 0.20 + min(0.34, sat_mean * 0.6 + vivid_ratio * 0.3)
    if sat_mean < 0.12:
        palette_weight *= 0.55
    if balance_score > 0.3 and weighted_saturation < 0.22:
        palette_weight *= max(0.12, 1.0 - balance_score * 1.35)

    two_color_balance = _mixed_color_balance(normalized, weights)
    reference = blend_colors(reference, weighted, two_color_balance)
    palette_weight *= 1.0 - two_color_balance

    return blend_colors(reference, matched, palette_weight)


def apply_cinematic_palette_hold(
    target: tuple[int, int, int],
    previous: Optional[tuple[int, int, int]],
    motion_score: float,
) -> tuple[int, int, int]:
    """Keep some prior hue in calm, low-saturation scenes without lagging on cuts."""

    if previous is None:
        return target

    target_sat = color_saturation(target)
    previous_sat = color_saturation(previous)
    target_luma = color_luma(target)

    if target_luma < 0.035:
        return target
    if motion_score > 0.045:
        return target
    if previous_sat < 0.12:
        return target
    if target_sat >= previous_sat * 0.92:
        return target

    hold_strength = min(0.34, max(0.0, (previous_sat - target_sat) * 0.85))
    hold_strength *= max(0.0, 1.0 - min(1.0, motion_score / 0.03))

    if target_sat < 0.12 and target_luma < 0.55:
        hold_strength = min(0.4, hold_strength + 0.08)

    if hold_strength <= 0.01:
        return target

    return blend_colors(target, previous, hold_strength)


def resolve_auto_display_profile(settings) -> dict:
    """Resolve auto-detected display gamut/HDR/profile when settings request 'auto'.

    Returns a dict with keys ``source_color_space``, ``hdr_tone_mapping``,
    ``device_profile``, ``supported_hdr_types``, ``primary_hdr_type``,
    ``detected_display_gamut``, ``detected_hdr_support``. Safe to call from
    any thread; falls back silently to current settings if detection fails.
    """
    try:
        from src.core.display_detection import detect_display_profile, cool_mix_bias_for_profile
    except Exception:
        return {}

    requested_space = str(getattr(settings, "source_color_space", "auto")).lower()
    requested_profile = str(getattr(settings, "device_profile", "auto")).lower()
    requested_hdr_tm = bool(getattr(settings, "hdr_tone_mapping", False))

    detected = detect_display_profile()
    if requested_space == "auto":
        # Capture-space binding: on Windows, DXCam/MSS 8-bit captures come from
        # the sRGB-composited desktop (with HDR on, Windows tone-maps into that
        # buffer too) even on wide-gamut OLED panels — treating them as P3/2020
        # double-processes and skews hue/brightness. macOS is the exception:
        # CGDisplay captures carry the display's real profile (P3 on Macs).
        import platform as _platform

        if _platform.system().lower() == "darwin":
            final_space = detected.source_color_space
        else:
            final_space = "srgb"
    else:
        final_space = requested_space
    final_profile = detected.device_profile if requested_profile == "auto" else requested_profile
    final_hdr_tm = requested_hdr_tm if requested_space != "auto" else False
    return {
        "source_color_space": final_space,
        "hdr_tone_mapping": final_hdr_tm,
        "device_profile": final_profile,
        "supported_hdr_types": detected.supported_hdr_types,
        "primary_hdr_type": detected.primary_hdr_type,
        "detected_display_gamut": detected.source_color_space,
        "detected_hdr_support": detected.hdr_supported,
        "cool_mix_bias": cool_mix_bias_for_profile(final_profile),
    }


def build_optimized_capture_config(
    settings,
    active_regions: Sequence[str],
) -> OptimizedCaptureConfig:
    """Build optimized runtime config from persisted screen-sync settings."""

    mode = getattr(settings, "mode", "single")
    color_algorithm = str(getattr(settings, "color_algorithm", "auto"))
    effective_mode = effective_screen_sync_mode(mode, active_regions)
    max_fps = int(getattr(settings, "max_fps", getattr(settings, "fps", 12)))
    min_fps = int(getattr(settings, "min_fps", max(4, max_fps // 2)))
    adaptive_fps = bool(getattr(settings, "adaptive_fps", True))
    smoothing = float(getattr(settings, "smoothing", 0.25))
    sample_size = int(getattr(settings, "sample_size", 48))
    color_boost = float(getattr(settings, "color_boost", 1.15))
    min_color_delta = int(getattr(settings, "min_color_delta", 12))

    single_cinematic = effective_mode == "single" and color_algorithm == "auto"
    if single_cinematic:
        sample_size = max(sample_size, 60)
        smoothing = min(smoothing, 0.18)
        color_boost = min(color_boost, 1.14)
        min_color_delta = min(min_color_delta, 6)
        max_fps = max(max_fps, 26)
        min_fps = max(min_fps, min(14, max_fps))

    return OptimizedCaptureConfig(
        mode=mode,
        monitor=getattr(settings, "monitor", 1),
        fps=max_fps if adaptive_fps else int(getattr(settings, "fps", max_fps)),
        smoothing=smoothing,
        sample_size=sample_size,
        region=getattr(settings, "region", None),
        ignore_letterbox=bool(getattr(settings, "ignore_letterbox", True)),
        edge_weight=float(getattr(settings, "edge_weight", 1.35)),
        color_boost=color_boost,
        min_brightness=int(getattr(settings, "min_brightness", 28)),
        min_color_delta=min_color_delta,
        active_regions=tuple(active_regions),
        use_gpu=bool(getattr(settings, "use_gpu", True)),
        adaptive_fps=adaptive_fps,
        min_fps=min_fps,
        max_fps=max_fps,
        color_algorithm=color_algorithm,
        predictive_smoothing=bool(getattr(settings, "predictive_smoothing", True)),
        parallel_regions=len(tuple(active_regions)) > 1,
        sync_profile=str(getattr(settings, "sync_profile", "auto")),
        brightness_follow=bool(getattr(settings, "brightness_follow", True)),
        brightness_mode=str(getattr(settings, "brightness_mode", None) or ("follow" if getattr(settings, "brightness_follow", True) else "constant")),
        sync_brightness=int(getattr(settings, "sync_brightness", 180)),
    )


# Downscale captured frames to at most this width BEFORE any per-frame copy /
# color work. A 4K (3840px) BGRA->RGB conversion cost ~39 ms/frame — the entire
# latency budget — yet extraction only samples ~60px. Working at ~640px drops
# that to ~1 ms with no measurable color-accuracy change (extraction strides
# either way), taking 4K sync from ~3 fps to the FPS cap.
_PROCESS_MAX_WIDTH = 640


@lru_cache(maxsize=4)
def _distributed_sample_indices(height: int, width: int, step: int) -> tuple[np.ndarray, np.ndarray]:
    """Choose a repeatable subpixel per cell without a fixed stride phase."""
    rows = np.arange((height + step - 1) // step, dtype=np.uint32)[:, None]
    cols = np.arange((width + step - 1) // step, dtype=np.uint32)[None, :]
    hashed = ((cols * np.uint32(1103515245) + rows * np.uint32(12345))
              ^ (cols * np.uint32(2654435761) + rows * np.uint32(2246822519)))
    y = np.minimum(rows * step + hashed % step, height - 1).astype(np.int32)
    x = np.minimum(cols * step + (hashed // step) % step, width - 1).astype(np.int32)
    return y, x


def _downscale_for_processing(frame: np.ndarray, *, bgra: bool = False) -> np.ndarray:
    """Sample across the whole frame, distributing subpixel phases per cell.

    Fixed-phase striding can mistake a repeating fine pattern for a single
    color. Distributed sampling reduces that bias while retaining actual
    source pixels for the downstream linear-light color calculation.
    """
    height, width = frame.shape[:2]
    channels = slice(2, None, -1) if bgra else slice(0, 3)
    step = max(1, width // _PROCESS_MAX_WIDTH)
    if step == 1:
        return np.ascontiguousarray(frame[:, :, channels])
    rows, cols = _distributed_sample_indices(height, width, step)
    return np.ascontiguousarray(frame[rows, cols, channels])


class GPUCaptureManager:
    """GPU screen capture via DXCam — non-blocking, restart-safe.

    Deliberately does NOT use dxcam's video mode: video-mode
    ``get_latest_frame()`` BLOCKS until the screen changes, which froze the
    sync loop forever on static desktops (Frame ms 0.0, no colors, status
    "Running"), made ``stop()``'s thread-join time out, and tore down the
    dxcam camera singleton underneath the stuck thread — poisoning every
    subsequent restart (the GUI restarts sync on each settings change).

    Instead each ``grab()`` is a non-blocking DXGI acquire that returns None
    when the screen hasn't changed; we then serve the cached last frame,
    which is pixel-identical by definition.
    """

    def __init__(self, device: int = 0, monitor: int = 0, backend: str = "dxgi"):
        self._camera = None
        self._device = device
        self._monitor = monitor
        self.backend = backend
        self._lock = threading.Lock()
        self._last_frame: Optional[np.ndarray] = None
        self._consecutive_errors = 0
        self._last_error: Optional[str] = None

    def start(self, target_fps: int = 30) -> bool:
        """Initialize GPU capture. target_fps kept for caller compatibility."""
        if not HAS_DXCAM:
            return False

        with self._lock:
            try:
                self._last_frame = None
                self._consecutive_errors = 0
                self._last_error = None
                # output_color="BGRA" keeps dxcam on the raw DXGI byte order and
                # avoids its cv2-based color converter entirely — the frozen
                # (PyInstaller) build excludes OpenCV, where the default "RGB"
                # mode made every grab() raise ModuleNotFoundError('cv2').
                # We slice BGRA -> RGB ourselves in grab().
                backend_options = {"backend": self.backend} if self.backend != "dxgi" else {}
                self._camera = dxcam.create(
                    device_idx=self._device,
                    output_idx=self._monitor,
                    output_color="BGRA",
                    **backend_options,
                )
                if self._camera is None:
                    return False
                # Prime the cache so the first static-screen grab has pixels —
                # and verify grabbing actually works in this environment.
                probe = self._camera.grab(new_frame_only=False) if self.backend == "winrt" else self._camera.grab()
                if probe is not None:
                    self._last_frame = self._to_rgb(probe)
                return True
            except Exception:
                # Release so the dxcam factory doesn't hand the broken
                # singleton back to the next create() call.
                camera = self._camera
                self._camera = None
                if camera is not None:
                    try:
                        camera.release()
                    except Exception:
                        pass
                return False

    @staticmethod
    def _to_rgb(frame: np.ndarray) -> np.ndarray:
        if frame.ndim == 3 and frame.shape[2] == 4:
            return _downscale_for_processing(frame, bgra=True)
        return _downscale_for_processing(frame)

    def grab(self) -> Optional[np.ndarray]:
        """Capture a frame. Returns RGB numpy array or None (never blocks)."""
        if self._camera is None:
            return None

        try:
            # WinRT's one-shot default waits briefly for a new frame; on
            # Windows that wait can be rounded up by the scheduler. Ask for
            # its cached frame instead so a static scene never incurs it.
            frame = self._camera.grab(new_frame_only=False) if self.backend == "winrt" else self._camera.grab()
        except Exception as exc:
            self._consecutive_errors += 1
            self._last_error = str(exc)
            # None without an exception means unchanged pixels; an exception
            # is a failed acquisition and must never masquerade as a new frame.
            if self._consecutive_errors >= 3:
                self.stop()
            return None
        self._consecutive_errors = 0
        self._last_error = None
        if frame is not None:
            self._last_frame = self._to_rgb(frame)
            return self._last_frame
        # None = screen unchanged since last acquire -> same pixels.
        return self._last_frame

    def stop(self) -> None:
        """Stop GPU capture."""
        with self._lock:
            camera = self._camera
            self._camera = None
            self._last_frame = None
            if camera is not None:
                try:
                    camera.release()
                except Exception:
                    pass
                del camera
                gc.collect()

    @property
    def is_active(self) -> bool:
        return self._camera is not None

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error


def should_use_gpu_capture(config: OptimizedCaptureConfig) -> bool:
    """Return whether DXCam can honor the selected capture target reliably."""

    if not HAS_DXCAM or not config.use_gpu:
        return False
    if config.region is not None:
        return False
    return int(config.monitor) > 0


def _stride_sample_rgb(image: np.ndarray, sample_size: int) -> np.ndarray:
    """Even-grid RGB sample of shape (sample_size, sample_size, 3).

    Matches the hot-path auto extractor's sampling so the optional
    weighted/histogram/kmeans algorithms avoid PIL object creation and large
    resize allocations.
    """

    return np.ascontiguousarray(_sample_even_rgb(image, sample_size))


def extract_dominant_histogram(
    image: np.ndarray,
    sample_size: int = 48,
    bins: int = 16,
) -> tuple[int, int, int]:
    """Extract dominant color using histogram peaks."""

    pixels = _stride_sample_rgb(image, sample_size)

    # Build histograms for each channel
    colors = []
    for channel in range(3):
        hist, bin_edges = np.histogram(pixels[:, :, channel].ravel(), bins=bins, range=(0, 256))
        # Find peak bin
        peak_bin = hist.argmax()
        # Get center of peak bin
        color_val = int((bin_edges[peak_bin] + bin_edges[peak_bin + 1]) / 2)
        colors.append(color_val)
    
    return tuple(colors)


def extract_dominant_weighted(
    image: np.ndarray,
    sample_size: int = 48,
    edge_weight: float = 1.35,
) -> tuple[int, int, int]:
    """Extract weighted ambient color (original algorithm)."""

    pixels = _stride_sample_rgb(image, sample_size).astype(np.float32)

    normalized = pixels / 255.0
    height, width = pixels.shape[:2]
    max_channel = normalized.max(axis=2)
    min_channel = normalized.min(axis=2)
    saturation = max_channel - min_channel
    luma = (
        normalized[:, :, 0] * 0.2126
        + normalized[:, :, 1] * 0.7152
        + normalized[:, :, 2] * 0.0722
    )
    
    weights = 0.15 + saturation * 2.2 + luma * 0.5
    border = max(1, min(height, width) // 6)
    if edge_weight > 1.0:
        edge_mask = np.ones((height, width), dtype=np.float32)
        edge_mask[:border, :] *= edge_weight
        edge_mask[-border:, :] *= edge_weight
        edge_mask[:, :border] *= edge_weight
        edge_mask[:, -border:] *= edge_weight
        weights *= edge_mask

    return _linear_mean_srgb8(pixels[:, :, :3], weights)


def extract_dominant_kmeans(
    image: np.ndarray,
    sample_size: int = 48,
    k: int = 3,
) -> tuple[int, int, int]:
    """Extract dominant color using k-means clustering."""

    pixels = _stride_sample_rgb(image, sample_size).reshape(-1, 3).astype(np.float32)

    if len(pixels) == 0:
        return (0, 0, 0)

    k = max(1, min(int(k), len(pixels)))
    rng = np.random.default_rng(42)
    indices = rng.choice(len(pixels), k, replace=False)
    centroids = pixels[indices]
    
    for _ in range(10):
        distances = np.sqrt(((pixels[:, np.newaxis] - centroids) ** 2).sum(axis=2))
        labels = distances.argmin(axis=1)
        new_centroids = np.array([
            pixels[labels == i].mean(axis=0) if (labels == i).any() else centroids[i]
            for i in range(k)
        ])
        if np.allclose(centroids, new_centroids):
            break
        centroids = new_centroids
    
    counts = np.bincount(labels, minlength=k)
    return tuple(int(channel) for channel in centroids[counts.argmax()])


class MotionDetector:
    """Detects motion between frames for adaptive FPS, plus hard scene cuts.

    Scene cuts use luma-histogram distance plus a large change in the frame's
    mean color. The latter also catches cuts between equally bright hues.
    Callers use ``last_scene_cut`` to bypass smoothing and gating for one frame.
    """

    _CUT_HIST_BINS = 32
    _CUT_HIST_THRESHOLD = 0.30  # total-variation distance (0..1) that counts as a cut
    _CUT_DIFF_THRESHOLD = 0.10  # instantaneous frame diff required alongside it
    _CUT_COLOR_THRESHOLD = 0.12  # mean absolute RGB change, normalized to 0..1

    def __init__(self, threshold: float = 0.015):
        self._threshold = threshold
        self._last_frame: Optional[np.ndarray] = None
        self._motion_history: deque = deque(maxlen=5)
        self._prev_hist: Optional[np.ndarray] = None
        self.last_scene_cut = False

    _CUT_HIST_TARGET_COLS = 320  # keep the cut histogram this dense regardless of input size

    def update(self, frame: np.ndarray) -> float:
        """Update with new frame and return motion score (0-1)."""
        # Downsample for fast comparison. The frame is already downscaled to
        # ~640px wide by _downscale_for_processing, so a fixed [::8] stride would
        # leave the luma histogram with only ~3.6k samples — too sparse for the
        # 32-bin total-variation cut test its 0.30 threshold was tuned on. Derive
        # the stride from the frame width so the histogram stays ~320 cols wide
        # (~57k samples) whatever the capture resolution, keeping the cut
        # threshold valid on both downscaled and full-res frames.
        step = max(1, frame.shape[1] // self._CUT_HIST_TARGET_COLS)
        small = frame[::step, ::step].astype(np.float32)

        # Luma histogram for scene-cut detection (cheap: runs on the same
        # downsample used for motion diffing). A cut must move the histogram
        # AND the instantaneous frame diff: histogram-only fires falsely on
        # near-solid fades (all luma in one bin hopping bins), diff-only fires
        # on fast panning. Hard cuts move both by an order of magnitude.
        luma = (
            small[:, :, 0] * 0.2126
            + small[:, :, 1] * 0.7152
            + small[:, :, 2] * 0.0722
        )
        hist = np.histogram(luma, bins=self._CUT_HIST_BINS, range=(0.0, 255.0))[0]
        hist = hist.astype(np.float64) / max(1.0, float(hist.sum()))
        hist_tv = (
            0.0
            if self._prev_hist is None
            else 0.5 * float(np.abs(hist - self._prev_hist).sum())
        )
        self._prev_hist = hist

        if self._last_frame is None or self._last_frame.shape != small.shape:
            self._last_frame = small
            self._motion_history.clear()
            self.last_scene_cut = False
            return 0.5  # Default to medium motion on first frame

        # Calculate frame difference
        diff = np.abs(small - self._last_frame).mean() / 255.0
        # Sample spatially before averaging to keep this inexpensive. Comparing
        # means (rather than pixels) avoids treating a pan of the same colors
        # as a global ambient-color change.
        color_change = 0.0
        if diff > self._CUT_DIFF_THRESHOLD and hist_tv <= self._CUT_HIST_THRESHOLD:
            mean_rgb = small[::4, ::4].mean(axis=(0, 1))
            previous_rgb = self._last_frame[::4, ::4].mean(axis=(0, 1))
            color_change = float(np.abs(mean_rgb - previous_rgb).mean() / 255.0)
        self._last_frame = small
        self._motion_history.append(diff)

        self.last_scene_cut = (
            diff > self._CUT_DIFF_THRESHOLD
            and (hist_tv > self._CUT_HIST_THRESHOLD or color_change > self._CUT_COLOR_THRESHOLD)
        )

        return float(np.mean(self._motion_history))

    def is_high_motion(self) -> bool:
        """Returns True if recent frames show high motion."""
        if not self._motion_history:
            return False
        return np.mean(self._motion_history) > self._threshold

    def reset(self) -> None:
        """Reset motion history."""
        self._last_frame = None
        self._motion_history.clear()
        self._prev_hist = None
        self.last_scene_cut = False


class ColorPredictor:
    """Predicts next color using linear extrapolation."""
    
    def __init__(self, history_size: int = 3, weight: float = 0.3):
        self._history: dict[str, deque] = {}
        self._history_size = history_size
        self._weight = weight
    
    def add_sample(self, key: str, color: tuple[int, int, int]) -> None:
        """Add a color sample for a key (region)."""
        if key not in self._history:
            self._history[key] = deque(maxlen=self._history_size)
        self._history[key].append(color)
    
    def predict(self, key: str, current: tuple[int, int, int]) -> tuple[int, int, int]:
        """Predict next color based on trend."""
        if key not in self._history or len(self._history[key]) < 2:
            return current
        
        history = list(self._history[key])
        if len(history) < 2:
            return current
        
        # Calculate velocity (color change per frame)
        velocities = []
        for i in range(1, len(history)):
            vel = tuple(history[i][c] - history[i-1][c] for c in range(3))
            velocities.append(vel)
        
        # Average velocity
        avg_vel = tuple(int(np.mean([v[c] for v in velocities])) for c in range(3))
        
        # Predict only along a trend the latest observed target still follows.
        # A pause or reversal must not carry stale momentum past the scene.
        predicted = []
        for c in range(3):
            latest_delta = current[c] - history[-1][c]
            velocity = avg_vel[c]
            if velocity * latest_delta <= 0:
                predicted.append(current[c])
                continue
            step = min(abs(velocity), abs(latest_delta)) * self._weight
            predicted.append(int(np.clip(current[c] + (step if velocity > 0 else -step), 0, 255)))
        
        return tuple(predicted)
    
    def clear(self) -> None:
        """Clear all history."""
        self._history.clear()


class OptimizedScreenSync:
    """High-performance screen color sync with all optimizations."""
    
    def __init__(
        self,
        on_color_change: Callable[[dict[str, tuple[int, int, int]]], None],
        config: Optional[OptimizedCaptureConfig] = None,
    ):
        self.config = config or OptimizedCaptureConfig()
        self.on_color_change = on_color_change
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._current_colors: dict[str, tuple[int, int, int]] = {}
        self._last_sent_colors: dict[str, tuple[int, int, int]] = {}
        self._last_target_colors: dict[str, tuple[int, int, int]] = {}
        self._stats_lock = threading.Lock()
        self._last_send_time: Optional[float] = None
        self._send_intervals: deque = deque(maxlen=30)
        self._motion_score = 0.0
        self._smoothing_factor = self.config.smoothing
        self._prediction_strength = 0.0
        self._effective_color_boost = self.config.color_boost
        self._send_delta_threshold_value = self.config.min_color_delta
        self._updates_sent = 0
        self._frames_processed = 0
        self._last_error: Optional[str] = None
        self._last_loop_time: Optional[float] = None
        self._last_color_send_at: dict[str, float] = {}
        self._cuts_detected = 0
        self._last_scene_cut = False
        self._last_neutral = 0.0
        self._scene_neutral = 0.0
        self._scene_luma = 0.0
        self._black_since: Optional[float] = None
        self._output_brightness = (
            255 if self.config.brightness_mode == "follow" else int(self.config.sync_brightness)
        )
        self._smoothed_brightness: Optional[float] = None
        self._last_sent_brightness: Optional[int] = None

        # Optimization components
        self._gpu_capture: Optional[GPUCaptureManager] = None
        self._rejected_gpu_capture: Optional[GPUCaptureManager] = None
        self._gpu_black_since: Optional[float] = None
        self._gpu_probe_after = 0.0
        self._gpu_fallback_reason: Optional[str] = None
        self._motion_detector = MotionDetector(self.config.motion_threshold)
        self._color_predictor = ColorPredictor(
            self.config.prediction_frames,
            self.config.prediction_weight,
        )
        self._executor: Optional[ThreadPoolExecutor] = None
        self._mss_instance: Optional[mss.mss] = None
        
        self._effective_mode = effective_screen_sync_mode(
            self.config.mode,
            self.config.active_regions,
        )
        self._current_fps = self.config.max_fps if self.config.adaptive_fps else self.config.fps
        
        # Performance stats
        self._frame_times: deque = deque(maxlen=30)
        self._capture_times: deque = deque(maxlen=120)
        self._processing_times: deque = deque(maxlen=120)
        self._capture_method = "mss"
        self._capture_status = "Ready"
        self._dxcam_recovery_events_seen = 0
        self._dxcam_recovery_status_until = 0.0
    
    def start(self) -> None:
        """Start optimized screen sync."""
        if self._running:
            return
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Previous screen-sync worker has not stopped")
        
        self._running = True
        self._gpu_black_since = None
        self._gpu_probe_after = 0.0
        self._gpu_fallback_reason = None
        self._motion_detector.reset()
        self._color_predictor.clear()
        self._current_fps = self.config.max_fps if self.config.adaptive_fps else self.config.fps
        with self._stats_lock:
            self._current_colors.clear()
            self._last_sent_colors.clear()
            self._frame_times.clear()
            self._capture_times.clear()
            self._processing_times.clear()
            self._last_target_colors = {}
            self._last_send_time = None
            self._send_intervals.clear()
            self._motion_score = 0.0
            self._smoothing_factor = self.config.smoothing
            self._prediction_strength = 0.0
            self._effective_color_boost = self.config.color_boost
            self._send_delta_threshold_value = self.config.min_color_delta
            self._updates_sent = 0
            self._frames_processed = 0
            self._last_error = None
            self._last_loop_time = None
            self._last_color_send_at.clear()
            self._cuts_detected = 0
            self._last_scene_cut = False
            self._last_neutral = 0.0
            self._scene_neutral = 0.0
            self._scene_luma = 0.0
            self._black_since = None
            self._output_brightness = (
                255 if self.config.brightness_mode == "follow" else int(self.config.sync_brightness)
            )
            self._smoothed_brightness = None
            self._last_sent_brightness = None
            self._capture_status = "OK"
            self._dxcam_recovery_events_seen, _ = _dxcam_recovery_state()
            self._dxcam_recovery_status_until = 0.0
        
        # Initialize GPU capture if available and enabled
        if should_use_gpu_capture(self.config):
            self._gpu_capture = GPUCaptureManager(
                device=self.config.gpu_device,
                monitor=max(0, self.config.monitor - 1),
            )
            if self._gpu_capture.start(target_fps=self.config.max_fps):
                self._capture_method = "dxcam"
            else:
                self._gpu_capture = None
                self._capture_method = "mss"
        else:
            self._gpu_capture = None
            self._capture_method = "mss"
        
        # Initialize thread pool for parallel region processing
        if self.config.parallel_regions and self._effective_mode == "zones":
            self._executor = ThreadPoolExecutor(max_workers=min(4, len(self.config.active_regions)))
        
        # mss is created lazily INSIDE the sync thread (see _capture_frame):
        # Windows GDI capture handles are thread-affine, and an instance made
        # here on the UI thread silently fails when used from the sync thread
        # in frozen builds.
        self._mss_instance = None

        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()
    
    def stop(self) -> None:
        """Stop screen sync and release resources."""
        self._running = False
        
        if self._thread:
            if self._thread is threading.current_thread():
                raise RuntimeError("Stop screen sync from outside its capture callback")
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise RuntimeError("Screen-sync worker did not stop; capture resources retained")
            self._thread = None
        
        if self._gpu_capture:
            self._gpu_capture.stop()
            self._gpu_capture = None
        if self._rejected_gpu_capture:
            self._rejected_gpu_capture.stop()
            self._rejected_gpu_capture = None
        
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None
        
    def _capture_frame(self) -> Optional[np.ndarray]:
        """Capture a frame using best available method."""
        # Try GPU capture first
        gpu_error = None
        if self._gpu_capture and self._gpu_capture.is_active:
            frame = self._gpu_capture.grab()
            gpu_error = self._gpu_capture.last_error
            if not self._gpu_capture.is_active:
                self._gpu_capture = None
            recovery_events, _ = _dxcam_recovery_state()
            if recovery_events > self._dxcam_recovery_events_seen:
                self._dxcam_recovery_events_seen = recovery_events
                self._dxcam_recovery_status_until = time.perf_counter() + 4.0
                with self._stats_lock:
                    self._capture_status = "GPU capture recovered"
            if frame is not None:
                self._capture_method = "winrt" if getattr(self._gpu_capture, "backend", "dxgi") == "winrt" else "dxcam"
                if time.perf_counter() > self._dxcam_recovery_status_until:
                    with self._stats_lock:
                        self._capture_status = "OK"
                now = time.perf_counter()
                sample = frame[::max(1, frame.shape[0] // 48), ::max(1, frame.shape[1] // 48)]
                if np.max(sample) < 8:
                    if self._gpu_black_since is None:
                        self._gpu_black_since = now
                    if now - self._gpu_black_since >= 1.0 and now >= self._gpu_probe_after:
                        self._gpu_probe_after = now + 2.0
                        comparison = self._capture_mss_frame()
                        if comparison is not None:
                            pixels = comparison[::max(1, comparison.shape[0] // 48),
                                                ::max(1, comparison.shape[1] // 48)]
                            cpu_bright = float(np.mean(pixels)) > 12 and float(np.mean(np.max(pixels, axis=2) > 24)) > 0.15
                            if getattr(self._gpu_capture, "backend", "dxgi") != "winrt":
                                alternative = self._try_windows_capture(comparison)
                                if alternative is not None:
                                    return alternative
                            if cpu_bright:
                                rejected, self._gpu_capture = self._gpu_capture, None
                                if getattr(rejected, "backend", "dxgi") == "winrt":
                                    rejected.stop()
                                else:
                                    self._rejected_gpu_capture = rejected
                                self._gpu_fallback_reason = "GPU returned black while CPU capture showed a picture"
                                self._capture_method = "mss"
                                with self._stats_lock:
                                    self._capture_status = f"MSS fallback ({self._gpu_fallback_reason})"
                                return comparison
                else:
                    self._gpu_black_since = None
                return frame

        frame = self._capture_mss_frame()
        if frame is not None:
            now = time.perf_counter()
            if self._gpu_capture is None and should_use_gpu_capture(self.config) and now >= self._gpu_probe_after:
                self._gpu_probe_after = now + 2.0
                alternative = self._try_windows_capture(frame)
                if alternative is not None:
                    return alternative
            self._capture_method = "mss"
            with self._stats_lock:
                reason = self._gpu_fallback_reason or (f"GPU error: {gpu_error}" if gpu_error else None)
                self._capture_status = f"MSS fallback ({reason})" if reason else "OK"
        return frame

    def _try_windows_capture(self, comparison: np.ndarray) -> Optional[np.ndarray]:
        """Try a second GPU backend and confirm it observes the same display."""
        candidate = GPUCaptureManager(device=self.config.gpu_device,
                                      monitor=max(0, self.config.monitor - 1), backend="winrt")
        if not candidate.start(target_fps=self.config.max_fps):
            return None
        frame = candidate.grab()
        if frame is not None:
            reference_mean = comparison.mean(axis=(0, 1))
            candidate_mean = frame.mean(axis=(0, 1))
            cpu_black = float(np.max(comparison)) < 8
            candidate_bright = float(np.mean(frame)) > 12 and float(np.mean(np.max(frame, axis=2) > 24)) > 0.15
            if candidate_bright and (cpu_black or float(np.max(np.abs(reference_mean - candidate_mean))) < 55):
                rejected = self._gpu_capture
                self._gpu_capture = candidate
                if rejected is not None and rejected is not candidate:
                    self._rejected_gpu_capture = rejected
                self._gpu_black_since = None
                self._capture_method = "winrt"
                with self._stats_lock:
                    self._capture_status = "GPU recovered (Windows Graphics Capture)"
                return frame
        candidate.stop()
        return None

    def _capture_mss_frame(self) -> Optional[np.ndarray]:
        """Capture on the calling worker; this also supports a GPU comparison."""
        # Fallback to mss (created here so the instance is owned by the sync
        # thread — GDI handles are thread-affine on Windows).
        if self._mss_instance is None and self._running:
            try:
                self._mss_instance = mss.mss()
            except Exception as exc:
                self._mss_instance = None
                with self._stats_lock:
                    self._last_error = f"MSS initialization failed: {exc}"
        if self._mss_instance:
            try:
                monitors = self._mss_instance.monitors
                if self.config.region:
                    monitor = {
                        "left": self.config.region[0],
                        "top": self.config.region[1],
                        "width": self.config.region[2],
                        "height": self.config.region[3],
                    }
                else:
                    monitor = _resolve_monitor(monitors, self.config.monitor)
                
                screenshot = self._mss_instance.grab(monitor)
                raw = np.frombuffer(screenshot.bgra, dtype=np.uint8).reshape(
                    screenshot.height,
                    screenshot.width,
                    4,
                )
                return _downscale_for_processing(raw, bgra=True)
            except Exception as exc:
                with self._stats_lock:
                    self._last_error = f"MSS capture failed: {exc}"
                    self._capture_status = "Capture failed"
        
        return None
    
    def _extract_color(self, image: np.ndarray) -> tuple[int, int, int]:
        """Extract dominant color using configured algorithm."""
        if self.config.color_algorithm == "auto":
            return extract_dominant_auto(image, self.config.sample_size, self.config.edge_weight)
        if self.config.color_algorithm == "histogram":
            return extract_dominant_histogram(image, self.config.sample_size)
        elif self.config.color_algorithm == "kmeans":
            return extract_dominant_kmeans(image, self.config.sample_size)
        else:
            return extract_dominant_weighted(image, self.config.sample_size, self.config.edge_weight)

    def _extract_single_color(self, image: np.ndarray) -> tuple[int, int, int]:
        """Extract a richer single-bulb ambient color."""

        if self.config.color_algorithm == "auto":
            return extract_cinematic_single_color(
                image,
                self.config.sample_size,
                self.config.edge_weight,
            )
        return self._extract_color(image)
    
    def _process_region(
        self,
        content: np.ndarray,
        region: str,
        neutral: float,
    ) -> tuple[str, tuple[int, int, int], float]:
        """Process a single region (for parallel execution)."""
        region_image = crop_relative_region(content, region)
        color = self._extract_color(region_image)
        effective_boost = adaptive_color_boost(color, self.config.color_boost)
        effective_boost = 1.0 + (effective_boost - 1.0) * (1.0 - neutral)
        enhanced = enhance_color(color, color_boost=effective_boost, min_brightness=0)
        adapted = _apply_content_adaptation(enhanced, neutral)
        return region, adapted, effective_boost

    def _extract_colors(self, image: np.ndarray) -> dict[str, tuple[int, int, int]]:
        """Extract colors from image, using parallel processing if enabled."""
        content = crop_content_area(image, ignore_letterbox=self.config.ignore_letterbox)

        # Scene-level content adaptation (B&W / dim-scene handling).
        avg_saturation, avg_luma, avg_value = _scene_stats(content)
        neutral = _neutral_strength(avg_saturation)
        brightness = _brightness_factor(avg_value)
        follow = self.config.brightness_mode == "follow"
        self._last_neutral = neutral
        with self._stats_lock:
            self._scene_neutral = neutral
            self._scene_luma = avg_luma

        # Brightness rides the setPilot dimming field, decoupled from hue.
        # Follow: scene value drives dimming, with the Dark Scene Floor slider
        # as the minimum (true black turns the bulb off below). Constant:
        # dimming is pinned so only hue/saturation track the screen. The final
        # value is set after extraction (see _finalize_output_brightness) so
        # near-black colors can taper the output instead of flashing white.
        if follow:
            floor_frac = max(10, min(60, int(self.config.min_brightness))) / 255.0
            raw_brightness = 255.0 * (floor_frac + (1.0 - floor_frac) * brightness)
        else:
            raw_brightness = float(self.config.sync_brightness)

        # A genuinely black frame: in follow mode the bulb turns OFF (payload
        # of zeros encodes state:false in the sender) — but only after the
        # black PERSISTS (~0.7s) so brief fades don't flicker the power state.
        # In constant mode the bulb HOLDS the last color instead (brightness
        # must not visibly react to content).
        is_black = avg_luma < _TRUE_BLACK_LUMA and neutral > 0.7
        now = time.perf_counter()
        if is_black:
            if self._black_since is None:
                self._black_since = now
        else:
            self._black_since = None
        if is_black and self._black_since is not None:
            if not follow or now - self._black_since < 0.7:
                # Hold both hue and brightness until follow-mode black has
                # persisted; ordinary extraction of exact black would bypass
                # the persistence guard and turn the lamp off on a scene cut.
                return {}
            if self._effective_mode == "single":
                return {"all": (0, 0, 0)}
            return {region: (0, 0, 0) for region in self.config.active_regions}

        if self._effective_mode == "single":
            color = self._extract_single_color(content)
            effective_boost = adaptive_color_boost(color, self.config.color_boost)
            effective_boost = 1.0 + (effective_boost - 1.0) * (1.0 - neutral)
            with self._stats_lock:
                self._effective_color_boost = effective_boost
            # min_brightness=0: the old floor inflated near-black noise into a
            # visible color; dimness now belongs to the dimming taper below.
            enhanced = enhance_color(color, color_boost=effective_boost, min_brightness=0)
            adapted = _apply_content_adaptation(enhanced, neutral)
            peak = max(adapted)
            if not follow and peak < 24:
                # Constant mode on a nearly-black frame: the hue is noise, and
                # brightness must not react — hold the last color instead.
                return {}
            self._finalize_output_brightness(raw_brightness, follow, peak)
            return {"all": _normalize_to_full_drive(adapted)}

        # Zone mode - process regions
        colors: dict[str, tuple[int, int, int]] = {}

        if self._executor and self.config.parallel_regions:
            # Parallel processing
            futures = [
                self._executor.submit(self._process_region, content, region, neutral)
                for region in self.config.active_regions
            ]
            boosts: list[float] = []
            # Bound the whole frame, not each zone independently. Cancel work
            # that has not started so slow workers cannot accumulate stale
            # frames in the executor queue.
            deadline = time.perf_counter() + 0.1
            for future in futures:
                try:
                    region, color, effective_boost = future.result(timeout=max(0.0, deadline - time.perf_counter()))
                    colors[region] = color
                    boosts.append(effective_boost)
                except Exception:
                    future.cancel()
            if boosts:
                with self._stats_lock:
                    self._effective_color_boost = float(np.mean(boosts))
        else:
            # Sequential processing
            boosts = []
            for region in self.config.active_regions:
                _, color, effective_boost = self._process_region(content, region, neutral)
                colors[region] = color
                boosts.append(effective_boost)
            if boosts:
                with self._stats_lock:
                    self._effective_color_boost = float(np.mean(boosts))

        peak = max((max(color) for color in colors.values()), default=0)
        if not follow and peak < 24:
            return {}
        self._finalize_output_brightness(raw_brightness, follow, peak)
        return {region: _normalize_to_full_drive(color) for region, color in colors.items()}
    
    def _finalize_output_brightness(self, raw_brightness: float, follow: bool, peak: int) -> None:
        """Finish the per-frame dimming value once the frame color is known.

        Follow mode tapers brightness with the pre-normalize color peak so a
        nearly-black scene glows faintly instead of flashing the normalized
        color at the scene-value floor, then smooths with a short EMA (~3
        frames) and quantizes to steps of 5 (deflicker + sender packet-cache
        hits). Constant mode is pinned by definition — no taper, no smoothing.
        """

        if follow:
            if peak < 48:
                raw_brightness *= max(0.0, peak) / 48.0
            prev = self._smoothed_brightness
            smoothed = raw_brightness if prev is None else prev + (raw_brightness - prev) * 0.35
            self._smoothed_brightness = smoothed
            out_brightness = int(round(smoothed / 5.0) * 5)
        else:
            self._smoothed_brightness = None
            out_brightness = int(round(raw_brightness / 5.0) * 5)
        with self._stats_lock:
            self._output_brightness = max(26, min(255, out_brightness))

    def _update_adaptive_fps(self, motion_score: float) -> None:
        """Update FPS based on motion detection."""
        if not self.config.adaptive_fps:
            self._current_fps = self.config.fps
            return
        
        # Scale FPS based on motion
        fps_range = self.config.max_fps - self.config.min_fps
        # motion_score typically 0-0.05, normalize to 0-1
        normalized_motion = min(1.0, motion_score / 0.05)
        self._current_fps = int(self.config.min_fps + fps_range * normalized_motion)

    def _target_smoothing(self, motion_score: float, dt: float) -> float:
        """Frame-rate-independent smoothing factor from a settling-time model.

        The ``smoothing`` setting maps to a settling window (0.05→40 ms …
        0.85→680 ms; default 0.2→160 ms) within which the output converges to
        ~95% of a new target, Hyperion-style, regardless of the adaptive FPS.
        The old per-frame EMA made convergence 2-3× slower at low FPS and its
        open-ended tail smeared scene changes for 500 ms+.
        """

        settle_s = max(0.03, self.config.smoothing * 0.8)
        normalized_motion = min(1.0, motion_score / max(self.config.motion_threshold * 3.0, 0.01))
        settle_s *= 1.0 - 0.55 * normalized_motion  # motion → snappier
        # Sync profile: cinema favors long, calm transitions; action favors
        # near-instant response for games / fast content.
        if self.config.sync_profile == "cinema":
            settle_s *= 1.7
        elif self.config.sync_profile == "action":
            settle_s *= 0.45
        dt = max(0.005, min(0.5, dt))
        if settle_s <= dt:
            return 1.0
        # tau = settle/3 → ~95% converged at the settling window.
        factor = 1.0 - float(np.exp(-3.0 * dt / settle_s))
        return max(0.05, min(1.0, factor))

    def _prediction_weight(self, motion_score: float) -> float:
        """Reduce prediction during scene cuts or very high motion."""

        if not self.config.predictive_smoothing:
            return 0.0

        normalized_motion = min(1.0, motion_score / max(self.config.motion_threshold * 4.0, 0.02))
        return self.config.prediction_weight * max(0.0, 1.0 - normalized_motion)

    def _send_delta_threshold(self, motion_score: float) -> int:
        """Raise send threshold during static scenes and lower it during motion."""

        base = max(1, int(self.config.min_color_delta))
        if not self.config.adaptive_fps:
            return base
        motion_factor = min(1.0, motion_score / max(self.config.motion_threshold * 4.0, 0.02))
        extra_cap = {"cinema": 8, "action": 2}.get(self.config.sync_profile, 6)
        static_extra = int((1.0 - motion_factor) * extra_cap)
        return max(base, base + static_extra)
    
    def _sync_loop(self) -> None:
        """Own and release thread-affine capture resources on this worker."""
        try:
            self._sync_loop_inner()
        finally:
            if self._gpu_capture and getattr(self._gpu_capture, "backend", "dxgi") == "winrt":
                capture_gpu, self._gpu_capture = self._gpu_capture, None
                capture_gpu.stop()
            capture, self._mss_instance = self._mss_instance, None
            if capture is not None:
                try:
                    capture.close()
                except Exception as exc:
                    with self._stats_lock:
                        self._last_error = f"MSS cleanup failed: {exc}"

    def _sync_loop_inner(self) -> None:
        """Main sync loop with all optimizations."""
        while self._running:
            frame_start = time.perf_counter()
            image = None
            capture_end = None
            
            try:
                # Capture frame
                image = self._capture_frame()
                capture_end = time.perf_counter()
                if image is None:
                    time.sleep(0.1)
                    continue
                
                # Detect motion / scene cuts and update adaptive FPS
                now = time.perf_counter()
                dt = (
                    1.0 / max(1, self._current_fps)
                    if self._last_loop_time is None
                    else now - self._last_loop_time
                )
                self._last_loop_time = now
                motion_score = self._motion_detector.update(image)
                scene_cut = self._motion_detector.last_scene_cut
                self._update_adaptive_fps(motion_score)
                smoothing_factor = 1.0 if scene_cut else self._target_smoothing(motion_score, dt)
                prediction_weight = 0.0 if scene_cut else self._prediction_weight(motion_score)
                if scene_cut:
                    # A cut invalidates velocity history; snap instead of smearing.
                    self._color_predictor.clear()
                with self._stats_lock:
                    self._motion_score = motion_score
                    self._smoothing_factor = smoothing_factor
                    self._prediction_strength = prediction_weight
                    self._frames_processed += 1
                    self._last_scene_cut = scene_cut
                    if scene_cut:
                        self._cuts_detected += 1

                # Extract colors
                target_colors = self._extract_colors(image)
                with self._stats_lock:
                    self._last_target_colors = dict(target_colors)
                changed_colors: dict[str, tuple[int, int, int]] = {}
                send_threshold = self._send_delta_threshold(motion_score)
                with self._stats_lock:
                    self._send_delta_threshold_value = send_threshold
                for key, target in target_colors.items():
                    black_off = (
                        self.config.brightness_mode == "follow"
                        and self._black_since is not None
                        and now - self._black_since >= 0.7
                        and target == (0, 0, 0)
                    )
                    with self._stats_lock:
                        current = self._current_colors.get(key, target)
                        previous = self._current_colors.get(key)
                        had_current = key in self._current_colors

                    observed_target = target
                    if self._effective_mode == "single" and key == "all":
                        # No hold on cuts, in action profile (kills responsiveness),
                        # or on neutral scenes (holding tints B&W content).
                        skip_hold = (
                            scene_cut
                            or self.config.sync_profile == "action"
                            or self._last_neutral > 0.5
                        )
                        hold_previous = None if skip_hold else previous
                        target = apply_cinematic_palette_hold(target, hold_previous, motion_score)

                    # Remember the unpredicted scene target; feeding the
                    # smoothed output back into prediction creates momentum
                    # even after the scene has stopped or reversed.
                    # Apply predictive smoothing
                    if prediction_weight > 0.0 and not black_off:
                        predicted = self._color_predictor.predict(key, target)
                        # Blend predicted with target
                        target = tuple(
                            int(target[c] * (1.0 - prediction_weight) + predicted[c] * prediction_weight)
                            for c in range(3)
                        )

                    # Smooth transition — perceptually-uniform Oklab lerp with a
                    # hard snap once within ~0.4 JND (kills the EMA's long tail).
                    if black_off or not had_current or smoothing_factor >= 1.0:
                        smoothed = tuple(int(v) for v in target)
                    else:
                        smoothed = lerp_oklab8(current, target, smoothing_factor)
                        if oklab_distance8(smoothed, target) * OKLAB_DELTA_SCALE < 1.6:
                            smoothed = tuple(int(v) for v in target)

                    with self._stats_lock:
                        self._current_colors[key] = smoothed
                    self._color_predictor.add_sample(key, observed_target)

                    # Send gate: perceptual delta, bypassed on scene cuts, with a
                    # periodic force-refresh so slow drifts still reach the bulb.
                    with self._stats_lock:
                        last_sent = self._last_sent_colors.get(key)
                        last_sent_at = self._last_color_send_at.get(key)
                    if last_sent is None or scene_cut:
                        delta = float("inf")
                    else:
                        delta = oklab_distance8(smoothed, last_sent) * OKLAB_DELTA_SCALE
                    # UDP has no delivery acknowledgement. Refresh even an
                    # unchanged scene so a lost burst or a reconnected bulb
                    # does not remain on the wrong color indefinitely.
                    needs_forced = last_sent_at is not None and now - last_sent_at >= 1.5
                    if delta >= send_threshold or needs_forced:
                        changed_colors[key] = smoothed
                
                # Clean up stale keys — but NOT when target_colors is empty on
                # purpose (constant-mode black hold): wiping state there would
                # force a resend/snap on every un-black transition.
                if target_colors:
                    with self._stats_lock:
                        # A slow/failed zone may be absent from this frame;
                        # keep its previous color until it can be computed.
                        valid_keys = (set(self.config.active_regions) if self._effective_mode == "zones"
                                      else set(target_colors))
                        stale_keys = set(self._current_colors) - valid_keys
                        for key in stale_keys:
                            self._current_colors.pop(key, None)
                            self._last_sent_colors.pop(key, None)
                            self._last_color_send_at.pop(key, None)

                # Brightness rides the dimming field: when it moves >=5 while
                # the color holds steady (e.g. a slow fade in follow mode), the
                # colors must be resent anyway or the bulb never dims.
                with self._stats_lock:
                    out_brightness = self._output_brightness
                    last_sent_brightness = self._last_sent_brightness
                if (
                    target_colors
                    and last_sent_brightness is not None
                    and abs(out_brightness - last_sent_brightness) >= 5
                ):
                    with self._stats_lock:
                        # Dimming is shared by all zones. A hue change in one
                        # zone must not prevent steady zones receiving it too.
                        changed_colors.update({
                            key: self._current_colors[key]
                            for key in target_colors
                            if key in self._current_colors
                        })

                # Send changes
                if changed_colors:
                    self.on_color_change(changed_colors)
                    now = time.perf_counter()
                    with self._stats_lock:
                        # Only advance the send gate once the callback accepts
                        # the update; a synchronous failure must be retried.
                        self._last_sent_colors.update(changed_colors)
                        self._last_color_send_at.update(dict.fromkeys(changed_colors, now))
                        if self._last_send_time is not None:
                            self._send_intervals.append(now - self._last_send_time)
                        self._last_send_time = now
                        self._updates_sent += 1
                        self._last_error = None
                        self._last_sent_brightness = out_brightness
            except Exception as exc:
                print(f"Screen sync error: {exc}")
                with self._stats_lock:
                    self._last_error = str(exc)
            
            # Frame timing
            frame_time = time.perf_counter() - frame_start
            with self._stats_lock:
                self._frame_times.append(frame_time)
                if image is not None and capture_end is not None:
                    self._capture_times.append(capture_end - frame_start)
                    self._processing_times.append(max(0.0, frame_time - (capture_end - frame_start)))
            
            target_frame_time = 1.0 / self._current_fps
            if frame_time < target_frame_time:
                time.sleep(target_frame_time - frame_time)
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    @property
    def effective_mode(self) -> str:
        return self._effective_mode
    
    @property
    def current_colors(self) -> dict[str, tuple[int, int, int]]:
        with self._stats_lock:
            return dict(self._current_colors)

    @property
    def debug_snapshot(self) -> dict[str, object]:
        """Return a thread-safe snapshot of runtime sync stats for the UI."""

        with self._stats_lock:
            frame_times = tuple(self._frame_times)
            capture_times = tuple(self._capture_times)
            processing_times = tuple(self._processing_times)
            send_intervals = tuple(self._send_intervals)
            target_colors = dict(self._last_target_colors)
            current_colors = dict(self._current_colors)
            avg_frame_time_ms = float(np.mean(frame_times) * 1000) if frame_times else 0.0
            avg_send_interval_ms = float(np.mean(send_intervals) * 1000) if send_intervals else 0.0
            send_rate_hz = float(1.0 / np.mean(send_intervals)) if send_intervals else 0.0
            requested_fps = self.config.max_fps if self.config.adaptive_fps else self.config.fps
            return {
                "running": self._running,
                "mode": self._effective_mode,
                "capture_method": self._capture_method,
                "capture_status": self._capture_status,
                "requested_fps": requested_fps,
                "requestedFps": requested_fps,
                "current_fps": self._current_fps,
                "currentFps": self._current_fps,
                "average_frame_time_ms": avg_frame_time_ms,
                "frameTimeMs": avg_frame_time_ms,
                "capture_time_ms": float(np.mean(capture_times) * 1000) if capture_times else None,
                "capture_time_p95_ms": float(np.percentile(capture_times, 95) * 1000) if capture_times else None,
                "processing_time_ms": float(np.mean(processing_times) * 1000) if processing_times else None,
                "processing_time_p95_ms": float(np.percentile(processing_times, 95) * 1000) if processing_times else None,
                "timing_samples": len(capture_times),
                "motion_score": self._motion_score,
                "motionScore": self._motion_score,
                "smoothing_factor": self._smoothing_factor,
                "smoothingFactor": self._smoothing_factor,
                "prediction_weight": self._prediction_strength,
                "effective_color_boost": self._effective_color_boost,
                "effectiveColorBoost": self._effective_color_boost,
                "min_brightness": self.config.min_brightness,
                "minBrightness": self.config.min_brightness,
                "delta_threshold": self._send_delta_threshold_value,
                "deltaThreshold": self._send_delta_threshold_value,
                "captureBackend": self._capture_method,
                "target_colors": target_colors,
                "targetColors": target_colors,
                "current_colors": current_colors,
                "outputColors": current_colors,
                "send_interval_ms": avg_send_interval_ms,
                "send_rate_hz": send_rate_hz,
                "sendRateHz": send_rate_hz,
                "updates_sent": self._updates_sent,
                "updatesSent": self._updates_sent,
                "frames_processed": self._frames_processed,
                "framesProcessed": self._frames_processed,
                "scene_cut": self._last_scene_cut,
                "sceneCut": self._last_scene_cut,
                "cuts_detected": self._cuts_detected,
                "cutsDetected": self._cuts_detected,
                "scene_neutral": self._scene_neutral,
                "sceneNeutral": self._scene_neutral,
                "scene_luma": self._scene_luma,
                "sceneLuma": self._scene_luma,
                "sync_profile": self.config.sync_profile,
                "syncProfile": self.config.sync_profile,
                "brightness_mode": self.config.brightness_mode,
                "brightnessMode": self.config.brightness_mode,
                "output_brightness": self._output_brightness,
                "outputBrightness": self._output_brightness,
                "capture_looks_black": self._capture_looks_black_locked(),
                "captureLooksBlack": self._capture_looks_black_locked(),
                "last_error": self._last_error,
                "lastError": self._last_error,
            }
    
    def _capture_looks_black_locked(self) -> bool:
        """True when capture has been solid black for >3s while running.

        Black frames may represent dark content or inaccessible protected or
        overlay video. This signal cannot distinguish between those cases.
        """

        return self._running and self._black_since is not None and (
            time.perf_counter() - self._black_since > 3.0
        )

    @property
    def capture_looks_black(self) -> bool:
        with self._stats_lock:
            return self._capture_looks_black_locked()

    @property
    def output_brightness(self) -> int:
        """Current bulb brightness (0-255) to send as the setPilot dimming."""

        with self._stats_lock:
            return self._output_brightness

    @property
    def current_fps(self) -> int:
        return self._current_fps

    @property
    def capture_method(self) -> str:
        return self._capture_method
    
    @property
    def average_frame_time_ms(self) -> float:
        if not self._frame_times:
            return 0.0
        return float(np.mean(self._frame_times) * 1000)
