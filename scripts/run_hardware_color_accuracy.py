#!/usr/bin/env python3
"""Run opt-in real-bulb color accuracy checks from a local manifest."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "hardware_targets.json"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_video_color_mapping.py"


@dataclass(frozen=True)
class HardwareTarget:
    name: str
    ip: str
    camera: int | None = None
    roi: tuple[int, int, int, int] | None = None
    mean_error_threshold: float | None = None
    max_error_threshold: float | None = None


@dataclass(frozen=True)
class HardwareManifest:
    video: Path
    output_dir: Path
    targets: tuple[HardwareTarget, ...]
    mean_error_threshold: float = 95.0
    max_error_threshold: float = 170.0
    sample_step: float = 1.0
    scene_gap: float = 4.0
    scene_threshold: float = 22.0
    max_scenes: int = 10
    settle: float = 0.45
    frames: int = 6
    warmup_frames: int = 4
    command_timeout: float = 8.0
    retries: int = 3
    retry_delay: float = 0.4
    camera_stabilize_seconds: float = 4.0
    ignore_tone_lut: bool = False
    save_tone_lut: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run hardware-gated WiZ bulb color accuracy checks."
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
        help="Path to hardware_targets.json.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually control bulbs and camera. Without this, only prints planned runs.",
    )
    parser.add_argument(
        "--target",
        action="append",
        help="Optional target name to run. May be passed multiple times.",
    )
    parser.add_argument(
        "--output-dir",
        help="Override the manifest output directory.",
    )
    return parser.parse_args()


def _manifest_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path)


def _read_float(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{key} must be a number")
    return float(value)


def _read_int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if type(value) is not int:
        raise ValueError(f"{key} must be an integer")
    return value


def _read_roi(raw: Any) -> tuple[int, int, int, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError("roi must be a four-item list: [x, y, width, height]")
    roi = tuple(int(value) for value in raw)
    if roi[2] <= 0 or roi[3] <= 0:
        raise ValueError("roi width and height must be positive")
    return roi


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-")
    return slug or "target"


def load_manifest(path: str | Path) -> HardwareManifest:
    manifest_path = _manifest_path(path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Hardware manifest must be a JSON object")

    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("Hardware manifest must contain at least one target")

    targets: list[HardwareTarget] = []
    for index, raw_target in enumerate(raw_targets, start=1):
        if not isinstance(raw_target, dict):
            raise ValueError(f"targets[{index}] must be a JSON object")
        ip = str(raw_target.get("ip", "")).strip()
        if not ip:
            raise ValueError(f"targets[{index}].ip is required")
        name = str(raw_target.get("name") or f"bulb-{index}")
        camera = raw_target.get("camera")
        if camera is not None and not isinstance(camera, int):
            raise ValueError(f"targets[{index}].camera must be an integer")
        targets.append(
            HardwareTarget(
                name=name,
                ip=ip,
                camera=camera,
                roi=_read_roi(raw_target.get("roi")),
                mean_error_threshold=(
                    _read_float(raw_target, "mean_error_threshold", 0.0)
                    if "mean_error_threshold" in raw_target
                    else None
                ),
                max_error_threshold=(
                    _read_float(raw_target, "max_error_threshold", 0.0)
                    if "max_error_threshold" in raw_target
                    else None
                ),
            )
        )

    video = _manifest_path(str(data.get("video", "")))
    if not str(data.get("video", "")).strip():
        raise ValueError("video is required")

    output_dir = _manifest_path(str(data.get("output_dir", "output/hardware-runs")))

    return HardwareManifest(
        video=video,
        output_dir=output_dir,
        targets=tuple(targets),
        mean_error_threshold=_read_float(data, "mean_error_threshold", 95.0),
        max_error_threshold=_read_float(data, "max_error_threshold", 170.0),
        sample_step=_read_float(data, "sample_step", 1.0),
        scene_gap=_read_float(data, "scene_gap", 4.0),
        scene_threshold=_read_float(data, "scene_threshold", 22.0),
        max_scenes=_read_int(data, "max_scenes", 10),
        settle=_read_float(data, "settle", 0.45),
        frames=_read_int(data, "frames", 6),
        warmup_frames=_read_int(data, "warmup_frames", 4),
        command_timeout=_read_float(data, "command_timeout", 8.0),
        retries=_read_int(data, "retries", 3),
        retry_delay=_read_float(data, "retry_delay", 0.4),
        camera_stabilize_seconds=_read_float(data, "camera_stabilize_seconds", 4.0),
        ignore_tone_lut=bool(data.get("ignore_tone_lut", False)),
        save_tone_lut=bool(data.get("save_tone_lut", False)),
    )


def build_verify_command(
    manifest: HardwareManifest,
    target: HardwareTarget,
    output_dir: Path,
) -> list[str]:
    command = [
        sys.executable,
        str(VERIFY_SCRIPT),
        str(manifest.video),
        "--ip",
        target.ip,
        "--sample-step",
        str(manifest.sample_step),
        "--scene-gap",
        str(manifest.scene_gap),
        "--scene-threshold",
        str(manifest.scene_threshold),
        "--max-scenes",
        str(manifest.max_scenes),
        "--settle",
        str(manifest.settle),
        "--frames",
        str(manifest.frames),
        "--warmup-frames",
        str(manifest.warmup_frames),
        "--command-timeout",
        str(manifest.command_timeout),
        "--retries",
        str(manifest.retries),
        "--retry-delay",
        str(manifest.retry_delay),
        "--output-dir",
        str(output_dir),
        "--camera-stabilize-seconds",
        str(manifest.camera_stabilize_seconds),
    ]
    if target.camera is not None:
        command.extend(["--camera", str(target.camera)])
    if target.roi is not None:
        command.extend(["--roi", ",".join(str(value) for value in target.roi)])
    if manifest.ignore_tone_lut:
        command.append("--ignore-tone-lut")
    if manifest.save_tone_lut:
        command.append("--save-tone-lut")
    return command


def expected_report_path(output_dir: Path, video: Path) -> Path:
    return output_dir / video.stem / "verification_report.json"


def evaluate_report(
    report_path: Path,
    manifest: HardwareManifest,
    target: HardwareTarget,
) -> tuple[bool, str]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = report.get("summary", {})
    if not isinstance(summary, dict):
        return False, "report has no summary"
    mean_error = summary.get("mean_error")
    max_error = summary.get("max_error")
    scene_count = summary.get("scene_count")
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in (mean_error, max_error)):
        return False, "report does not contain finite nonnegative mean/max errors"
    if type(scene_count) is not int or scene_count <= 0:
        return False, "report has no measured scenes"

    mean_limit = manifest.mean_error_threshold if target.mean_error_threshold is None else target.mean_error_threshold
    max_limit = manifest.max_error_threshold if target.max_error_threshold is None else target.max_error_threshold
    if mean_error > mean_limit:
        return False, f"mean error {mean_error:.1f} exceeded threshold {mean_limit:.1f}"
    if max_error > max_limit:
        return False, f"max error {max_error:.1f} exceeded threshold {max_limit:.1f}"
    return True, f"mean={mean_error:.1f}, max={max_error:.1f}, scenes={scene_count}"


def select_targets(
    manifest: HardwareManifest,
    target_names: list[str] | None,
) -> tuple[HardwareTarget, ...]:
    if not target_names:
        return manifest.targets
    wanted = set(target_names)
    selected = tuple(target for target in manifest.targets if target.name in wanted)
    missing = wanted - {target.name for target in selected}
    if missing:
        raise ValueError(f"Unknown target(s): {', '.join(sorted(missing))}")
    return selected


def run_manifest(
    manifest: HardwareManifest,
    *,
    execute: bool,
    target_names: list[str] | None = None,
    output_dir_override: str | None = None,
) -> int:
    selected_targets = select_targets(manifest, target_names)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_output = _manifest_path(output_dir_override) if output_dir_override else manifest.output_dir
    base_output = base_output / run_id

    if execute and not manifest.video.exists():
        raise FileNotFoundError(f"Video file does not exist: {manifest.video}")

    failures: list[str] = []
    for target in selected_targets:
        target_output = base_output / _slug(target.name)
        command = build_verify_command(manifest, target, target_output)
        printable = " ".join(f'"{part}"' if " " in part else part for part in command)
        print(f"\nTarget: {target.name} ({target.ip})")
        print(f"Command: {printable}")
        if not execute:
            continue

        result = subprocess.run(command, cwd=REPO_ROOT, check=False)
        if result.returncode != 0:
            failures.append(f"{target.name}: verifier exited with {result.returncode}")
            continue

        report_path = expected_report_path(target_output, manifest.video)
        if not report_path.exists():
            failures.append(f"{target.name}: expected report missing at {report_path}")
            continue
        passed, message = evaluate_report(report_path, manifest, target)
        status = "PASS" if passed else "FAIL"
        print(f"{status}: {target.name}: {message}")
        if not passed:
            failures.append(f"{target.name}: {message}")

    if failures:
        print("\nHardware color accuracy failures:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    if not execute:
        print("\nDry run only. Pass --execute to control bulbs and camera.")
    return 0


def main() -> int:
    args = parse_args()
    manifest = load_manifest(args.manifest)
    return run_manifest(
        manifest,
        execute=args.execute,
        target_names=args.target,
        output_dir_override=args.output_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
