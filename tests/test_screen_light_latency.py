"""Tests for pairing observed screen and lamp transitions."""

import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from scripts.analyze_screen_light_latency import overlaps, pair_transitions, parse_roi, score, summarize_transition_delays, transitions
from scripts.measure_screen_light_camera import summarize


def test_hysteresis_requires_stable_frames_and_dates_the_first_frame():
    signal = [0.8] * 4 + [-0.8] + [0.8] * 2 + [-0.8] * 3 + [0.8] * 2
    assert transitions(signal, threshold=0.2, stable_frames=2) == [(7, -1), (10, 1)]


def test_pairs_only_matching_changes_before_the_next_screen_cut():
    screen = [(10, -1), (30, 1), (50, -1)]
    light = [(8, 1), (13, -1), (33, 1), (80, -1)]
    assert pair_transitions(screen, light, max_frames=20) == [(10, 13, -1), (30, 33, 1)]


def test_reports_late_and_missing_transitions_separately():
    times = [i * .1 for i in range(80)]
    screen = [(10, -1), (30, 1), (60, -1)]
    light = [(12, -1), (49, 1)]
    result = summarize_transition_delays(screen, light, times, 1500)
    assert result["matched_transitions"] == 1
    assert result["late_transitions"] == 1
    assert result["late_samples_ms"] == pytest.approx([1900])
    assert result["unobserved_before_next_transition_or_end"] == 1
    assert result["unmatched_screen_transitions"] == 2
    assert result["all_observed_median_ms"] == pytest.approx(1050)
    assert result["all_observed_max_ms"] == pytest.approx(1900)


def test_roi_score_and_bounds():
    frame = np.zeros((5, 6, 3), dtype=np.uint8)
    frame[1:3, 2:5, 2] = 200
    assert score(frame, parse_roi("2,1,3,2")) == 1.0
    with pytest.raises(ValueError):
        score(frame, (5, 4, 2, 2))
    assert not overlaps((0, 0, 2, 2), (2, 0, 2, 2))
    assert overlaps((0, 0, 3, 2), (2, 0, 2, 2))


def test_video_analysis_reports_known_frame_delay(tmp_path):
    video = tmp_path / "known-delay.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (64, 32))
    if not writer.isOpened():
        pytest.skip("MJPEG encoder unavailable")
    try:
        for index in range(180):
            screen_red = (index // 30) % 2 == 0
            light_red = (max(0, index - 6) // 30) % 2 == 0
            frame = np.zeros((32, 64, 3), dtype=np.uint8)
            frame[:, :32] = (0, 0, 255) if screen_red else (255, 0, 0)
            frame[:, 32:] = (0, 0, 255) if light_red else (255, 0, 0)
            writer.write(frame)
    finally:
        writer.release()
    script = Path(__file__).resolve().parents[1] / "scripts/analyze_screen_light_latency.py"
    result = subprocess.run([sys.executable, str(script), str(video),
                             "--screen-roi", "0,0,32,32", "--light-roi", "32,0,32,32"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["matched_transitions"] == 5
    assert report["median_ms"] == pytest.approx(200.0)
    late = subprocess.run([sys.executable, str(script), str(video),
                           "--screen-roi", "0,0,32,32", "--light-roi", "32,0,32,32",
                           "--max-latency-ms", "100"], capture_output=True, text=True, timeout=20)
    assert late.returncode == 1
    late_report = json.loads(late.stdout)
    assert late_report["late_transitions"] == 5
    assert late_report["all_observed_max_ms"] == pytest.approx(200)
    assert late_report["unobserved_before_next_transition_or_end"] == 0


def test_timestamped_measurement_uses_real_frame_intervals():
    samples = [
        (0.00, 1, 1), (0.03, 1, 1), (0.08, -1, 1), (0.11, -1, 1),
        (0.18, -1, -1), (0.22, -1, -1),
    ]
    report = summarize(samples)
    assert report["matched_transitions"] == 1
    assert report["median_ms"] == pytest.approx(100)
    assert report["frame_interval_max_ms"] == pytest.approx(70)
    assert summarize([(0.0, 1, 1), (0.0, -1, -1)])["equal_timestamp_intervals"] == 1
    with pytest.raises(ValueError):
        summarize([(0.1, 1, 1), (0.0, -1, -1)])
