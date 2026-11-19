"""Clap detection for toggle functionality."""

import time
import threading
from dataclasses import dataclass
from typing import Any, Optional, Callable

import numpy as np



def _sounddevice():
    """Import sounddevice on first use (it costs ~0.3 s at process start)."""
    import sounddevice

    return sounddevice


def __getattr__(name: str):
    # Keeps ``clap_detector.sd`` available (tests patch ``sd.InputStream``)
    # without importing sounddevice when the module is merely imported.
    if name == "sd":
        return _sounddevice()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass
class ClapConfig:
    """Configuration for clap detection."""
    threshold: float = 0.055  # Peak amplitude threshold (0-1)
    rms_threshold: float = 0.01  # Minimum RMS energy for a clap block
    min_peak_to_rms: float = 2.7  # Clap should be a short transient, not steady noise
    adaptive_multiplier: float = 5.0  # Dynamic threshold factor over ambient noise floor
    noise_floor_decay: float = 0.985  # Smoothing for ambient-noise tracking
    noise_floor_fast_rise: float = 0.30  # Faster rise when ambient noise increases
    min_duration: float = 0.005  # Minimum clap duration in seconds
    max_duration: float = 0.2  # Maximum clap duration in seconds
    cooldown: float = 0.45  # Minimum time between triggered actions
    double_clap: bool = True  # Require double clap to trigger
    double_clap_window: float = 0.85  # Max time between double claps
    use_freq_gate: bool = True
    freq_gate_low_hz: float = 800.0
    freq_gate_high_hz: float = 5000.0
    freq_band_ratio_min: float = 0.12
    sample_rate: int = 44100
    block_size: int = 256
    device_index: Optional[int] = None

    def __post_init__(self) -> None:
        self.threshold = 0.055 if self.threshold > 0.2 else self.threshold
        self.threshold = max(0.01, min(0.2, float(self.threshold)))
        self.rms_threshold = max(0.002, min(0.05, float(self.rms_threshold)))
        self.min_peak_to_rms = max(1.2, min(6.0, float(self.min_peak_to_rms)))
        self.adaptive_multiplier = max(1.5, min(12.0, float(self.adaptive_multiplier)))
        self.noise_floor_fast_rise = max(0.05, min(0.8, float(self.noise_floor_fast_rise)))
        self.min_duration = max(0.002, min(0.05, float(self.min_duration)))
        self.max_duration = max(0.04, min(0.4, float(self.max_duration)))
        self.cooldown = max(0.1, min(2.0, float(self.cooldown)))
        self.double_clap_window = max(0.25, min(1.5, float(self.double_clap_window)))
        self.block_size = max(64, min(1024, int(self.block_size)))


