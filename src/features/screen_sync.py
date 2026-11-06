"""Screen capture and color extraction for screen sync feature."""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

import mss
import numpy as np
from PIL import Image


SCREEN_REGIONS: dict[str, tuple[float, float, float, float]] = {
    # These regions intentionally bias toward screen edges rather than full thirds.
    # Ambient lighting feels more accurate when it reacts to border colors, not UI-heavy centers.
    "left": (0.0, 0.08, 0.22, 0.84),
    "center": (0.22, 0.12, 0.56, 0.76),
    "right": (0.78, 0.08, 0.22, 0.84),
    "top-left": (0.0, 0.0, 0.34, 0.28),
    "top": (0.22, 0.0, 0.56, 0.24),
    "top-right": (0.66, 0.0, 0.34, 0.28),
    "bottom-left": (0.0, 0.72, 0.34, 0.28),
    "bottom": (0.22, 0.76, 0.56, 0.24),
    "bottom-right": (0.66, 0.72, 0.34, 0.28),
}

_MIN_CROP_PIXELS = 4


@dataclass
class CaptureConfig:
    """Configuration for screen capture."""

    mode: str = "single"
    monitor: int = 1  # 0 = all monitors, 1+ = specific monitor
    fps: int = 12
    sample_size: int = 48
    region: Optional[tuple[int, int, int, int]] = None
    ignore_letterbox: bool = True
    edge_weight: float = 1.35
    color_boost: float = 1.15
    min_brightness: int = 28
    min_color_delta: int = 12
    active_regions: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.mode = self.mode if self.mode in {"single", "zones"} else "single"
        self.monitor = int(self.monitor)
        self.fps = max(4, min(30, int(self.fps)))
        self.sample_size = max(16, min(96, int(self.sample_size)))
        self.edge_weight = max(1.0, float(self.edge_weight))
        self.color_boost = max(1.0, float(self.color_boost))
        self.min_brightness = max(0, min(255, int(self.min_brightness)))
        self.min_color_delta = max(1, min(255, int(self.min_color_delta)))
        self.active_regions = tuple(
            region for region in self.active_regions if region in SCREEN_REGIONS
        )


def list_monitors() -> list[dict[str, int | str]]:
    """Return available monitor choices for the UI."""

    with mss.mss() as sct:
        monitors = sct.monitors
        options: list[dict[str, int | str]] = []

        if len(monitors) > 1:
            combined = monitors[0]
            options.append(
                {
                    "index": 0,
                    "label": f"All monitors ({combined['width']}x{combined['height']})",
                }
            )
            for index, monitor in enumerate(monitors[1:], start=1):
                options.append(
                    {
                        "index": index,
                        "label": f"Monitor {index} ({monitor['width']}x{monitor['height']})",
                    }
                )
        elif monitors:
            monitor = monitors[0]
            options.append(
                {
                    "index": 0,
                    "label": f"Primary monitor ({monitor['width']}x{monitor['height']})",
                }
            )

    return options


def resolve_active_regions(
    bulb_layout: Mapping[str, str],
    bulb_ips: Sequence[str],
) -> tuple[str, ...]:
    """Resolve configured bulb regions in screen order."""

    configured = {
        bulb_layout[ip]
        for ip in bulb_ips
        if bulb_layout.get(ip) in SCREEN_REGIONS
    }
    ordered = tuple(region for region in SCREEN_REGIONS if region in configured)
    return ordered if len(ordered) >= 2 else ()


def effective_screen_sync_mode(mode: str, active_regions: Sequence[str]) -> str:
    """Return the effective runtime mode after applying fallbacks."""

    if mode == "zones" and len(tuple(active_regions)) >= 2:
        return "zones"
    return "single"


def _resolve_monitor(monitors: list[dict], monitor_index: int) -> dict:
    if len(monitors) == 1:
        return monitors[0]
    if monitor_index == 0:
        return monitors[0]
    return monitors[min(max(1, monitor_index), len(monitors) - 1)]



def detect_content_bounds(
    image: np.ndarray,
    threshold: int = 16,
    min_active_ratio: float = 0.02,
) -> tuple[int, int, int, int]:
    """Detect the active content area and ignore letterbox bars when possible."""

    height, width = image.shape[:2]
    if height < 8 or width < 8:
        return (0, 0, width, height)

    pixels = image.astype(np.float32)
    luma = pixels[:, :, 0] * 0.2126 + pixels[:, :, 1] * 0.7152 + pixels[:, :, 2] * 0.0722
    row_activity = (luma > threshold).mean(axis=1)
    col_activity = (luma > threshold).mean(axis=0)

    active_rows = np.where(row_activity > min_active_ratio)[0]
    active_cols = np.where(col_activity > min_active_ratio)[0]
    if active_rows.size == 0 or active_cols.size == 0:
        return (0, 0, width, height)

    top = int(active_rows[0])
    bottom = int(active_rows[-1] + 1)
    left = int(active_cols[0])
    right = int(active_cols[-1] + 1)

    if (bottom - top) < height * 0.45 or (right - left) < width * 0.45:
        return (0, 0, width, height)

    return (left, top, right - left, bottom - top)


def crop_content_area(image: np.ndarray, ignore_letterbox: bool = True) -> np.ndarray:
    """Crop black bars and side bars from the capture when configured."""

    if not ignore_letterbox:
        return image

    left, top, width, height = detect_content_bounds(image)
    if width < _MIN_CROP_PIXELS or height < _MIN_CROP_PIXELS:
        return image

    cropped = image[top : top + height, left : left + width]
    if cropped.size == 0:
        return image
    return cropped


