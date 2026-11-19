"""Software timing statistics are distinct from light response latency."""

import pytest

from src.features.screen_sync_v2 import OptimizedCaptureConfig, OptimizedScreenSync


def test_timing_snapshot_reports_sampled_averages_and_tail():
    engine = OptimizedScreenSync(lambda colors: None, OptimizedCaptureConfig())
    assert engine.debug_snapshot["capture_time_ms"] is None
    assert engine.debug_snapshot["timing_samples"] == 0
    with engine._stats_lock:
        engine._capture_times.extend([0.001, 0.003])
        engine._processing_times.extend([0.004, 0.006])
    snapshot = engine.debug_snapshot
    assert snapshot["timing_samples"] == 2
    assert snapshot["capture_time_ms"] == pytest.approx(2.0)
    assert snapshot["capture_time_p95_ms"] == pytest.approx(2.9)
    assert snapshot["processing_time_ms"] == pytest.approx(5.0)
    assert snapshot["processing_time_p95_ms"] == pytest.approx(5.9)