class ClapDetector:
    """Real-time clap detection using microphone input."""
    
    def __init__(
        self,
        on_clap: Callable[[], None],
        config: Optional[ClapConfig] = None,
    ):
        """
        Args:
            on_clap: Callback when clap (or double clap) is detected
            config: Detection configuration
        """
        self.config = config or ClapConfig()
        self.on_clap = on_clap
        
        self._running = False
        self._stream: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        
        # State for detection
        self._last_clap_time = 0.0
        self._single_clap_time: Optional[float] = None  # For double clap detection
        self._in_clap = False
        self._clap_start_time = 0.0
        self._last_signal_time = 0.0
        self._noise_floor = max(self.config.rms_threshold / 4, 1e-4)
        self._noise_floor_fast = self._noise_floor
        if self.config.use_freq_gate:
            freqs = np.fft.rfftfreq(self.config.block_size, d=1.0 / self.config.sample_rate)
            self._freq_low_idx = int(np.searchsorted(freqs, self.config.freq_gate_low_hz))
            self._freq_high_idx = int(np.searchsorted(freqs, self.config.freq_gate_high_hz))
        else:
            self._freq_low_idx = 0
            self._freq_high_idx = 0
    
    def start(self) -> None:
        """Start clap detection."""
        if self._running:
            return
        
        self._running = True
        try:
            self._start_stream()
        except Exception:
            self._running = False
            stream, self._stream = self._stream, None
            if stream is not None:
                stream.close()
            raise

    def _start_stream(self) -> None:
        self._stream = _sounddevice().InputStream(
            samplerate=self.config.sample_rate,
            blocksize=self.config.block_size,
            channels=1,
            device=self.config.device_index,
            dtype="float32",
            latency="low",
            callback=self._audio_callback,
        )
        self._stream.start()
    
    def stop(self) -> None:
        """Stop clap detection."""
        self._running = False
        if self._stream:
            stream, self._stream = self._stream, None
            try:
                stream.stop()
            finally:
                stream.close()
    
    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Process audio block."""
        if status:
            print(f"Audio status: {status}")

        raw_samples = indata[:, 0].astype(np.float32, copy=False)
        samples = np.abs(raw_samples)
        rms = float(np.sqrt(np.mean(samples ** 2)))
        peak = float(np.max(samples))
        local_floor = float(np.quantile(samples, 0.75))
        current_time = time.time()

        if not self._in_clap:
            quiet_rms = min(rms, self.config.rms_threshold * 1.5)
            self._noise_floor = (
                self._noise_floor * self.config.noise_floor_decay
                + quiet_rms * (1 - self.config.noise_floor_decay)
            )
            if quiet_rms > self._noise_floor_fast:
                self._noise_floor_fast = (
                    self._noise_floor_fast * (1.0 - self.config.noise_floor_fast_rise)
                    + quiet_rms * self.config.noise_floor_fast_rise
                )
            else:
                self._noise_floor_fast = (
                    self._noise_floor_fast * self.config.noise_floor_decay
                    + quiet_rms * (1 - self.config.noise_floor_decay)
                )

        noise_floor = max(self._noise_floor, self._noise_floor_fast, 1e-5)

        dynamic_rms_threshold = max(
            self.config.rms_threshold,
            noise_floor * self.config.adaptive_multiplier,
        )
        dynamic_peak_threshold = max(
            self.config.threshold,
            dynamic_rms_threshold * 2.5,
        )
        peak_to_rms = peak / max(rms, 1e-6)
        peak_to_floor = peak / max(local_floor, noise_floor, 1e-6)
        is_clap_block = (
            peak >= dynamic_peak_threshold
            and rms >= dynamic_rms_threshold * 0.35
            and peak_to_rms >= self.config.min_peak_to_rms
            and peak_to_floor >= 1.60
        )
        if is_clap_block and self.config.use_freq_gate:
            spectrum = np.abs(np.fft.rfft(raw_samples))
            total_energy = float((spectrum ** 2).sum()) + 1e-12
            band_energy = float((spectrum[self._freq_low_idx : self._freq_high_idx] ** 2).sum())
            if band_energy / total_energy < self.config.freq_band_ratio_min:
                is_clap_block = False

        block_duration = frames / self.config.sample_rate

        if is_clap_block and not self._in_clap:
            self._in_clap = True
            self._clap_start_time = current_time
            self._last_signal_time = current_time
            return

        if is_clap_block and self._in_clap:
            self._last_signal_time = current_time
            if current_time - self._clap_start_time > self.config.max_duration:
                self._in_clap = False
            return

        if self._in_clap and current_time - self._last_signal_time >= block_duration:
            clap_duration = (self._last_signal_time + block_duration) - self._clap_start_time
            self._in_clap = False
            if self.config.min_duration <= clap_duration <= self.config.max_duration:
                self._handle_clap(current_time)
    
    def _handle_clap(self, current_time: float) -> None:
        """Handle a detected clap."""
        # Check cooldown
        if current_time - self._last_clap_time < self.config.cooldown:
            return
        
        if self.config.double_clap:
            if self._single_clap_time is None:
                self._single_clap_time = current_time
            elif current_time - self._single_clap_time <= self.config.double_clap_window:
                self._single_clap_time = None
                self._last_clap_time = current_time
                self._trigger_callback()
            else:
                self._single_clap_time = current_time
        else:
            # Single clap mode
            self._last_clap_time = current_time
            self._trigger_callback()
    
    def _trigger_callback(self) -> None:
        """Trigger the on_clap callback in a separate thread."""
        threading.Thread(target=self.on_clap, daemon=True).start()
    
    @property
    def is_running(self) -> bool:
        return self._running


def list_audio_devices() -> list[dict]:
    """List available audio input devices."""
    sd = _sounddevice()
    devices = sd.query_devices()
    input_devices = []
    default_input = None

    try:
        default_input = sd.default.device[0]
    except (TypeError, IndexError):
        default_input = None
    
    for i, device in enumerate(devices):
        if device["max_input_channels"] > 0:
            input_devices.append({
                "index": i,
                "name": device["name"],
                "channels": device["max_input_channels"],
                "sample_rate": device["default_samplerate"],
                "is_default": i == default_input,
            })
    
    return input_devices
