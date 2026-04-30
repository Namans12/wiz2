"""
clap_detector.py  (v2 – improved signal processing)
=====================================================
Robust double-clap detection with adaptive noise floor, high-pass
filtering, and a clean state machine.

Improvements over the original
-------------------------------
* Adaptive noise floor: exponential moving average of background RMS
  so the threshold self-adjusts to quiet vs noisy rooms.
* High-pass filter (IIR) to isolate clap transients (> ~800 Hz).
  This rejects speech, music, and HVAC noise.
* Peak-detector instead of raw threshold crossing (avoids double-firing
  on ringing).
* Hysteresis: a clap event only fires once per distinct transient.
* Double-clap state machine with configurable timing window.
* All parameters exposed; sensible defaults that work across most mics.
* Thread-safe; exposes a stop() that joins cleanly.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class ClapConfig:
    # --- Sensitivity ---
    # Peak amplitude threshold (0–1 normalised, relative to full-scale).
    # Reduce to detect quieter claps; increase to reduce false positives.
    threshold: float = 0.25

    # RMS threshold: minimum overall energy in the window.
    rms_threshold: float = 0.05

    # Peak / RMS ratio: clap transients are "sharp" (high peak, low RMS).
    # Lower values accept more claps; 3.0 is a good starting point.
    min_peak_to_rms: float = 2.5

    # Adaptive floor multiplier: how many × above the adaptive RMS floor
    # the peak must be to qualify as a clap.
    adaptive_multiplier: float = 6.0

    # Maximum duration of a valid clap burst (seconds).
    max_duration: float = 0.12

    # Minimum silence between two independent claps (seconds).
    cooldown: float = 0.18

    # Enable double-clap mode (requires two claps within double_clap_window).
    double_clap: bool = True

    # Maximum inter-clap gap for a double clap (seconds).
    double_clap_window: float = 0.60

    # Minimum inter-clap gap (seconds, prevents "echo" double-detection).
    double_clap_min_gap: float = 0.08

    # Audio capture settings
    sample_rate: int = 16000      # Hz
    block_size:  int = 512        # samples per processing block (~32 ms)
    channels:    int = 1

    # High-pass filter cutoff (Hz). Claps are mainly 1–4 kHz.
    hp_cutoff_hz: float = 800.0

    # EMA decay for adaptive noise floor (0 = static, 1 = instant).
    # 0.005 means the floor adapts over ~200 blocks (~6 seconds).
    noise_floor_decay: float = 0.005


# ---------------------------------------------------------------------------
# Simple first-order high-pass IIR filter (no scipy required)
# ---------------------------------------------------------------------------

class _HighPassFilter:
    """First-order IIR high-pass filter using the bilinear transform."""

    def __init__(self, cutoff_hz: float, sample_rate: int):
        dt  = 1.0 / sample_rate
        rc  = 1.0 / (2.0 * np.pi * cutoff_hz)
        self._alpha = rc / (rc + dt)
        self._prev_in  = 0.0
        self._prev_out = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        """Apply high-pass filter to a 1-D float32 array."""
        out = np.empty_like(x)
        prev_in  = self._prev_in
        prev_out = self._prev_out
        a = self._alpha
        for i, xi in enumerate(x):
            y = a * (prev_out + xi - prev_in)
            out[i] = y
            prev_in  = xi
            prev_out = y
        self._prev_in  = prev_in
        self._prev_out = prev_out
        return out

    def reset(self):
        self._prev_in  = 0.0
        self._prev_out = 0.0


# ---------------------------------------------------------------------------
# Double-clap state machine
# ---------------------------------------------------------------------------

class _DoubleClap:
    """Tracks clap events and fires callback on confirmed double-clap."""

    def __init__(
        self,
        on_double_clap: Callable[[], None],
        window_s: float,
        min_gap_s: float,
    ):
        self._cb       = on_double_clap
        self._window   = window_s
        self._min_gap  = min_gap_s
        self._first_t: Optional[float] = None
        self._lock = threading.Lock()

    def register_clap(self, t: float) -> None:
        with self._lock:
            if self._first_t is None:
                # Start waiting for second clap
                self._first_t = t
                return

            gap = t - self._first_t
            if gap < self._min_gap:
                # Too fast – probably same clap ringing; update first time
                self._first_t = t
                return

            if gap <= self._window:
                # Valid double clap!
                self._first_t = None
                # Fire callback outside lock to avoid deadlock
                threading.Thread(target=self._cb, daemon=True).start()
            else:
                # Expired window – treat this as a new first clap
                self._first_t = t

    def reset(self):
        with self._lock:
            self._first_t = None


# ---------------------------------------------------------------------------
# Main detector
# ---------------------------------------------------------------------------

class ClapDetector:
    """
    Listens to the microphone and calls on_clap() on confirmed clap events.

    With double_clap=True (default) two claps within double_clap_window
    seconds are required to trigger.  With double_clap=False a single
    clap triggers.
    """

    def __init__(
        self,
        on_clap: Callable[[], None],
        config: Optional[ClapConfig] = None,
    ):
        self._cb     = on_clap
        self.config  = config or ClapConfig()

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Signal processing
        self._hp = _HighPassFilter(
            self.config.hp_cutoff_hz, self.config.sample_rate
        )
        self._noise_floor_rms: float = self.config.rms_threshold
        self._in_burst  = False
        self._burst_start: float = 0.0
        self._last_clap_t: float = 0.0

        # Double-clap state machine
        self._dclap = _DoubleClap(
            on_double_clap=self._cb,
            window_s=self.config.double_clap_window,
            min_gap_s=self.config.double_clap_min_gap,
        ) if self.config.double_clap else None

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._hp.reset()
        if self._dclap:
            self._dclap.reset()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="ClapDetector"
        )
        self._thread.start()
        logger.info("ClapDetector started (double=%s)", self.config.double_clap)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("ClapDetector stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    def _run(self) -> None:
        try:
            import sounddevice as sd  # type: ignore
        except ImportError:
            logger.error(
                "sounddevice not installed – clap detection unavailable. "
                "Install with: pip install sounddevice"
            )
            self._running = False
            return

        cfg = self.config
        try:
            with sd.InputStream(
                samplerate=cfg.sample_rate,
                blocksize=cfg.block_size,
                channels=cfg.channels,
                dtype="float32",
                latency="low",
            ) as stream:
                while self._running:
                    try:
                        block, _ = stream.read(cfg.block_size)
                    except sd.PortAudioError as e:
                        logger.warning("Audio read error: %s", e)
                        time.sleep(0.1)
                        continue

                    audio = block[:, 0] if block.ndim > 1 else block.ravel()
                    self._process_block(audio, time.monotonic())
        except Exception as exc:
            logger.error("ClapDetector audio stream error: %s", exc)
            self._running = False

    # ------------------------------------------------------------------
    def _process_block(self, audio: np.ndarray, t: float) -> None:
        cfg = self.config

        # High-pass filter
        hp_audio = self._hp.process(audio)

        peak = float(np.abs(hp_audio).max())
        rms  = float(np.sqrt(np.mean(hp_audio ** 2)))

        # Update adaptive noise floor (EMA on RMS when no burst is active)
        if not self._in_burst:
            d = cfg.noise_floor_decay
            self._noise_floor_rms = (
                self._noise_floor_rms * (1.0 - d) + rms * d
            )
        noise_floor = max(cfg.rms_threshold, self._noise_floor_rms)

        # Compute adaptive peak threshold
        adaptive_peak_threshold = max(
            cfg.threshold,
            noise_floor * cfg.adaptive_multiplier,
        )

        # --- Burst detection ---
        if not self._in_burst:
            if (
                peak >= adaptive_peak_threshold
                and rms  >= cfg.rms_threshold
                and (peak / max(rms, _EPS)) >= cfg.min_peak_to_rms
                and (t - self._last_clap_t) >= cfg.cooldown
            ):
                self._in_burst    = True
                self._burst_start = t
        else:
            burst_dur = t - self._burst_start
            if burst_dur > cfg.max_duration or peak < adaptive_peak_threshold * 0.3:
                # End of burst – classify as clap
                if burst_dur <= cfg.max_duration:
                    self._last_clap_t = t
                    self._on_clap_detected(t)
                self._in_burst = False

    def _on_clap_detected(self, t: float) -> None:
        logger.debug("Clap detected at %.3f s", t)
        if self._dclap:
            self._dclap.register_clap(t)
        else:
            threading.Thread(target=self._cb, daemon=True).start()


_EPS = 1e-8
