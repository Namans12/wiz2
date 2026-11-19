#!/usr/bin/env python3
"""Set the WizLight version everywhere in one go.

    python scripts/bump_version.py 1.1.0              # keeps the Android build number
    python scripts/bump_version.py 1.1.0 --build 7    # also sets the Android build number

``src/__init__.py`` is the source of truth.  ``pyproject.toml`` (dynamic version), the Windows
version resource and the macOS bundle all read it at build time, so only two files hold a
literal: ``src/__init__.py``, ``wizlight-android/pubspec.yaml`` and the ``'appVersion'`` string
in ``wizlight-android/lib/screens/home_screen.dart`` (the Android diagnostics export).  Re-running with the current
version changes nothing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_FILE = ROOT / "src" / "__init__.py"
PUBSPEC = ROOT / "wizlight-android" / "pubspec.yaml"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
INIT_RE = re.compile(r"""^(__version__\s*=\s*)["'][^"']*["']""", re.MULTILINE)
PUBSPEC_RE = re.compile(r"^version:[ \t]*(\d+\.\d+\.\d+)(?:\+(\d+))?[ \t]*(?=\r?$)", re.MULTILINE)
HOME_SCREEN_RE = re.compile(r"""('appVersion':\s*')(\d+\.\d+\.\d+\+\d+)(')""")


def _rewrite(path: Path, pattern: re.Pattern[str], replacement, label: str) -> bool:
    # Work on bytes-preserving text so CRLF/LF line endings survive untouched.
    raw = path.read_bytes().decode("utf-8")
    new, count = pattern.subn(replacement, raw, count=1)
    if count != 1:
        raise SystemExit(f"error: could not find the version line in {path}")
    if new == raw:
        print(f"unchanged  {label}")
        return False
    path.write_bytes(new.encode("utf-8"))
    print(f"updated    {label}")
    return True


def bump(version: str, build: int | None = None, root: Path = ROOT) -> bool:
    if not VERSION_RE.match(version):
        raise SystemExit(f"error: {version!r} is not MAJOR.MINOR.PATCH")
    init_file = root / "src" / "__init__.py"
    pubspec = root / "wizlight-android" / "pubspec.yaml"
    home_screen = root / "wizlight-android" / "lib" / "screens" / "home_screen.dart"

    full_version: list[str] = []

    def pubspec_line(match: re.Match[str]) -> str:
        number = str(build) if build is not None else match.group(2) or "1"
        full_version.append(f"{version}+{number}")
        return f"version: {full_version[0]}"

    changed = _rewrite(init_file, INIT_RE, lambda m: f'{m.group(1)}"{version}"', "src/__init__.py")
    changed |= _rewrite(pubspec, PUBSPEC_RE, pubspec_line, "wizlight-android/pubspec.yaml")
    # The Android diagnostics export reports the same "version+build" string as a literal.
    changed |= _rewrite(
        home_screen,
        HOME_SCREEN_RE,
        lambda m: f"{m.group(1)}{full_version[0]}{m.group(3)}",
        "wizlight-android/lib/screens/home_screen.dart",
    )
    print("pyproject.toml, Windows version resource and macOS bundle read src/__init__.py at build time.")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="new version as MAJOR.MINOR.PATCH")
    parser.add_argument("--build", type=int, help="Android build number (default: keep the current one)")
    args = parser.parse_args(argv)
    bump(args.version, args.build)
    return 0


if __name__ == "__main__":
    sys.exit(main())
