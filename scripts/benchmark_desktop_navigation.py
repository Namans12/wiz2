"""Measure synthetic desktop page-switch time; does not control lights."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.config import Config  # noqa: E402
from src.gui import modern_app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=60)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")

    with tempfile.TemporaryDirectory(prefix="wizlight-ui-benchmark-") as directory:
        config = Config(_config_path=Path(directory) / "config.json")
        modern_app.Config.load = classmethod(lambda cls, path=None: config)
        modern_app.is_tray_available = lambda: False
        app = modern_app.WizLightModernGUI()
        try:
            app.root.update()
            samples = []
            for index in range(args.iterations):
                started = time.perf_counter()
                app._switch_view(("sync", "lights", "tools")[index % 3])
                app.root.update()
                samples.append((time.perf_counter() - started) * 1000)
            ordered = sorted(samples)
            print(json.dumps({
                "scope": "synthetic page navigation and Tk event processing; no capture, network or lights",
                "iterations": len(samples),
                "median_ms": round(statistics.median(samples), 2),
                "p95_ms": round(ordered[min(len(ordered) - 1, (95 * len(ordered) + 99) // 100 - 1)], 2),
                "max_ms": round(max(samples), 2),
            }, indent=2))
        finally:
            app._on_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
