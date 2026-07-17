"""Optimized screen capture and color extraction with GPU acceleration and adaptive FPS."""

import gc
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
    brightness_follow: bool = True   # bulb brightness tracks scene luma

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
        self.brightness_follow = bool(self.brightness_follow)


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


def _sample_pixels(
    image: np.ndarray,
    sample_size: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Downsample once and return pixel arrays for shared color analysis.

    The hot path only needs a stable color sample, not a high-quality image
    resize. Strided sampling avoids PIL object creation and large resize
    allocations while keeping the existing cinematic algorithm intact.
    """

    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        pixels = np.zeros((sample_size, sample_size, 3), dtype=np.float32)
        return pixels, pixels

    step_y = max(1, height // sample_size)
    step_x = max(1, width // sample_size)
    sampled = image[::step_y, ::step_x, :3][:sample_size, :sample_size]
    if sampled.shape[0] < sample_size or sampled.shape[1] < sample_size:
        padded = np.empty((sample_size, sample_size, 3), dtype=image.dtype)
        padded[:] = sampled[-1:, -1:, :] if sampled.size else 0
        padded[: sampled.shape[0], : sampled.shape[1], :] = sampled
        sampled = padded

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
_BRIGHTNESS_FOLLOW_KNEE = 0.32
_TRUE_BLACK_LUMA = 0.015


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
    """Scene value (avg max-channel) → output brightness factor (1.0 at knee)."""

    return max(0.0, min(1.0, avg_value / _BRIGHTNESS_FOLLOW_KNEE)) ** 0.8


def _apply_content_adaptation(
    color: tuple[int, int, int],
    neutral: float,
    brightness: float,
    follow: bool,
) -> tuple[int, int, int]:
    """Desaturate neutral scenes toward true gray and track scene brightness.

    Neutral scenes must land on the bulb's white channels (the WiZ mapper
    routes low-saturation colors to CW/WW), so B&W film reads as clean white
    instead of an amplified color cast. Brightness-follow scales dim scenes
    down instead of pinning everything at the minimum-brightness floor.
    """

    r, g, b = color
    if neutral > 0.0:
        gray = int(round(r * 0.2126 + g * 0.7152 + b * 0.0722))
        r = int(round(r + (gray - r) * neutral))
        g = int(round(g + (gray - g) * neutral))
        b = int(round(b + (gray - b) * neutral))
    if follow:
        scale = 0.35 + 0.65 * brightness
        r = int(round(r * scale))
        g = int(round(g * scale))
        b = int(round(b * scale))
    return (
        max(0, min(255, r)),
        max(0, min(255, g)),
        max(0, min(255, b)),
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


def extract_vibrant_accent(
    image: np.ndarray,
    sample_size: int = 48,
) -> tuple[int, int, int]:
    """Extract a vivid accent color from the most energetic pixels in the frame."""

    pixels, normalized = _sample_pixels(image, sample_size)
    saturation, luma = _scene_channels(normalized)
    return _extract_vibrant_accent_from_sample(pixels, saturation, luma)


def extract_palette_anchor(
    image: np.ndarray,
    sample_size: int = 48,
) -> tuple[int, int, int]:
    """Extract a tighter palette anchor from the most color-rich pixels."""

    pixels, normalized = _sample_pixels(image, sample_size)
    saturation, luma = _scene_channels(normalized)
    return _extract_palette_anchor_from_sample(pixels, saturation, luma)


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
    )


# Downscale captured frames to at most this width BEFORE any per-frame copy /
# color work. A 4K (3840px) BGRA->RGB conversion cost ~39 ms/frame — the entire
# latency budget — yet extraction only samples ~60px. Working at ~640px drops
# that to ~1 ms with no measurable color-accuracy change (extraction strides
# either way), taking 4K sync from ~3 fps to the FPS cap.
_PROCESS_MAX_WIDTH = 640


def _downscale_for_processing(frame: np.ndarray) -> np.ndarray:
    """Strided downscale (view) so the max width is ~_PROCESS_MAX_WIDTH."""
    w = frame.shape[1]
    if w <= _PROCESS_MAX_WIDTH:
        return frame
    step = max(1, w // _PROCESS_MAX_WIDTH)
    return frame[::step, ::step]


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

    def __init__(self, device: int = 0, monitor: int = 0):
        self._camera = None
        self._device = device
        self._monitor = monitor
        self._lock = threading.Lock()
        self._last_frame: Optional[np.ndarray] = None

    def start(self, target_fps: int = 30) -> bool:
        """Initialize GPU capture. target_fps kept for caller compatibility."""
        if not HAS_DXCAM:
            return False

        with self._lock:
            try:
                # output_color="BGRA" keeps dxcam on the raw DXGI byte order and
                # avoids its cv2-based color converter entirely — the frozen
                # (PyInstaller) build excludes OpenCV, where the default "RGB"
                # mode made every grab() raise ModuleNotFoundError('cv2').
                # We slice BGRA -> RGB ourselves in grab().
                self._camera = dxcam.create(
                    device_idx=self._device,
                    output_idx=self._monitor,
                    output_color="BGRA",
                )
                if self._camera is None:
                    return False
                # Prime the cache so the first static-screen grab has pixels —
                # and verify grabbing actually works in this environment.
                probe = self._camera.grab()
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
        # Downscale (cheap strided view) BEFORE the channel-reverse copy so we
        # never allocate/convert a full 4K buffer per frame.
        frame = _downscale_for_processing(frame)
        if frame.ndim == 3 and frame.shape[2] == 4:
            return np.ascontiguousarray(frame[:, :, 2::-1])
        return np.ascontiguousarray(frame[:, :, :3])

    def grab(self) -> Optional[np.ndarray]:
        """Capture a frame. Returns RGB numpy array or None (never blocks)."""
        if self._camera is None:
            return None

        try:
            frame = self._camera.grab()
        except Exception:
            return self._last_frame
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


def should_use_gpu_capture(config: OptimizedCaptureConfig) -> bool:
    """Return whether DXCam can honor the selected capture target reliably."""

    if not HAS_DXCAM or not config.use_gpu:
        return False
    if config.region is not None:
        return False
    return int(config.monitor) > 0


def _stride_sample_rgb(image: np.ndarray, sample_size: int) -> np.ndarray:
    """Strided RGB downsample to (sample_size, sample_size, 3) — no PIL.

    Matches the hot-path auto extractor's sampling so the optional
    weighted/histogram/kmeans algorithms avoid PIL object creation and large
    resize allocations.
    """

    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        return np.zeros((sample_size, sample_size, 3), dtype=np.uint8)

    step_y = max(1, height // sample_size)
    step_x = max(1, width // sample_size)
    sampled = image[::step_y, ::step_x, :3][:sample_size, :sample_size]
    if sampled.shape[0] < sample_size or sampled.shape[1] < sample_size:
        padded = np.empty((sample_size, sample_size, 3), dtype=image.dtype)
        padded[:] = sampled[-1:, -1:, :] if sampled.size else 0
        padded[: sampled.shape[0], : sampled.shape[1], :] = sampled
        sampled = padded
    return np.ascontiguousarray(sampled[:, :, :3])


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

    Scene cuts are detected via luma-histogram total-variation distance
    (PySceneDetect-style): panning/motion shifts the histogram slowly, a cut
    replaces it. Callers use ``last_scene_cut`` to bypass smoothing/gating for
    one frame so cuts land instantly instead of smearing through the EMA.
    """

    _CUT_HIST_BINS = 32
    _CUT_HIST_THRESHOLD = 0.30  # total-variation distance (0..1) that counts as a cut
    _CUT_DIFF_THRESHOLD = 0.10  # instantaneous frame diff required alongside it

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

        if self._last_frame is None:
            self._last_frame = small
            self.last_scene_cut = False
            return 0.5  # Default to medium motion on first frame

        # Calculate frame difference
        diff = np.abs(small - self._last_frame).mean() / 255.0
        self._last_frame = small
        self._motion_history.append(diff)

        self.last_scene_cut = (
            hist_tv > self._CUT_HIST_THRESHOLD and diff > self._CUT_DIFF_THRESHOLD
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
        
        # Predict next color
        predicted = tuple(
            int(np.clip(current[c] + avg_vel[c] * self._weight, 0, 255))
            for c in range(3)
        )
        
        return predicted
    
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
        self._last_forced_send_at = 0.0
        self._cuts_detected = 0
        self._last_scene_cut = False
        self._last_neutral = 0.0
        self._scene_neutral = 0.0
        self._scene_luma = 0.0
        self._black_since: Optional[float] = None

        # Optimization components
        self._gpu_capture: Optional[GPUCaptureManager] = None
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
        self._capture_method = "mss"
        self._capture_status = "Ready"
        self._dxcam_recovery_events_seen = 0
        self._dxcam_recovery_status_until = 0.0
    
    def start(self) -> None:
        """Start optimized screen sync."""
        if self._running:
            return
        
        self._running = True
        self._motion_detector.reset()
        self._color_predictor.clear()
        self._current_fps = self.config.max_fps if self.config.adaptive_fps else self.config.fps
        with self._stats_lock:
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
            self._last_forced_send_at = 0.0
            self._cuts_detected = 0
            self._last_scene_cut = False
            self._last_neutral = 0.0
            self._scene_neutral = 0.0
            self._scene_luma = 0.0
            self._black_since = None
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
            self._thread.join(timeout=2.0)
            self._thread = None
        
        if self._gpu_capture:
            self._gpu_capture.stop()
            self._gpu_capture = None
        
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None
        
        if self._mss_instance:
            self._mss_instance.close()
            self._mss_instance = None
    
    def _capture_frame(self) -> Optional[np.ndarray]:
        """Capture a frame using best available method."""
        # Try GPU capture first
        if self._gpu_capture and self._gpu_capture.is_active:
            frame = self._gpu_capture.grab()
            recovery_events, _ = _dxcam_recovery_state()
            if recovery_events > self._dxcam_recovery_events_seen:
                self._dxcam_recovery_events_seen = recovery_events
                self._dxcam_recovery_status_until = time.perf_counter() + 4.0
                with self._stats_lock:
                    self._capture_status = "GPU capture recovered"
            if frame is not None:
                if time.perf_counter() > self._dxcam_recovery_status_until:
                    with self._stats_lock:
                        self._capture_status = "OK"
                return frame
        
        # Fallback to mss (created here so the instance is owned by the sync
        # thread — GDI handles are thread-affine on Windows).
        if self._mss_instance is None and self._running:
            try:
                self._mss_instance = mss.mss()
            except Exception:
                self._mss_instance = None
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
                if time.perf_counter() > self._dxcam_recovery_status_until:
                    with self._stats_lock:
                        self._capture_status = "OK"
                # Downscale first (view) so the RGB copy is on a small array.
                raw = _downscale_for_processing(raw)
                return np.ascontiguousarray(raw[:, :, 2::-1])
            except Exception:
                pass
        
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
        brightness: float,
        follow: bool,
    ) -> tuple[str, tuple[int, int, int], float]:
        """Process a single region (for parallel execution)."""
        region_image = crop_relative_region(content, region)
        color = self._extract_color(region_image)
        effective_boost = adaptive_color_boost(color, self.config.color_boost)
        effective_boost = 1.0 + (effective_boost - 1.0) * (1.0 - neutral)
        floor = int(self.config.min_brightness * (brightness if follow else 1.0))
        enhanced = enhance_color(
            color,
            color_boost=effective_boost,
            min_brightness=floor,
        )
        adapted = _apply_content_adaptation(enhanced, neutral, brightness, follow)
        return region, adapted, effective_boost

    def _extract_colors(self, image: np.ndarray) -> dict[str, tuple[int, int, int]]:
        """Extract colors from image, using parallel processing if enabled."""
        content = crop_content_area(image, ignore_letterbox=self.config.ignore_letterbox)

        # Scene-level content adaptation (B&W / dim-scene handling).
        avg_saturation, avg_luma, avg_value = _scene_stats(content)
        neutral = _neutral_strength(avg_saturation)
        brightness = _brightness_factor(avg_value)
        follow = self.config.brightness_follow
        self._last_neutral = neutral
        with self._stats_lock:
            self._scene_neutral = neutral
            self._scene_luma = avg_luma

        # A genuinely black frame turns the bulb off (payload of zeros encodes
        # state:false in the sender) — but only after the black PERSISTS
        # (~0.7s) so brief fades don't flicker the bulb's power state.
        is_black = avg_luma < _TRUE_BLACK_LUMA and neutral > 0.7
        now = time.perf_counter()
        if is_black:
            if self._black_since is None:
                self._black_since = now
        else:
            self._black_since = None
        if follow and is_black and self._black_since is not None and now - self._black_since >= 0.7:
            if self._effective_mode == "single":
                return {"all": (0, 0, 0)}
            return {region: (0, 0, 0) for region in self.config.active_regions}

        if self._effective_mode == "single":
            color = self._extract_single_color(content)
            effective_boost = adaptive_color_boost(color, self.config.color_boost)
            effective_boost = 1.0 + (effective_boost - 1.0) * (1.0 - neutral)
            floor = int(self.config.min_brightness * (brightness if follow else 1.0))
            with self._stats_lock:
                self._effective_color_boost = effective_boost
            enhanced = enhance_color(
                color,
                color_boost=effective_boost,
                min_brightness=floor,
            )
            return {"all": _apply_content_adaptation(enhanced, neutral, brightness, follow)}

        # Zone mode - process regions
        colors: dict[str, tuple[int, int, int]] = {}

        if self._executor and self.config.parallel_regions:
            # Parallel processing
            futures = [
                self._executor.submit(
                    self._process_region, content, region, neutral, brightness, follow
                )
                for region in self.config.active_regions
            ]
            boosts: list[float] = []
            for future in futures:
                try:
                    region, color, effective_boost = future.result(timeout=0.1)
                    colors[region] = color
                    boosts.append(effective_boost)
                except Exception:
                    pass
            if boosts:
                with self._stats_lock:
                    self._effective_color_boost = float(np.mean(boosts))
        else:
            # Sequential processing
            boosts = []
            for region in self.config.active_regions:
                _, color, effective_boost = self._process_region(
                    content, region, neutral, brightness, follow
                )
                colors[region] = color
                boosts.append(effective_boost)
            if boosts:
                with self._stats_lock:
                    self._effective_color_boost = float(np.mean(boosts))

        return colors
    
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
        """Main sync loop with all optimizations."""
        while self._running:
            frame_start = time.perf_counter()
            
            try:
                # Capture frame
                image = self._capture_frame()
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
                force_refresh_due = now - self._last_forced_send_at >= 1.5

                for key, target in target_colors.items():
                    with self._stats_lock:
                        current = self._current_colors.get(key, target)
                        previous = self._current_colors.get(key)
                        had_current = key in self._current_colors

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

                    # Apply predictive smoothing
                    if prediction_weight > 0.0:
                        predicted = self._color_predictor.predict(key, target)
                        # Blend predicted with target
                        target = tuple(
                            int(target[c] * (1.0 - prediction_weight) + predicted[c] * prediction_weight)
                            for c in range(3)
                        )

                    # Smooth transition — perceptually-uniform Oklab lerp with a
                    # hard snap once within ~0.4 JND (kills the EMA's long tail).
                    if not had_current or smoothing_factor >= 1.0:
                        smoothed = tuple(int(v) for v in target)
                    else:
                        smoothed = lerp_oklab8(current, target, smoothing_factor)
                        if oklab_distance8(smoothed, target) * OKLAB_DELTA_SCALE < 1.6:
                            smoothed = tuple(int(v) for v in target)

                    with self._stats_lock:
                        self._current_colors[key] = smoothed
                    self._color_predictor.add_sample(key, smoothed)

                    # Send gate: perceptual delta, bypassed on scene cuts, with a
                    # periodic force-refresh so slow drifts still reach the bulb.
                    with self._stats_lock:
                        last_sent = self._last_sent_colors.get(key)
                    if last_sent is None or scene_cut:
                        delta = float("inf")
                    else:
                        delta = oklab_distance8(smoothed, last_sent) * OKLAB_DELTA_SCALE
                    needs_forced = (
                        force_refresh_due
                        and last_sent is not None
                        and delta >= max(2.0, send_threshold / 2.0)
                    )
                    if delta >= send_threshold or needs_forced:
                        with self._stats_lock:
                            self._last_sent_colors[key] = smoothed
                        changed_colors[key] = smoothed
                
                # Clean up stale keys
                with self._stats_lock:
                    stale_keys = set(self._current_colors) - set(target_colors)
                    for key in stale_keys:
                        self._current_colors.pop(key, None)
                        self._last_sent_colors.pop(key, None)
                
                # Send changes
                if changed_colors:
                    self.on_color_change(changed_colors)
                    now = time.perf_counter()
                    with self._stats_lock:
                        if self._last_send_time is not None:
                            self._send_intervals.append(now - self._last_send_time)
                        self._last_send_time = now
                        self._updates_sent += 1
                        self._last_error = None
                if changed_colors or self._last_forced_send_at == 0.0:
                    self._last_forced_send_at = time.perf_counter()
                
            except Exception as exc:
                print(f"Screen sync error: {exc}")
                with self._stats_lock:
                    self._last_error = str(exc)
            
            # Frame timing
            frame_time = time.perf_counter() - frame_start
            with self._stats_lock:
                self._frame_times.append(frame_time)
            
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
                "brightness_follow": self.config.brightness_follow,
                "brightnessFollow": self.config.brightness_follow,
                "last_error": self._last_error,
                "lastError": self._last_error,
            }
    
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


def is_gpu_capture_available() -> bool:
    """Check if GPU capture is available on this system."""
    if not HAS_DXCAM:
        return False
    try:
        cam = dxcam.create()
        if cam is not None:
            try:
                cam.release()
            except Exception:
                pass
            del cam
            return True
    except Exception:
        pass
    return False
