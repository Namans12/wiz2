"""Diagnostics report helpers for support and production validation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def build_diagnostics_report(
    *,
    platform: str,
    app_version: str,
    bulbs: list[dict[str, Any]],
    screen_sync_settings: dict[str, Any],
    runtime_stats: dict[str, Any] | None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Build a compact JSON-safe diagnostics payload."""

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "platform": platform,
        "appVersion": app_version,
        "bulbs": _json_safe(bulbs),
        "screenSyncSettings": _json_safe(screen_sync_settings),
        "runtimeStats": _json_safe(runtime_stats or {}),
        "notes": notes or [],
    }
