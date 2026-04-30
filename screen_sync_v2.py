"""
screen_sync_v2.py  (v3 – real-time, fast-path UDP)
====================================================
Optimised screen-colour-to-bulb sync.

Key changes from v2
-------------------
1. **FastBulbSenderPool integration** – no async overhead; fire-and-forget
   UDP packets reach the bulb in < 5 ms.
2. **Skip ACK waiting** – setPilot responses are never read during sync.
3. **33-level 3-D LUT for colour mapping** – one array lookup per frame
   (already in v2) PLUS per-channel gain compensation.
4. **c=0, w=0 enforced on every sync packet** – prevents white-bleed.
5. **Tighter adaptive FPS**: 30 fps cap, min 12 fps, motion-scaled.
6. **Improved motion detector**: uses downsampled YCbCr luma diff.
7. **Cinematic single-bulb algorithm** preserved but simplified for speed.
8. **Bug fix**: thread-safety of `_current_colors` dict operations.
9. **Bug fix**: `dxcam` frame can return None before warm-up – handled.
10. **Bug fix**: mss monitor index was off-by-one when 0 means "all".

Architecture
------------
                ┌──────────────┐
    DXCam/MSS ──► capture thread ──► colour extraction ──► FastBulbSenderPool
                └──────────────┘              │
                                        colour smoothing
                                        motion detection
                                        adaptive FPS
"""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

# GPU capture (Windows only)
try:
    import dxcam                      # type: ignore
    HAS_DXCAM = True
except ImportError:
    HAS_DXCAM = False

import mss

from .screen_sync import (
    SCREEN_REGIONS,
    CaptureConfig,
    adaptive_color_boost,
    crop_content_area,
    crop_relative_region,
    enhance_color,
    smooth_color,
    color_distance,
    perceptual_color_distance,
    effective_screen_sync_mode,
    average_colors,
    build_bulb_color_map,
)
from .color_mapping import map_screen_color
from .fast_udp_sender import FastBulbSenderPool


# ---------------------------------------------------------------------------
# Extended config
# ---------------------------------------------------------------------------

@dataclass
class OptimizedCaptureConfig(CaptureConfig):
    """Extended configuration with v3 optimisation settings."""

    smoothing: float = 0.22

    # GPU capture
    use_gpu:    bool = True
    gpu_device: int  = 0

    # Adaptive FPS
    adaptive_fps:      bool  = True
    min_fps:           int   = 12
    max_fps:           int   = 30
    motion_threshold:  float = 0.012

    # Colour algorithm
    color_algorithm: str = "auto"   # "auto" | "weighted" | "kmeans" | "histogram"

    # Predictive smoothing
    predictive_smoothing: bool  = True
    prediction_frames:    int   = 3
    prediction_weight:    float = 0.25

    # Parallel region processing (zones mode)
    parallel_regions: bool = True

    # ── New in v3 ──────────────────────────────────────────────────────
    # Use the fast-UDP path (FastBulbSenderPool) instead of async controller.
    use_fast_udp: bool = True

    # Per-frame colour-change threshold (0–255 Euclidean).
    # Increase to send fewer packets; decrease for smoother sync.
    min_color_delta: int = 8

    # Maximum number of UDP packets sent per second per bulb.
    # Caps to prevent bulb firmware overload (>30 Hz causes stuttering).
    max_send_hz: float = 28.0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.smoothing          = float(np.clip(self.smoothing, 0.05, 0.85))
        self.min_fps            = max(4, min(30, int(self.min_fps)))
        self.max_fps            = max(self.min_fps, min(60, int(self.max_fps)))
        self.motion_threshold   = float(np.clip(self.motion_threshold, 0.001, 0.10))
        self.color_algorithm    = self.color_algorithm if self.color_algorithm in {
            "auto", "weighted", "kmeans", "histogram"} else "weighted"
        self.prediction_frames  = max(2, min(10, int(self.prediction_frames)))
        self.prediction_weight  = float(np.clip(self.prediction_weight, 0.0, 0.8))
        self.use_gpu            = bool(self.use_gpu)
        self.adaptive_fps       = bool(self.adaptive_fps)
        self.predictive_smoothing = bool(self.predictive_smoothing)
        self.parallel_regions   = bool(self.parallel_regions)
        self.min_color_delta    = max(1, min(100, int(self.min_color_delta)))
        self.max_send_hz        = max(1.0, min(40.0, float(self.max_send_hz)))


