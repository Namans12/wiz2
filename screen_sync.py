"""Screen capture and color extraction for screen sync feature.

Bug fixes applied (v2):
- BUG 1: _current_colors and _last_sent_colors were accessed from both the
  sync thread and GUI thread without any lock → added threading.Lock().
- BUG 2: monitor index passed to mss was off-by-one: config.monitor=1
  means "first real monitor" but monitors[1] is already correct; however
  when only one monitor is present, monitors[0] is the combined/primary and
  monitors[1] doesn't exist → _resolve_monitor() now guards this correctly.
- BUG 3: crop_content_area could return a zero-height or zero-width slice
  when all pixels are dark (e.g. during screen blank / screensaver) and that
  caused downstream numpy crashes → added minimum size guard.
- BUG 4: extract_dominant_color_kmeans used np.random.seed(42) globally
  which mutates the global RNG state and can interfere with other code;
  replaced with a local Generator.
- BUG 5: smooth_color factor was applied as (1 - factor) on current but the
  semantics were reversed: factor=0.3 meant "move 30% toward target" but the
  docstring said "smoothing factor"; clarified and made consistent with v2.
- BUG 6: ScreenSync.stop() set _running=False but _thread.join(timeout=2.0)
  could silently time out, leaving the thread running and causing a double-
  start crash on the next call. Now logs a warning if join times out.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

import mss
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


SCREEN_REGIONS: dict[str, tuple[float, float, float, float]] = {
    # Intentionally biased toward screen edges rather than full thirds.
    "left":         (0.00, 0.08, 0.22, 0.84),
    "center":       (0.22, 0.12, 0.56, 0.76),
    "right":        (0.78, 0.08, 0.22, 0.84),
    "top-left":     (0.00, 0.00, 0.34, 0.28),
    "top":          (0.22, 0.00, 0.56, 0.24),
    "top-right":    (0.66, 0.00, 0.34, 0.28),
    "bottom-left":  (0.00, 0.72, 0.34, 0.28),
    "bottom":       (0.22, 0.76, 0.56, 0.24),
    "bottom-right": (0.66, 0.72, 0.34, 0.28),
}

_MIN_CROP_PX = 4          # minimum pixel dimension after letterbox crop


@dataclass
class CaptureConfig:
    """Configuration for screen capture."""

    mode:             str   = "single"
    monitor:          int   = 1          # 0 = all monitors, 1+ = specific monitor
    fps:              int   = 12
    sample_size:      int   = 48
    region:           Optional[tuple[int, int, int, int]] = None
    ignore_letterbox: bool  = True
    edge_weight:      float = 1.35
    color_boost:      float = 1.15
    min_brightness:   int   = 28
    min_color_delta:  int   = 12
    active_regions:   tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.mode           = self.mode if self.mode in {"single", "zones"} else "single"
        self.monitor        = int(self.monitor)
        self.fps            = max(4, min(60, int(self.fps)))
        self.sample_size    = max(16, min(96, int(self.sample_size)))
        self.edge_weight    = max(1.0, float(self.edge_weight))
        self.color_boost    = max(1.0, float(self.color_boost))
        self.min_brightness = max(0, min(255, int(self.min_brightness)))
        self.min_color_delta = max(1, min(255, int(self.min_color_delta)))
        self.active_regions  = tuple(
            r for r in self.active_regions if r in SCREEN_REGIONS
        )


# ---------------------------------------------------------------------------
# Monitor utilities
# ---------------------------------------------------------------------------

def list_monitors() -> list[dict[str, int | str]]:
    """Return available monitor choices for the UI."""
    with mss.mss() as sct:
        monitors = sct.monitors
        options: list[dict[str, int | str]] = []

        if len(monitors) > 1:
            # monitors[0] is the "all screens" virtual monitor
            combined = monitors[0]
            options.append({
                "index": 0,
                "label": f"All monitors ({combined['width']}x{combined['height']})",
            })
            for idx, mon in enumerate(monitors[1:], start=1):
                options.append({
                    "index": idx,
                    "label": f"Monitor {idx} ({mon['width']}x{mon['height']})",
                })
        elif monitors:
            mon = monitors[0]
            options.append({
                "index": 0,
                "label": f"Primary monitor ({mon['width']}x{mon['height']})",
            })
    return options


def _resolve_monitor(monitors: list[dict], monitor_index: int) -> dict:
    """
    FIX (BUG 2): Original code accessed monitors[monitor_index] directly
    without bounding.  When only one physical monitor exists, mss returns
    monitors = [combined_only] so monitors[1] would raise IndexError.
    """
    n = len(monitors)
    if n == 0:
        raise RuntimeError("No monitors found by mss")
    if n == 1:
        return monitors[0]
    # monitors[0] is the "all screens" composite; real screens start at 1
    if monitor_index == 0:
        return monitors[0]
    idx = max(1, min(monitor_index, n - 1))
    return monitors[idx]


def resolve_active_regions(
    bulb_layout: Mapping[str, str],
    bulb_ips:    Sequence[str],
) -> tuple[str, ...]:
    configured = {
        bulb_layout[ip]
        for ip in bulb_ips
        if bulb_layout.get(ip) in SCREEN_REGIONS
    }
    ordered = tuple(r for r in SCREEN_REGIONS if r in configured)
    return ordered if len(ordered) >= 2 else ()


def effective_screen_sync_mode(mode: str, active_regions: Sequence[str]) -> str:
    if mode == "zones" and len(tuple(active_regions)) >= 2:
        return "zones"
    return "single"


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def capture_screen(sct: mss.mss, config: CaptureConfig) -> np.ndarray:
    monitors = sct.monitors
    if config.region:
        monitor = {
            "left":   config.region[0],
            "top":    config.region[1],
            "width":  config.region[2],
            "height": config.region[3],
        }
    else:
        monitor = _resolve_monitor(monitors, config.monitor)

    screenshot = sct.grab(monitor)
    image = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
    return np.array(image)


# ---------------------------------------------------------------------------
# Letterbox / content-area detection
# ---------------------------------------------------------------------------

def detect_content_bounds(
    image:           np.ndarray,
    threshold:       int   = 16,
    min_active_ratio: float = 0.02,
) -> tuple[int, int, int, int]:
    """
    Detect active content bounds and return (left, top, width, height).
    Returns full image dimensions if no content is found.
    """
    height, width = image.shape[:2]
    if height < _MIN_CROP_PX or width < _MIN_CROP_PX:
        return (0, 0, width, height)

    pixels  = image.astype(np.float32)
    luma    = (pixels[:, :, 0] * 0.2126
               + pixels[:, :, 1] * 0.7152
               + pixels[:, :, 2] * 0.0722)
    row_act = (luma > threshold).mean(axis=1)
    col_act = (luma > threshold).mean(axis=0)

    active_rows = np.where(row_act > min_active_ratio)[0]
    active_cols = np.where(col_act > min_active_ratio)[0]

    if active_rows.size == 0 or active_cols.size == 0:
        return (0, 0, width, height)

    top    = int(active_rows[0])
    bottom = int(active_rows[-1] + 1)
    left   = int(active_cols[0])
    right  = int(active_cols[-1] + 1)

    # Reject very small crops – probably a black / blank screen
    if (bottom - top) < height * 0.45 or (right - left) < width * 0.45:
        return (0, 0, width, height)

    return (left, top, right - left, bottom - top)


def crop_content_area(image: np.ndarray, ignore_letterbox: bool = True) -> np.ndarray:
    """
    Crop black bars.

    FIX (BUG 3): After cropping, check that the result has a minimum size
    so downstream numpy operations (resize, mean, etc.) never see an empty
    array.  If the crop would be too small, return the full image instead.
    """
    if not ignore_letterbox:
        return image

    left, top, w, h = detect_content_bounds(image)
    if w < _MIN_CROP_PX or h < _MIN_CROP_PX:
        return image                               # screen is probably blank
    cropped = image[top : top + h, left : left + w]
    if cropped.size == 0:
        return image
    return cropped


def crop_relative_region(image: np.ndarray, region_name: str) -> np.ndarray:
    """Crop a logical region using relative bounds."""
    if region_name not in SCREEN_REGIONS:
        return image

    rel_left, rel_top, rel_w, rel_h = SCREEN_REGIONS[region_name]
    height, width = image.shape[:2]
    left   = int(width  * rel_left)
    top    = int(height * rel_top)
    right  = max(left + _MIN_CROP_PX, int(width  * (rel_left + rel_w)))
    bottom = max(top  + _MIN_CROP_PX, int(height * (rel_top  + rel_h)))
    return image[top:bottom, left:right]


# ---------------------------------------------------------------------------
# Colour extraction
# ---------------------------------------------------------------------------

def extract_dominant_color(
    image:       np.ndarray,
    sample_size: int   = 48,
    edge_weight: float = 1.35,
) -> tuple[int, int, int]:
    """Extract a weighted ambient colour from an RGB image."""
    img = Image.fromarray(image).resize(
        (sample_size, sample_size), Image.Resampling.BILINEAR
    )
    pixels     = np.array(img).astype(np.float32)
    normalized = pixels / 255.0
    height, width = pixels.shape[:2]

    max_ch  = normalized.max(axis=2)
    min_ch  = normalized.min(axis=2)
    sat     = max_ch - min_ch
    luma    = (normalized[:, :, 0] * 0.2126
               + normalized[:, :, 1] * 0.7152
               + normalized[:, :, 2] * 0.0722)

    weights = 0.15 + sat * 2.2 + luma * 0.5
    border  = max(1, min(height, width) // 6)
    if edge_weight > 1.0:
        weights[:border, :]  *= edge_weight
        weights[-border:, :] *= edge_weight
        weights[:, :border]  *= edge_weight
        weights[:, -border:] *= edge_weight

    total = max(float(weights.sum()), 1e-6)
    avg   = (pixels * weights[:, :, None]).sum(axis=(0, 1)) / total
    return tuple(int(ch) for ch in np.clip(avg[:3], 0, 255))


def extract_dominant_color_kmeans(
    image:       np.ndarray,
    sample_size: int   = 50,
    edge_weight: float = 1.35,
    k:           int   = 3,
) -> tuple[int, int, int]:
    """
    Extract dominant colour using a small k-means cluster pass.

    FIX (BUG 4): Original code called np.random.seed(42) which corrupts the
    global NumPy RNG state.  Replaced with a local np.random.Generator.
    """
    del edge_weight

    img    = Image.fromarray(image).resize(
        (sample_size, sample_size), Image.Resampling.BILINEAR
    )
    pixels = np.array(img).reshape(-1, 3).astype(np.float32)

    # Local RNG – does not touch global state
    rng        = np.random.default_rng(42)
    centroids  = pixels[rng.choice(len(pixels), min(k, len(pixels)), replace=False)]

    for _ in range(10):
        dists     = np.linalg.norm(pixels[:, None] - centroids[None], axis=2)
        labels    = dists.argmin(axis=1)
        new_cents = np.array([
            pixels[labels == i].mean(axis=0) if (labels == i).any() else centroids[i]
            for i in range(len(centroids))
        ])
        if np.allclose(centroids, new_cents):
            break
        centroids = new_cents

    counts = np.bincount(labels, minlength=len(centroids))
    return tuple(int(ch) for ch in centroids[counts.argmax()])


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def smooth_color(
    current: tuple[int, int, int],
    target:  tuple[int, int, int],
    factor:  float = 0.3,
) -> tuple[int, int, int]:
    """
    EMA towards target.  factor = fraction to move per frame.

    FIX (BUG 5): Original formula was semantically the same but the variable
    name "smoothing" was ambiguous.  Clarified: factor=0.3 means "move 30 %
    toward target", 0.0 = frozen, 1.0 = instant snap.
    """
    f = max(0.0, min(1.0, float(factor)))
    return tuple(int(current[i] + (target[i] - current[i]) * f) for i in range(3))


def enhance_color(
    color:          tuple[int, int, int],
    color_boost:    float = 1.15,
    min_brightness: int   = 28,
) -> tuple[int, int, int]:
    """Boost saturation slightly and enforce a useful minimum brightness."""
    values = np.array(color, dtype=np.float32)
    mean   = values.mean()
    values = mean + (values - mean) * color_boost

    peak = float(values.max())
    if peak > 0 and peak < min_brightness:
        values *= min_brightness / peak

    return tuple(int(ch) for ch in np.clip(values, 0, 255))


def adaptive_color_boost(
    color:            tuple[int, int, int],
    configured_boost: float,
) -> float:
    """Convert an aggressive configured boost into a scene-aware effective boost."""
    peak = max(color)
    if peak <= 0:
        return 1.0
    sat    = (peak - min(color)) / peak
    extra  = max(0.0, float(configured_boost) - 1.0)
    strength = min(0.45, sat * 0.7)
    return 1.0 + extra * strength


def average_colors(colors: Sequence[tuple[int, int, int]]) -> tuple[int, int, int]:
    if not colors:
        return (128, 128, 128)
    arr = np.array(list(colors), dtype=np.float32)
    return tuple(int(ch) for ch in arr.mean(axis=0))


def perceptual_color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    r_mean = (a[0] + b[0]) / 2.0
    dr, dg, db = float(a[0]-b[0]), float(a[1]-b[1]), float(a[2]-b[2])
    return float(np.sqrt(
        (2.0 + r_mean / 256.0) * dr * dr
        + 4.0 * dg * dg
        + (2.0 + (255.0 - r_mean) / 256.0) * db * db
    ))


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return perceptual_color_distance(a, b)


def build_bulb_color_map(
    bulb_ips:         Sequence[str],
    colors_by_target: Mapping[str, tuple[int, int, int]],
    mode:             str,
    bulb_layout:      Mapping[str, str],
) -> dict[str, tuple[int, int, int]]:
    if not bulb_ips or not colors_by_target:
        return {}

    if (
        effective_screen_sync_mode(mode, tuple(colors_by_target)) == "single"
        or "all" in colors_by_target
    ):
        single = colors_by_target.get(
            "all", average_colors(tuple(colors_by_target.values()))
        )
        return {ip: single for ip in bulb_ips}

    fallback = average_colors(tuple(colors_by_target.values()))
    return {
        ip: colors_by_target.get(bulb_layout.get(ip, ""), fallback)
        for ip in bulb_ips
    }


# ---------------------------------------------------------------------------
# ScreenSync
# ---------------------------------------------------------------------------

class ScreenSync:
    """Real-time screen colour sync manager."""

    def __init__(
        self,
        on_color_change: Callable[[dict[str, tuple[int, int, int]]], None],
        config:          Optional[CaptureConfig] = None,
        use_kmeans:      bool  = False,
        smoothing:       float = 0.3,
    ):
        self.config          = config or CaptureConfig()
        self.on_color_change = on_color_change
        self.use_kmeans      = use_kmeans
        self.smoothing       = smoothing

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # FIX (BUG 1): protect _current_colors and _last_sent_colors with a lock
        # because the sync thread writes and the GUI thread reads.
        self._lock                 = threading.Lock()
        self._current_colors:     dict[str, tuple[int, int, int]] = {}
        self._last_sent_colors:   dict[str, tuple[int, int, int]] = {}

        self._effective_mode = effective_screen_sync_mode(
            self.config.mode, self.config.active_regions
        )

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        with self._lock:
            self._current_colors.clear()
            self._last_sent_colors.clear()
        self._thread = threading.Thread(
            target=self._sync_loop, daemon=True, name="ScreenSync"
        )
        self._thread.start()

    def stop(self) -> None:
        """
        FIX (BUG 6): Original join(timeout=2.0) silently failed, leaving a
        ghost thread that caused double-send after a restart.  Now we log a
        warning when the join times out.
        """
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                logger.warning(
                    "ScreenSync thread did not stop within 2 s – may still be running"
                )
            self._thread = None

    def _extract_colors(self, image: np.ndarray) -> dict[str, tuple[int, int, int]]:
        extract_fn = (
            extract_dominant_color_kmeans if self.use_kmeans else extract_dominant_color
        )
        content = crop_content_area(image, ignore_letterbox=self.config.ignore_letterbox)

        if self._effective_mode == "single":
            color = extract_fn(content, self.config.sample_size, self.config.edge_weight)
            return {
                "all": enhance_color(
                    color,
                    color_boost=adaptive_color_boost(color, self.config.color_boost),
                    min_brightness=self.config.min_brightness,
                )
            }

        colors: dict[str, tuple[int, int, int]] = {}
        for region in self.config.active_regions:
            roi   = crop_relative_region(content, region)
            color = extract_fn(roi, self.config.sample_size, self.config.edge_weight)
            colors[region] = enhance_color(
                color,
                color_boost=adaptive_color_boost(color, self.config.color_boost),
                min_brightness=self.config.min_brightness,
            )
        return colors

    def _sync_loop(self) -> None:
        frame_time = 1.0 / max(1, self.config.fps)
        with mss.mss() as sct:
            while self._running:
                t0 = time.perf_counter()
                try:
                    image         = capture_screen(sct, self.config)
                    target_colors = self._extract_colors(image)
                    changed:      dict[str, tuple[int, int, int]] = {}

                    with self._lock:
                        for key, target in target_colors.items():
                            current  = self._current_colors.get(key, target)
                            smoothed = (
                                target
                                if key not in self._current_colors
                                else smooth_color(current, target, self.smoothing)
                            )
                            self._current_colors[key] = smoothed
                            last = self._last_sent_colors.get(key)
                            if last is None or color_distance(smoothed, last) >= self.config.min_color_delta:
                                self._last_sent_colors[key] = smoothed
                                changed[key] = smoothed

                        stale = set(self._current_colors) - set(target_colors)
                        for k in stale:
                            self._current_colors.pop(k, None)
                            self._last_sent_colors.pop(k, None)

                    if changed:
                        self.on_color_change(changed)

                except Exception as exc:
                    logger.error("Screen sync error: %s", exc)

                elapsed = time.perf_counter() - t0
                sleep   = frame_time - elapsed
                if sleep > 0:
                    time.sleep(sleep)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def effective_mode(self) -> str:
        return self._effective_mode

    @property
    def current_colors(self) -> dict[str, tuple[int, int, int]]:
        with self._lock:
            return dict(self._current_colors)
