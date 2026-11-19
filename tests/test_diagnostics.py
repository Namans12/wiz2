from src.core.diagnostics import build_diagnostics_report


def test_diagnostics_report_is_json_safe_and_contains_runtime_stats():
    report = build_diagnostics_report(
        platform="windows-desktop",
        app_version="0.1.0",
        bulbs=[{"name": "Desk", "ip": "192.168.1.4", "mac": None}],
        screen_sync_settings={"max_fps": 28, "smoothing": 0.2},
        runtime_stats={
            "targetColors": {"all": (10, 20, 30)},
            "outputColors": {"all": (12, 22, 32)},
            "requestedFps": 28,
        },
        notes=["test"],
    )

    assert report["schemaVersion"] == 1
    assert report["platform"] == "windows-desktop"
    assert report["runtimeStats"]["targetColors"]["all"] == [10, 20, 30]
    assert report["screenSyncSettings"]["max_fps"] == 28
    assert report["notes"] == ["test"]