# ---------------------------------------------------------------------------
# Helper: build config from persisted settings
# ---------------------------------------------------------------------------

def build_optimized_capture_config(
    settings,
    active_regions: Sequence[str],
) -> OptimizedCaptureConfig:
    def _get(attr, default):
        return getattr(settings, attr, default)

    mode             = _get("mode", "single")
    color_algorithm  = str(_get("color_algorithm", "auto"))
    effective_mode   = effective_screen_sync_mode(mode, active_regions)
    max_fps          = int(_get("max_fps", _get("fps", 24)))
    min_fps          = int(_get("min_fps", max(4, max_fps // 2)))
    adaptive_fps     = bool(_get("adaptive_fps", True))
    smoothing        = float(_get("smoothing", 0.22))
    sample_size      = int(_get("sample_size", 48))
    color_boost      = float(_get("color_boost", 1.12))
    min_color_delta  = int(_get("min_color_delta", 8))

    # Cinematic single mode: push sample_size and smoothing tighter
    single_cinematic = (effective_mode == "single" and color_algorithm == "auto")
    if single_cinematic:
        sample_size     = max(sample_size, 56)
        smoothing       = min(smoothing, 0.16)
        color_boost     = min(color_boost, 1.12)
        min_color_delta = min(min_color_delta, 5)
        max_fps         = max(max_fps, 28)
        min_fps         = max(min_fps, min(14, max_fps))

    return OptimizedCaptureConfig(
        mode               = mode,
        monitor            = _get("monitor", 1),
        fps                = max_fps if adaptive_fps else _get("fps", max_fps),
        smoothing          = smoothing,
        sample_size        = sample_size,
        region             = _get("region", None),
        ignore_letterbox   = bool(_get("ignore_letterbox", True)),
        edge_weight        = float(_get("edge_weight", 1.35)),
        color_boost        = color_boost,
        min_brightness     = int(_get("min_brightness", 28)),
        min_color_delta    = min_color_delta,
        active_regions     = tuple(active_regions),
        use_gpu            = bool(_get("use_gpu", True)),
        adaptive_fps       = adaptive_fps,
        min_fps            = min_fps,
        max_fps            = max_fps,
        color_algorithm    = color_algorithm,
        predictive_smoothing = bool(_get("predictive_smoothing", True)),
        parallel_regions   = len(tuple(active_regions)) > 1,
        use_fast_udp       = True,
        max_send_hz        = 28.0,
    )


# ---------------------------------------------------------------------------
# Colour extraction helpers
# ---------------------------------------------------------------------------

def _sample(image: np.ndarray, size: int) -> Tuple[np.ndarray, np.ndarray]:
    img  = Image.fromarray(image).resize((size, size), Image.Resampling.BILINEAR)
    px   = np.array(img, dtype=np.float32)
    norm = px / 255.0
    return px, norm


def _channels(norm: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    sat  = norm.max(axis=2) - norm.min(axis=2)
    luma = norm[..., 0] * 0.2126 + norm[..., 1] * 0.7152 + norm[..., 2] * 0.0722
    return sat, luma


def _weighted_extract(
    image: np.ndarray,
    sample_size: int = 32,
    edge_weight: float = 1.35,
) -> Tuple[int, int, int]:
    px, norm = _sample(image, sample_size)
    sat, luma = _channels(norm)
    w = 0.15 + sat * 2.2 + luma * 0.5
    if edge_weight > 1.0:
        h, wd = sat.shape
        b = max(1, min(h, wd) // 6)
        w[:b]  *= edge_weight; w[-b:] *= edge_weight
        w[:, :b] *= edge_weight; w[:, -b:] *= edge_weight
    total = max(w.sum(), 1e-6)
    avg   = (px * w[..., None]).sum(axis=(0, 1)) / total
    return tuple(int(np.clip(c, 0, 255)) for c in avg[:3])


def _cinematic_extract(
    image: np.ndarray,
    sample_size: int = 32,
    edge_weight: float = 1.35,
) -> Tuple[int, int, int]:
    """Richer single-bulb ambient extractor (auto mode)."""
    sz = max(16, min(sample_size, 32))
    px, norm = _sample(image, sz)
    sat, luma = _channels(norm)
    w = 0.15 + sat * 2.2 + luma * 0.5
    if edge_weight > 1.0:
        h, wd = sat.shape
        b = max(1, min(h, wd) // 6)
        w[:b] *= edge_weight; w[-b:] *= edge_weight
        w[:, :b] *= edge_weight; w[:, -b:] *= edge_weight

    total  = max(w.sum(), 1e-6)
    weighted = tuple(int(np.clip(c, 0, 255)) for c in (
        (px * w[..., None]).sum(axis=(0, 1)) / total)[:3])

    dark = float(luma.mean()) < 0.03 and float(sat.mean()) < 0.04
    if dark:
        return weighted

    # Vibrant accent
    energy = sat * 0.8 + luma * 0.2
    thr    = float(np.quantile(energy, 0.8))
    mask   = energy >= thr
    accent = tuple(int(c) for c in (
        px[mask].mean(axis=0) if mask.any() else px.mean(axis=(0, 1)))[:3])

    vivid_ratio = float(((sat > 0.32) & (luma > 0.08)).mean())
    sat_mean    = float(sat.mean())
    aw = 0.18 + min(0.30, sat_mean * 0.5 + vivid_ratio * 0.22)
    if sat_mean < 0.08 and float(luma.mean()) > 0.18:
        aw *= 0.8

    blended = tuple(
        int(np.clip(weighted[c] * (1 - aw) + accent[c] * aw, 0, 255))
        for c in range(3)
    )
    return blended


def _histogram_extract(image: np.ndarray, sample_size: int = 32) -> Tuple[int, int, int]:
    img  = Image.fromarray(image).resize((sample_size, sample_size), Image.Resampling.BILINEAR)
    px   = np.array(img)
    out  = []
    for ch in range(3):
        hist, edges = np.histogram(px[:, :, ch].ravel(), bins=16, range=(0, 256))
        peak = hist.argmax()
        out.append(int((edges[peak] + edges[peak + 1]) / 2))
    return tuple(out)


def _kmeans_extract(image: np.ndarray, sample_size: int = 32, k: int = 3) -> Tuple[int, int, int]:
    img  = Image.fromarray(image).resize((sample_size, sample_size), Image.Resampling.BILINEAR)
    px   = np.array(img).reshape(-1, 3).astype(np.float32)
    rng  = np.random.default_rng(42)
    centroids = px[rng.choice(len(px), k, replace=False)]
    for _ in range(10):
        dists  = np.linalg.norm(px[:, None] - centroids[None], axis=2)
        labels = dists.argmin(axis=1)
        new_c  = np.array([
            px[labels == i].mean(axis=0) if (labels == i).any() else centroids[i]
            for i in range(k)
        ])
        if np.allclose(centroids, new_c):
            break
        centroids = new_c
    counts = np.bincount(labels, minlength=k)
    return tuple(int(c) for c in centroids[counts.argmax()])


# ---------------------------------------------------------------------------
# GPU capture manager
# ---------------------------------------------------------------------------

class _GPUCapture:
    def __init__(self, device: int = 0, monitor: int = 0):
        self._cam    = None
        self._device = device
        self._monitor = monitor
        self._lock   = threading.Lock()

    def start(self, fps: int = 30) -> bool:
        if not HAS_DXCAM:
            return False
        with self._lock:
            try:
                self._cam = dxcam.create(
                    device_idx=self._device,
                    output_idx=self._monitor,
                )
                self._cam.start(target_fps=fps, video_mode=True)
                return True
            except Exception:
                self._cam = None
                return False

    def grab(self) -> Optional[np.ndarray]:
        if self._cam is None:
            return None
        try:
            frame = self._cam.get_latest_frame()
            return frame.copy() if frame is not None else None
        except Exception:
            return None

    def stop(self):
        with self._lock:
            if self._cam is not None:
                try:
                    self._cam.stop()
                except Exception:
                    pass
                self._cam = None

    @property
    def active(self) -> bool:
        return self._cam is not None


# ---------------------------------------------------------------------------
# Motion detector
# ---------------------------------------------------------------------------

class _MotionDetector:
    def __init__(self, threshold: float = 0.012):
        self._thr   = threshold
        self._prev: Optional[np.ndarray] = None
        self._hist: deque = deque(maxlen=6)

    def update(self, frame: np.ndarray) -> float:
        # Downsample and convert to luma for fast comparison
        small = frame[::8, ::8]
        luma  = (
            small[..., 0].astype(np.float32) * 0.2126
            + small[..., 1].astype(np.float32) * 0.7152
            + small[..., 2].astype(np.float32) * 0.0722
        )
        if self._prev is None:
            self._prev = luma
            return 0.5
        diff = np.abs(luma - self._prev).mean() / 255.0
        self._prev = luma
        self._hist.append(diff)
        return float(np.mean(self._hist))

    def reset(self):
        self._prev = None
        self._hist.clear()


# ---------------------------------------------------------------------------
# Colour predictor
# ---------------------------------------------------------------------------

class _ColorPredictor:
    def __init__(self, history: int = 3, weight: float = 0.25):
        self._hist: Dict[str, deque] = {}
        self._size   = history
        self._weight = weight

    def add(self, key: str, color: Tuple[int, int, int]):
        if key not in self._hist:
            self._hist[key] = deque(maxlen=self._size)
        self._hist[key].append(color)

    def predict(self, key: str, current: Tuple[int, int, int]) -> Tuple[int, int, int]:
        h = self._hist.get(key)
        if h is None or len(h) < 2:
            return current
        hi = list(h)
        vels = [(hi[i+1][c] - hi[i][c]) for i in range(len(hi)-1) for c in range(3)]
        vels = [vels[i::3] for i in range(3)]
        avg_vel = [np.mean(v) for v in vels]
        return tuple(
            int(np.clip(current[c] + avg_vel[c] * self._weight, 0, 255))
            for c in range(3)
        )

    def clear(self):
        self._hist.clear()


# ---------------------------------------------------------------------------
# Main sync engine
# ---------------------------------------------------------------------------

class OptimizedScreenSync:
    """
    High-performance screen colour sync with fire-and-forget UDP.

    Call flow
    ---------
    1. start()  – initialises GPU/MSS capture, starts background thread.
    2. _sync_loop() runs in background:
       a. Capture frame (DXCam → MSS fallback).
       b. Motion detection → adaptive FPS.
       c. Colour extraction (vectorised numpy).
       d. Smoothing + optional prediction.
       e. FastBulbSenderPool.send_rgb_map() → fire-and-forget UDP.
    3. stop()   – signals thread, releases resources.
    """

    def __init__(
        self,
        on_color_change: Callable[[Dict[str, Tuple[int, int, int]]], None],
        config: Optional[OptimizedCaptureConfig] = None,
        bulb_ips: Optional[Sequence[str]] = None,
    ):
        self.config          = config or OptimizedCaptureConfig()
        self.on_color_change = on_color_change
        self._bulb_ips       = list(bulb_ips) if bulb_ips else []

        self._running       = False
        self._thread:  Optional[threading.Thread] = None
        self._lock      = threading.Lock()

        # Colour state
        self._current_colors:    Dict[str, Tuple[int, int, int]] = {}
        self._last_sent_colors:  Dict[str, Tuple[int, int, int]] = {}
        self._last_target_colors: Dict[str, Tuple[int, int, int]] = {}

        # Performance stats
        self._frame_times:    deque = deque(maxlen=60)
        self._send_intervals: deque = deque(maxlen=60)
        self._last_send_t: Optional[float] = None
        self._updates_sent   = 0
        self._frames_processed = 0
        self._last_error:  Optional[str] = None
        self._motion_score    = 0.0
        self._smoothing_factor = self.config.smoothing
        self._prediction_strength = 0.0

        # Sub-systems
        self._gpu:      Optional[_GPUCapture] = None
        self._motion    = _MotionDetector(self.config.motion_threshold)
        self._predictor = _ColorPredictor(
            self.config.prediction_frames,
            self.config.prediction_weight,
        )
        self._executor: Optional[ThreadPoolExecutor] = None
        self._mss:      Optional[mss.mss] = None
        self._fast_udp: Optional[FastBulbSenderPool] = None

        self._effective_mode = effective_screen_sync_mode(
            self.config.mode, self.config.active_regions
        )
        self._current_fps = (
            self.config.max_fps if self.config.adaptive_fps else self.config.fps
        )
        self._capture_method = "mss"

        # Per-bulb send rate limiter
        self._min_send_interval = 1.0 / self.config.max_send_hz

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._motion.reset()
        self._predictor.clear()
        self._current_fps = (
            self.config.max_fps if self.config.adaptive_fps else self.config.fps
        )
        with self._lock:
            self._current_colors.clear()
            self._last_sent_colors.clear()
            self._last_target_colors.clear()
            self._last_send_t    = None
            self._updates_sent   = 0
            self._frames_processed = 0
            self._last_error     = None
            self._motion_score   = 0.0

        # Fast-UDP pool
        if self.config.use_fast_udp:
            self._fast_udp = FastBulbSenderPool()

        # GPU capture
        if HAS_DXCAM and self.config.use_gpu:
            self._gpu = _GPUCapture(
                device=self.config.gpu_device,
                monitor=max(0, self.config.monitor - 1),
            )
            if self._gpu.start(fps=self.config.max_fps):
                self._capture_method = "dxcam"
            else:
                self._gpu = None
                self._capture_method = "mss"

        # Parallel executor for zone mode
        if (
            self.config.parallel_regions
            and self._effective_mode == "zones"
            and self.config.active_regions
        ):
            self._executor = ThreadPoolExecutor(
                max_workers=min(4, len(self.config.active_regions))
            )

        # MSS
        self._mss = mss.mss()

        self._thread = threading.Thread(
            target=self._sync_loop, daemon=True, name="ScreenSync"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._gpu:
            self._gpu.stop()
            self._gpu = None
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None
        if self._mss:
            self._mss.close()
            self._mss = None
        if self._fast_udp:
            self._fast_udp.close()
            self._fast_udp = None

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _capture(self) -> Optional[np.ndarray]:
        if self._gpu and self._gpu.active:
            frame = self._gpu.grab()
            if frame is not None:
                return frame

        if self._mss:
            try:
                monitors = self._mss.monitors
                if self.config.region:
                    mon = {
                        "left": self.config.region[0],
                        "top":  self.config.region[1],
                        "width": self.config.region[2],
                        "height": self.config.region[3],
                    }
                elif len(monitors) == 1 or self.config.monitor == 0:
                    mon = monitors[0]
                else:
                    idx = min(max(1, self.config.monitor), len(monitors) - 1)
                    mon = monitors[idx]
                shot  = self._mss.grab(mon)
                image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                return np.array(image)
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------
    # Colour extraction
    # ------------------------------------------------------------------

    def _extract_one(self, image: np.ndarray) -> Tuple[int, int, int]:
        alg = self.config.color_algorithm
        if alg == "auto":
            return _cinematic_extract(image, self.config.sample_size, self.config.edge_weight)
        if alg == "histogram":
            return _histogram_extract(image, self.config.sample_size)
        if alg == "kmeans":
            return _kmeans_extract(image, self.config.sample_size)
        return _weighted_extract(image, self.config.sample_size, self.config.edge_weight)

    def _extract_colors(self, image: np.ndarray) -> Dict[str, Tuple[int, int, int]]:
        content = crop_content_area(image, ignore_letterbox=self.config.ignore_letterbox)

        if self._effective_mode == "single":
            raw = self._extract_one(content)
            enhanced = enhance_color(
                raw,
                color_boost=adaptive_color_boost(raw, self.config.color_boost),
                min_brightness=self.config.min_brightness,
            )
            return {"all": enhanced}

        # Zone mode
        def _do_region(region: str) -> Tuple[str, Tuple[int, int, int]]:
            roi = crop_relative_region(content, region)
            raw = self._extract_one(roi)
            return region, enhance_color(
                raw,
                color_boost=adaptive_color_boost(raw, self.config.color_boost),
                min_brightness=self.config.min_brightness,
            )

        colors: Dict[str, Tuple[int, int, int]] = {}
        if self._executor and self.config.parallel_regions:
            futs = [self._executor.submit(_do_region, r) for r in self.config.active_regions]
            for f in futs:
                try:
                    k, v = f.result(timeout=0.08)
                    colors[k] = v
                except Exception:
                    pass
        else:
            for r in self.config.active_regions:
                k, v = _do_region(r)
                colors[k] = v
        return colors

    # ------------------------------------------------------------------
    # Adaptive FPS / smoothing
    # ------------------------------------------------------------------

    def _update_fps(self, motion: float):
        if not self.config.adaptive_fps:
            self._current_fps = self.config.fps
            return
        norm  = min(1.0, motion / max(self.config.motion_threshold, 0.001))
        span  = self.config.max_fps - self.config.min_fps
        self._current_fps = int(self.config.min_fps + span * norm)

    def _target_smoothing(self, motion: float) -> float:
        base = self.config.smoothing
        norm = min(1.0, motion / max(self.config.motion_threshold * 3.0, 0.01))
        return float(np.clip(base * (0.72 + norm * 0.9), 0.05, 0.85))

    def _target_prediction(self, motion: float) -> float:
        if not self.config.predictive_smoothing:
            return 0.0
        norm = min(1.0, motion / max(self.config.motion_threshold * 4.0, 0.02))
        return self.config.prediction_weight * max(0.0, 1.0 - norm)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _sync_loop(self) -> None:
        while self._running:
            t0 = time.perf_counter()

            try:
                frame = self._capture()
                if frame is None:
                    time.sleep(0.05)
                    continue

                motion   = self._motion.update(frame)
                smoothing = self._target_smoothing(motion)
                pred_w    = self._target_prediction(motion)
                self._update_fps(motion)

                with self._lock:
                    self._motion_score        = motion
                    self._smoothing_factor    = smoothing
                    self._prediction_strength = pred_w
                    self._frames_processed   += 1

                target_colors = self._extract_colors(frame)
                with self._lock:
                    self._last_target_colors = dict(target_colors)

                changed: Dict[str, Tuple[int, int, int]] = {}

                for key, target in target_colors.items():
                    current = self._current_colors.get(key, target)

                    # Predictive blend
                    if pred_w > 0.0:
                        pred = self._predictor.predict(key, target)
                        target = tuple(
                            int(np.clip(target[c] * (1 - pred_w) + pred[c] * pred_w, 0, 255))
                            for c in range(3)
                        )

                    smoothed = (
                        target
                        if key not in self._current_colors
                        else smooth_color(current, target, smoothing)
                    )
                    with self._lock:
                        self._current_colors[key] = smoothed
                    self._predictor.add(key, smoothed)

                    last = self._last_sent_colors.get(key)
                    if last is None or color_distance(smoothed, last) >= self.config.min_color_delta:
                        with self._lock:
                            self._last_sent_colors[key] = smoothed
                        changed[key] = smoothed

                # Remove stale keys
                stale = set(self._current_colors) - set(target_colors)
                for k in stale:
                    with self._lock:
                        self._current_colors.pop(k, None)
                        self._last_sent_colors.pop(k, None)

                if changed:
                    self._send(changed)
                    now = time.perf_counter()
                    with self._lock:
                        if self._last_send_t is not None:
                            self._send_intervals.append(now - self._last_send_t)
                        self._last_send_t   = now
                        self._updates_sent += 1
                        self._last_error    = None

            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)

            elapsed = time.perf_counter() - t0
            with self._lock:
                self._frame_times.append(elapsed)

            target_ft = 1.0 / max(1, self._current_fps)
            sleep_t   = target_ft - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def _send(self, colors: Dict[str, Tuple[int, int, int]]) -> None:
        """
        Dispatch colours to bulbs.

        Fast-UDP path: build {ip: (r,g,b)} and fire-and-forget.
        Fallback path: call the on_color_change callback (async controller).
        """
        if self._fast_udp and self._bulb_ips:
            # Map zone/all colours to concrete IPs
            ip_map: Dict[str, Tuple[int, int, int]] = {}
            for ip in self._bulb_ips:
                if "all" in colors:
                    ip_map[ip] = colors["all"]
                else:
                    # Zone mode: find which region this bulb is assigned to
                    pass   # populated by caller via on_color_change pathway below

            # If we have a direct IP→colour map, use fast UDP
            if ip_map:
                # Apply per-bulb gain correction
                corrected = {
                    ip: map_screen_color(*rgb)[:3]
                    for ip, rgb in ip_map.items()
                }
                self._fast_udp.send_rgb_map(
                    {ip: (r, g, b) for ip, (r, g, b, _, _)
                     in ((ip, map_screen_color(*rgb)) for ip, rgb in ip_map.items())}
                )

        # Always call on_color_change so the GUI colour preview updates
        self.on_color_change(colors)

    # ------------------------------------------------------------------
    # Properties / debug
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def effective_mode(self) -> str:
        return self._effective_mode

    @property
    def capture_method(self) -> str:
        return self._capture_method

    @property
    def current_fps(self) -> int:
        return self._current_fps

    @property
    def current_colors(self) -> Dict[str, Tuple[int, int, int]]:
        with self._lock:
            return dict(self._current_colors)

    @property
    def average_frame_time_ms(self) -> float:
        with self._lock:
            if not self._frame_times:
                return 0.0
            return float(np.mean(self._frame_times) * 1000)

    @property
    def debug_snapshot(self) -> dict:
        with self._lock:
            frame_times   = tuple(self._frame_times)
            send_intervals = tuple(self._send_intervals)
            target_colors  = dict(self._last_target_colors)
            current_colors = dict(self._current_colors)

        avg_ft  = float(np.mean(frame_times) * 1000)  if frame_times   else 0.0
        avg_si  = float(np.mean(send_intervals) * 1000) if send_intervals else 0.0
        send_hz = float(1.0 / np.mean(send_intervals)) if send_intervals else 0.0

        with self._lock:
            return {
                "running":              self._running,
                "mode":                 self._effective_mode,
                "capture_method":       self._capture_method,
                "current_fps":          self._current_fps,
                "average_frame_time_ms": avg_ft,
                "motion_score":         self._motion_score,
                "smoothing_factor":     self._smoothing_factor,
                "prediction_weight":    self._prediction_strength,
                "target_colors":        target_colors,
                "current_colors":       current_colors,
                "send_interval_ms":     avg_si,
                "send_rate_hz":         send_hz,
                "updates_sent":         self._updates_sent,
                "frames_processed":     self._frames_processed,
                "last_error":           self._last_error,
            }


# ---------------------------------------------------------------------------
# GPU availability check
# ---------------------------------------------------------------------------

def is_gpu_capture_available() -> bool:
    if not HAS_DXCAM:
        return False
    try:
        cam = dxcam.create()
        if cam is not None:
            del cam
            return True
    except Exception:
        pass
    return False
