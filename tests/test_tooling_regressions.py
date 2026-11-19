"""Software-only regressions for validation and packaging contracts."""

import argparse
import json
import shutil
import subprocess
import sys
import weakref
from pathlib import Path

import numpy as np
import pytest

from scripts import benchmark_screen_sync, measure_control_effects, run_hardware_color_accuracy
from scripts.fake_wiz_bulb_udp import validate_screen_sync_packet
from src.features.screen_sync_v2 import OptimizedScreenSync


ROOT = Path(__file__).resolve().parents[1]


def _packet(**updates):
    params = dict(state=True, r=12, g=20, b=30, c=0, w=0, dimming=60)
    params.update(updates)
    return {"method": "setPilot", "params": params}


def test_fake_receiver_accepts_mapped_white_channels():
    assert validate_screen_sync_packet(_packet(c=45, w=80)) == []
    assert validate_screen_sync_packet(_packet(state=False, r=0, g=0, b=0, c=0, w=0)) == []


@pytest.mark.parametrize("updates", [
    {"r": -1}, {"g": 256}, {"b": True}, {"c": 0.5}, {"w": "0"},
    {"dimming": 0}, {"dimming": 101}, {"dimming": True}, {"state": "true"},
    {"state": False}, {"r": 0, "g": 0, "b": 0, "c": 0, "w": 0},
])
def test_fake_receiver_rejects_invalid_output_contract(updates):
    assert validate_screen_sync_packet(_packet(**updates))


def test_hardware_environment_never_overrides_explicit_dry_run(monkeypatch):
    monkeypatch.setenv("RUN_HARDWARE_TESTS", "1")
    monkeypatch.setattr(run_hardware_color_accuracy, "parse_args", lambda: argparse.Namespace(
        manifest="unused", execute=False, target=None, output_dir=None,
    ))
    monkeypatch.setattr(run_hardware_color_accuracy, "load_manifest", lambda _: object())
    calls = []
    monkeypatch.setattr(run_hardware_color_accuracy, "run_manifest", lambda manifest, **kw: calls.append(kw) or 0)
    assert run_hardware_color_accuracy.main() == 0
    assert calls[0]["execute"] is False


def test_benchmark_streams_frames_and_measures_real_mapper(monkeypatch):
    refs = []
    max_live = 0
    mapped = []

    def frame(index, width, height):
        nonlocal max_live
        result = np.full((height, width, 3), 70 + index, dtype=np.uint8)
        refs.append(weakref.ref(result))
        max_live = max(max_live, sum(ref() is not None for ref in refs))
        return result

    class Mapper:
        def map_rgb(self, color):
            mapped.append(color)
            return (*color, 0, 0)

    monkeypatch.setattr(benchmark_screen_sync, "_synthetic_frame", frame)
    monkeypatch.setattr(benchmark_screen_sync, "BulbGamutMapper", lambda *args, **kw: Mapper(), raising=False)
    report = benchmark_screen_sync.benchmark(6, 96, 64, 24)
    assert max_live <= 2, "synthetic frames must not accumulate with iteration count"
    assert len(mapped) == 6
    assert report["scope"] == "synthetic software processing; excludes capture, UDP and bulb latency"
    assert "frameProcessingMs" in report["stages"]


def test_control_effect_gate_fails_when_runtime_fps_does_not_change(monkeypatch):
    monkeypatch.setattr(OptimizedScreenSync, "_update_adaptive_fps", lambda self, motion: setattr(self, "_current_fps", 10))
    report = measure_control_effects.measure()
    assert report["checks"]["responsiveness"]["passes"] is False
    assert report["summary"]["passed"] is False


@pytest.mark.parametrize("native_exit,stale", [(7, False), (0, True)])
def test_windows_build_rejects_native_failure_or_stale_artifacts(tmp_path, native_exit, stale):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell unavailable")
    source = (ROOT / "packaging/build_windows.ps1").read_text(encoding="utf-8")
    assert "PythonPath" in source, "build must permit an isolated interpreter stub"
    packaging = tmp_path / "packaging"
    packaging.mkdir()
    script = packaging / "build_windows.ps1"
    script.write_text(source, encoding="utf-8")
    stub = tmp_path / "stub-python.cmd"
    stub.write_text(f"@echo off\nexit /b {native_exit}\n", encoding="utf-8")
    if stale:
        output = tmp_path / "dist"
        (output / "installer").mkdir(parents=True)
        (output / "WizLight").mkdir()
        (output / "WizLight/WizLight.exe").write_bytes(b"old")
        (output / "installer/WizLight-Setup.exe").write_bytes(b"old")
    result = subprocess.run([shell, "-NoProfile", "-File", str(script), "-PythonPath", str(stub)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Build complete:" not in result.stdout
    if stale:
        assert (tmp_path / "dist/WizLight/WizLight.exe").read_bytes() == b"old"


def test_hardware_report_rejects_nonfinite_errors(tmp_path):
    manifest = run_hardware_color_accuracy.HardwareManifest(tmp_path / "video", tmp_path, ())
    target = run_hardware_color_accuracy.HardwareTarget("test", "127.0.0.1")
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"summary": {"scene_count": 1, "mean_error": float("nan"), "max_error": 1}}))
    assert run_hardware_color_accuracy.evaluate_report(path, manifest, target)[0] is False


