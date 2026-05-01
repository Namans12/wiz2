$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

Set-Location $RepoRoot

& $Python packaging\make_icon.py
& $Python -m PyInstaller --clean --noconfirm packaging\WizLight.spec
& $Python packaging\make_iexpress_installer.py

Write-Host ""
Write-Host "Build complete:"
Write-Host "  App:       $RepoRoot\dist\WizLight.exe"
Write-Host "  Installer: $RepoRoot\dist\installer\WizLight-Setup.exe"
