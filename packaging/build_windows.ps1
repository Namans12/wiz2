param([string]$PythonPath)
$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = if ($PythonPath) { $PythonPath } else { Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe" }
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

Set-Location $RepoRoot

function Assert-NativeSuccess([string]$Stage) {
    if ($LASTEXITCODE -ne 0) { throw "$Stage failed with exit code $LASTEXITCODE" }
}
function Assert-FreshArtifact([string]$Path, [datetime]$Started) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Artifact not produced: $Path" }
    $artifact = Get-Item -LiteralPath $Path
    if ($artifact.Length -eq 0 -or $artifact.LastWriteTimeUtc -lt $Started) {
        throw "Artifact is empty or stale: $Path"
    }
}
$started = [datetime]::UtcNow
$Version = (& $Python packaging\version_tools.py --print | Select-Object -Last 1)
Assert-NativeSuccess 'Version lookup'
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Bad version from src/__init__.py: $Version" }
Write-Host "Building WizLight $Version"
& $Python packaging\make_icon.py
Assert-NativeSuccess 'Icon generation'
& $Python -m PyInstaller --clean --noconfirm packaging\WizLight.spec
Assert-NativeSuccess 'PyInstaller'
Assert-FreshArtifact (Join-Path $RepoRoot 'dist\WizLight\WizLight.exe') $started
& $Python packaging\make_iexpress_installer.py
Assert-NativeSuccess 'Installer generation'
Assert-FreshArtifact (Join-Path $RepoRoot "dist\WizLight-$Version-portable.zip") $started
Assert-FreshArtifact (Join-Path $RepoRoot "dist\installer\WizLight-$Version-Setup.exe") $started

Write-Host ""
Write-Host "Build complete:"
Write-Host "  App:       $RepoRoot\dist\WizLight\WizLight.exe (onedir)"
Write-Host "  Portable:  $RepoRoot\dist\WizLight-$Version-portable.zip"
Write-Host "  Installer: $RepoRoot\dist\installer\WizLight-$Version-Setup.exe"
