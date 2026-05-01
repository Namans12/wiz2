import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.run_hardware_color_accuracy import (
    build_verify_command,
    evaluate_report,
    load_manifest,
    run_manifest,
)


def write_manifest(tmp_path: Path) -> Path:
    manifest = {
        "video": str(tmp_path / "sample.mp4"),
        "output_dir": str(tmp_path / "runs"),
        "mean_error_threshold": 42.0,
        "max_error_threshold": 80.0,
        "targets": [
            {
                "name": "left-bulb",
                "ip": "192.168.1.3",
                "camera": 1,
                "roi": [10, 20, 30, 40],
            }
        ],
    }
    path = tmp_path / "hardware_targets.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_hardware_manifest_builds_verifier_command(tmp_path):
    manifest = load_manifest(write_manifest(tmp_path))
    command = build_verify_command(manifest, manifest.targets[0], tmp_path / "out")

    assert command[0] == sys.executable
    assert "scripts\\verify_video_color_mapping.py" in command[1] or (
        "scripts/verify_video_color_mapping.py" in command[1]
    )
    assert "--ip" in command
    assert "192.168.1.3" in command
    assert "--camera" in command
    assert "1" in command
    assert "--roi" in command
    assert "10,20,30,40" in command


def test_hardware_report_threshold_evaluation(tmp_path):
    manifest = load_manifest(write_manifest(tmp_path))
    report = {
        "summary": {
            "scene_count": 3,
            "mean_error": 32.5,
            "max_error": 70.0,
        }
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    passed, message = evaluate_report(report_path, manifest, manifest.targets[0])

    assert passed is True
    assert "mean=32.5" in message


def test_hardware_report_fails_when_error_exceeds_threshold(tmp_path):
    manifest = load_manifest(write_manifest(tmp_path))
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps({"summary": {"scene_count": 2, "mean_error": 50.0, "max_error": 70.0}}),
        encoding="utf-8",
    )

    passed, message = evaluate_report(report_path, manifest, manifest.targets[0])

    assert passed is False
    assert "mean error" in message


def test_hardware_runner_dry_run_does_not_require_video(tmp_path, capsys):
    manifest = load_manifest(write_manifest(tmp_path))

    exit_code = run_manifest(manifest, execute=False)

    assert exit_code == 0
    assert "Dry run only" in capsys.readouterr().out


@pytest.mark.hardware
def test_real_bulb_color_accuracy_manifest():
    if os.environ.get("RUN_HARDWARE_TESTS") != "1":
        pytest.skip("Set RUN_HARDWARE_TESTS=1 to run real bulb/camera verification.")
    manifest = Path("hardware_targets.json")
    if not manifest.exists():
        pytest.skip("Create hardware_targets.json from hardware_targets.example.json first.")

    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_hardware_color_accuracy.py",
            "--manifest",
            str(manifest),
            "--execute",
        ],
        check=False,
    )

    assert result.returncode == 0
