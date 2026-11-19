"""Build a simple per-user Windows installer with IExpress."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from version_tools import read_version  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
VERSION = read_version()  # single source: src/__init__.py
APP_DIR = ROOT / "dist" / "WizLight"
PORTABLE_ZIP = ROOT / "dist" / f"WizLight-{VERSION}-portable.zip"
PAYLOAD_NAME = "WizLight-app.zip"
STAGING = ROOT / "build" / "installer-staging"
OUT_DIR = ROOT / "dist" / "installer"
SETUP_EXE = OUT_DIR / f"WizLight-{VERSION}-Setup.exe"
SED = ROOT / "build" / "WizLight-Setup.sed"


def make_portable_zip(app_dir: Path = APP_DIR, zip_path: Path = PORTABLE_ZIP) -> Path:
    """Zip the onedir build as ``WizLight/...`` (forward-slash entries, openable by any unzip tool)."""
    if not (app_dir / "WizLight.exe").is_file():
        raise FileNotFoundError(app_dir / "WizLight.exe")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = zip_path.with_name(zip_path.name + ".tmp")
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for folder, _dirs, names in os.walk(app_dir):
            for name in sorted(names):
                path = Path(folder) / name
                archive.write(path, Path(app_dir.name, path.relative_to(app_dir)).as_posix())
    os.replace(tmp_path, zip_path)
    return zip_path


def copy_payload() -> list[str]:
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    portable = make_portable_zip()
    shutil.copy2(portable, STAGING / PAYLOAD_NAME)

    files = [
        ROOT / "packaging" / "installer" / "install.cmd",
        ROOT / "packaging" / "installer" / "install.ps1",
        ROOT / "packaging" / "installer" / "uninstall.cmd",
        ROOT / "README.md",
        ROOT / "LICENSE",
    ]

    copied: list[str] = [PAYLOAD_NAME]
    for source in files:
        if not source.exists():
            raise FileNotFoundError(source)
        destination = STAGING / source.name
        shutil.copy2(source, destination)
        copied.append(source.name)
    return copied


def write_sed(file_names: list[str]) -> None:
    file_strings = "\n".join(
        f'FILE{index}="{name}"' for index, name in enumerate(file_names)
    )
    source_entries = "\n".join(f"%FILE{index}%=" for index in range(len(file_names)))
    content = f"""[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=0
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=
DisplayLicense=
FinishMessage=WizLight has been installed.
TargetName={SETUP_EXE}
FriendlyName=WizLight Setup
AppLaunched=install.cmd
PostInstallCmd=<None>
AdminQuietInstCmd=install.cmd
UserQuietInstCmd=install.cmd
SourceFiles=SourceFiles
[Strings]
{file_strings}
[SourceFiles]
SourceFiles0={STAGING}
[SourceFiles0]
{source_entries}
"""
    SED.parent.mkdir(parents=True, exist_ok=True)
    SED.write_text(content, encoding="utf-8")


def main() -> None:
    if "--zip-only" in sys.argv:
        print(f"Wrote {make_portable_zip()}")
        return
    file_names = copy_payload()
    write_sed(file_names)
    subprocess.run(["iexpress.exe", "/N", str(SED)], cwd=ROOT, check=True)
    if not SETUP_EXE.exists():
        raise RuntimeError(f"IExpress did not create {SETUP_EXE}")
    print(f"Wrote {SETUP_EXE}")


if __name__ == "__main__":
    main()
