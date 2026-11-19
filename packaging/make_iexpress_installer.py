"""Build a simple per-user Windows installer with IExpress."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "build" / "installer-staging"
OUT_DIR = ROOT / "dist" / "installer"
SETUP_EXE = OUT_DIR / "WizLight-Setup.exe"
SED = ROOT / "build" / "WizLight-Setup.sed"


def copy_payload() -> list[str]:
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    files = [
        ROOT / "dist" / "WizLight.exe",
        ROOT / "packaging" / "installer" / "install.cmd",
        ROOT / "packaging" / "installer" / "install.ps1",
        ROOT / "packaging" / "installer" / "uninstall.cmd",
        ROOT / "README.md",
        ROOT / "LICENSE",
    ]

    copied: list[str] = []
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
    file_names = copy_payload()
    write_sed(file_names)
    subprocess.run(["iexpress.exe", "/N", str(SED)], cwd=ROOT, check=True)
    if not SETUP_EXE.exists():
        raise RuntimeError(f"IExpress did not create {SETUP_EXE}")
    print(f"Wrote {SETUP_EXE}")


if __name__ == "__main__":
    main()