@pytest.mark.parametrize("arguments, expected", [([], 0), (["--execute"], 2)])
def test_frozen_diagnostic_requires_explicit_execution_and_target(arguments, expected):
    result = subprocess.run([sys.executable, str(ROOT / "packaging/frozen_diag.py"), *arguments],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == expected
    assert "=== 1. imports ===" not in result.stdout


def test_uninstaller_preserves_unrelated_files_in_custom_install_directory(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None or sys.platform != "win32":
        pytest.skip("Windows PowerShell and WScript are required")
    source = (ROOT / "packaging/installer/install.ps1").read_text(encoding="utf-8")
    body = source.split("@'\n", 1)[1].split("\n'@", 1)[0]
    install = tmp_path / "install"
    menu = tmp_path / "menu"
    desktop = tmp_path / "desktop"
    for directory in (install, menu, desktop):
        directory.mkdir()
    (install / "WizLight.exe").write_bytes(b"test executable")
    (install / "_internal/sub").mkdir(parents=True)
    (install / "_internal/sub/lib.dll").write_bytes(b"dll")
    (install / "uninstall.cmd").write_text("test")
    (install / "other.txt").write_text("keep")
    (menu / "other.txt").write_text("keep")
    script = install / "uninstall.ps1"
    script.write_text(body, encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-File", str(script),
                             "-StartMenuDir", str(menu), "-DesktopDir", str(desktop)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert (install / "other.txt").read_text() == "keep"
    assert (menu / "other.txt").read_text() == "keep"
    assert not (install / "WizLight.exe").exists()
    assert not (install / "_internal").exists()
    assert not script.exists()


def test_onedir_installer_upgrades_cleanly_and_uninstalls_whole_folder(tmp_path):
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None or sys.platform != "win32":
        pytest.skip("Windows PowerShell and WScript are required")
    import zipfile

    source = tmp_path / "src dir"
    source.mkdir()
    shutil.copy2(ROOT / "packaging/installer/install.ps1", source / "install.ps1")
    with zipfile.ZipFile(source / "WizLight-app.zip", "w") as archive:
        archive.writestr("WizLight/WizLight.exe", b"new exe")
        archive.writestr("WizLight/_internal/lib/new.dll", b"dll")
    install = tmp_path / "install dir" / "Wiz Light"
    (install / "_internal").mkdir(parents=True)
    (install / "WizLight.exe").write_bytes(b"old onefile exe")
    (install / "_internal/stale.dll").write_bytes(b"stale")
    (install / "keep.txt").write_text("keep")
    menu, desktop = tmp_path / "menu", tmp_path / "desktop"
    desktop.mkdir()

    def run(script, *args):
        return subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
                              capture_output=True, text=True, timeout=60)

    for _ in range(2):  # fresh upgrade over an old layout, then reinstall over the new one
        result = run(source / "install.ps1", "-SourceDir", str(source), "-InstallDir", str(install),
                     "-StartMenuDir", str(menu), "-DesktopDir", str(desktop), "-NoLaunch")
        assert result.returncode == 0, result.stderr
        assert (install / "WizLight.exe").read_bytes() == b"new exe"
        assert (install / "_internal/lib/new.dll").is_file()
        assert not (install / "_internal/stale.dll").exists()
        assert (install / "keep.txt").read_text() == "keep"
        assert (menu / "WizLight.lnk").is_file() and (desktop / "WizLight.lnk").is_file()
    (install / "keep.txt").unlink()
    result = run(install / "uninstall.ps1", "-StartMenuDir", str(menu), "-DesktopDir", str(desktop))
    assert result.returncode == 0, result.stderr
    assert not install.exists()
    assert not (menu / "WizLight.lnk").exists() and not (desktop / "WizLight.lnk").exists()
