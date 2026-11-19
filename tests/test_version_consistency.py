"""Every place that carries the WizLight version must agree with ``src.__version__``."""

from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path

import pytest

import src

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


version_tools = _load("wizlight_version_tools", "packaging/version_tools.py")
bump_version = _load("wizlight_bump_version", "scripts/bump_version.py")
iexpress = _load("wizlight_make_iexpress_installer", "packaging/make_iexpress_installer.py")


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_version_is_plain_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", src.__version__)


def test_packaging_helper_reads_the_package_version():
    assert version_tools.read_version() == src.__version__


def test_pyproject_reads_version_from_the_package():
    text = _text("pyproject.toml")
    assert 'dynamic = ["version"]' in text
    assert 'version = {attr = "src.__version__"}' in text
    # No stray literal version in [project].
    project = text.split("[project.optional-dependencies]")[0]
    assert not re.search(r'^version\s*=', project, re.MULTILINE)


def test_android_pubspec_matches_with_a_build_number():
    match = re.search(r"^version:\s*(\S+)\s*$", _text("wizlight-android/pubspec.yaml"), re.MULTILINE)
    assert match, "pubspec.yaml has no version"
    name, _, build = match.group(1).partition("+")
    assert name == src.__version__
    assert build.isdigit() and int(build) >= 1


def test_macos_spec_reads_the_version_instead_of_hard_coding_it():
    spec = _text("packaging/WizLight.macos.spec")
    assert "read_version" in spec
    assert not re.search(r"""["']\d+\.\d+\.\d+["']""", spec)


def test_windows_spec_embeds_a_version_resource():
    spec = _text("packaging/WizLight.spec")
    assert "version=" in spec and "write_windows_version_file" in spec


def test_windows_version_resource_carries_the_version():
    text = version_tools.windows_version_info(src.__version__)
    numeric = version_tools.version_tuple(src.__version__)
    assert f"filevers={numeric!r}" in text and f"prodvers={numeric!r}" in text
    assert f"StringStruct('ProductVersion', '{src.__version__}')" in text
    namespace = {}
    pytest.importorskip("PyInstaller")
    from PyInstaller.utils.win32 import versioninfo

    exec(
        compile(text, "version_info.txt", "exec"),
        {name: getattr(versioninfo, name) for name in dir(versioninfo)},
        namespace,
    )


def test_installer_and_portable_names_carry_the_version():
    assert iexpress.VERSION == src.__version__
    assert iexpress.PORTABLE_ZIP.name == f"WizLight-{src.__version__}-portable.zip"
    assert iexpress.SETUP_EXE.name == f"WizLight-{src.__version__}-Setup.exe"
    build_script = _text("packaging/build_windows.ps1")
    assert "-Setup.exe" in build_script and "-portable.zip" in build_script
    assert "WizLight-Setup.exe" not in build_script and "WizLight-portable.zip" not in build_script


BUMPED_FILES = (
    "src/__init__.py",
    "wizlight-android/pubspec.yaml",
    "wizlight-android/lib/screens/home_screen.dart",
)


def test_bump_version_is_a_noop_for_the_current_version(tmp_path):
    for relative in BUMPED_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    before = {p: (tmp_path / p).read_bytes() for p in BUMPED_FILES}
    assert bump_version.bump(src.__version__, root=tmp_path) is False
    assert {p: (tmp_path / p).read_bytes() for p in before} == before


def test_bump_version_updates_all_literals_and_keeps_line_endings(tmp_path):
    init = tmp_path / "src/__init__.py"
    pubspec = tmp_path / "wizlight-android/pubspec.yaml"
    home = tmp_path / "wizlight-android/lib/screens/home_screen.dart"
    for path in (init, pubspec, home):
        path.parent.mkdir(parents=True, exist_ok=True)
    init.write_bytes(b'"""Doc."""\r\n\r\n__version__ = "1.0.0"\r\n')
    pubspec.write_bytes(b"name: x\r\nversion: 1.0.0+4\r\nenvironment:\r\n")
    home.write_bytes(b"final r = {\r\n  'appVersion': '1.0.0+4',\r\n  'bulbs': 1,\r\n};\r\n")
    assert bump_version.bump("2.3.4", root=tmp_path) is True
    assert init.read_bytes() == b'"""Doc."""\r\n\r\n__version__ = "2.3.4"\r\n'
    assert pubspec.read_bytes() == b"name: x\r\nversion: 2.3.4+4\r\nenvironment:\r\n"
    assert home.read_bytes() == b"final r = {\r\n  'appVersion': '2.3.4+4',\r\n  'bulbs': 1,\r\n};\r\n"
    bump_version.bump("2.3.4", build=9, root=tmp_path)
    assert b"version: 2.3.4+9\r\n" in pubspec.read_bytes()
    assert b"'appVersion': '2.3.4+9'," in home.read_bytes()
    assert b"\r\n" in home.read_bytes() and b"\n" not in home.read_bytes().replace(b"\r\n", b"")


def test_bump_version_rejects_bad_versions(tmp_path):
    with pytest.raises(SystemExit):
        bump_version.bump("1.0", root=tmp_path)


def test_gui_diagnostics_use_the_package_version_not_a_literal():
    source = _text("src/gui/modern_app.py")
    assert "app_version=__version__" in source
    assert not re.search(r"""app_version\s*=\s*["']""", source)


def test_android_reported_app_version_matches_pubspec():
    pubspec = re.search(r"^version:\s*(\S+)\s*$", _text("wizlight-android/pubspec.yaml"), re.MULTILINE).group(1)
    literal = re.search(r"'appVersion':\s*'([^']+)'", _text("wizlight-android/lib/screens/home_screen.dart"))
    assert literal and literal.group(1) == pubspec


def test_macos_dmg_name_carries_the_version():
    script = _text("packaging/build_macos.sh")
    assert "version_tools.py --print" in script
    assert "WizLight-${VERSION}-macOS.dmg" in script
    assert "WizLight-macOS.dmg" not in script