def crop_relative_region(image: np.ndarray, region_name: str) -> np.ndarray:
    """Crop a logical region from the image using relative bounds."""

    if region_name not in SCREEN_REGIONS:
        return image

    rel_left, rel_top, rel_width, rel_height = SCREEN_REGIONS[region_name]
    height, width = image.shape[:2]
    left = int(width * rel_left)
    top = int(height * rel_top)
    right = max(left + 1, int(width * (rel_left + rel_width)))
    bottom = max(top + 1, int(height * (rel_top + rel_height)))
    return image[top:bottom, left:right]


def extract_dominant_color(
    image: np.ndarray,
    sample_size: int = 48,
    edge_weight: float = 1.35,
) -> tuple[int, int, int]:
    """Extract a weighted ambient color from an RGB image."""

    img = Image.fromarray(image)
    img = img.resize((sample_size, sample_size), Image.Resampling.BILINEAR)
    pixels = np.array(img).astype(np.float32)

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

    avg_color = (pixels * weights[:, :, None]).sum(axis=(0, 1)) / weights.sum()
    return tuple(int(channel) for channel in avg_color[:3])


def extract_dominant_color_kmeans(
    image: np.ndarray,
    sample_size: int = 50,
    edge_weight: float = 1.35,
    k: int = 3,
) -> tuple[int, int, int]:
    """Extract dominant color using a small k-means cluster pass."""

    del edge_weight

    img = Image.fromarray(image)
    img = img.resize((sample_size, sample_size), Image.Resampling.BILINEAR)
    pixels = np.array(img).reshape(-1, 3).astype(np.float32)

    if len(pixels) == 0:
        return (0, 0, 0)

    k = max(1, min(int(k), len(pixels)))
    rng = np.random.default_rng(42)
    indices = rng.choice(len(pixels), k, replace=False)
    centroids = pixels[indices]

    for _ in range(10):
        distances = np.sqrt(((pixels[:, np.newaxis] - centroids) ** 2).sum(axis=2))
        labels = distances.argmin(axis=1)
        new_centroids = np.array(
            [
                pixels[labels == i].mean(axis=0) if (labels == i).any() else centroids[i]
                for i in range(k)
            ]
        )
        if np.allclose(centroids, new_centroids):
            break
        centroids = new_centroids

    counts = np.bincount(labels, minlength=k)
    return tuple(int(channel) for channel in centroids[counts.argmax()])


def smooth_color(
    current: tuple[int, int, int],
    target: tuple[int, int, int],
    factor: float = 0.3,
) -> tuple[int, int, int]:
    """Smooth color transitions to reduce flicker."""

    return tuple(int(current[i] + (target[i] - current[i]) * factor) for i in range(3))


def enhance_color(
    color: tuple[int, int, int],
    color_boost: float = 1.15,
    min_brightness: int = 28,
) -> tuple[int, int, int]:
    """Boost saturation slightly and enforce a useful minimum brightness."""

    values = np.array(color, dtype=np.float32)
    mean_value = values.mean()
    values = mean_value + (values - mean_value) * color_boost

    peak_value = max(float(values.max()), 1.0)
    if peak_value < min_brightness:
        values *= min_brightness / peak_value

    return tuple(int(channel) for channel in np.clip(values, 0, 255))


def adaptive_color_boost(
    color: tuple[int, int, int],
    configured_boost: float,
) -> float:
    """Convert an aggressive configured boost into a scene-aware effective boost."""

    peak = max(color)
    if peak <= 0:
        return 1.0

    saturation = (peak - min(color)) / peak
    extra = max(0.0, float(configured_boost) - 1.0)
    strength = min(0.45, saturation * 0.7)
    return 1.0 + extra * strength


def average_colors(colors: Sequence[tuple[int, int, int]]) -> tuple[int, int, int]:
    """Compute an RGB average across multiple colors."""

    if not colors:
        return (128, 128, 128)
    values = np.array(list(colors), dtype=np.float32)
    return tuple(int(channel) for channel in values.mean(axis=0))


def perceptual_color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Approximate visual distance, weighting green changes more heavily."""

    r_mean = (a[0] + b[0]) / 2.0
    dr = float(a[0] - b[0])
    dg = float(a[1] - b[1])
    db = float(a[2] - b[2])
    return float(
        np.sqrt(
            (2.0 + r_mean / 256.0) * dr * dr
            + 4.0 * dg * dg
            + (2.0 + (255.0 - r_mean) / 256.0) * db * db
        )
    )


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Compute a perceptual color delta for update throttling."""

    return perceptual_color_distance(a, b)


def build_bulb_color_map(
    bulb_ips: Sequence[str],
    colors_by_target: Mapping[str, tuple[int, int, int]],
    mode: str,
    bulb_layout: Mapping[str, str],
) -> dict[str, tuple[int, int, int]]:
    """Map sync output colors onto concrete bulbs."""

    if not bulb_ips or not colors_by_target:
        return {}

    if effective_screen_sync_mode(mode, tuple(colors_by_target)) == "single" or "all" in colors_by_target:
        single_color = colors_by_target.get("all", average_colors(tuple(colors_by_target.values())))
        return {ip: single_color for ip in bulb_ips}

    fallback = average_colors(tuple(colors_by_target.values()))
    return {
        ip: colors_by_target.get(bulb_layout.get(ip, ""), fallback)
        for ip in bulb_ips
    }

