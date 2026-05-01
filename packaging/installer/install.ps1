param(
    [string]$SourceDir = $PSScriptRoot,
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\WizLight"
)

$ErrorActionPreference = "Stop"

$appSource = Join-Path $SourceDir "WizLight.exe"
if (-not (Test-Path -LiteralPath $appSource)) {
    throw "WizLight.exe was not found next to the installer script."
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -LiteralPath $appSource -Destination (Join-Path $InstallDir "WizLight.exe") -Force

$readme = Join-Path $SourceDir "README.md"
if (Test-Path -LiteralPath $readme) {
    Copy-Item -LiteralPath $readme -Destination (Join-Path $InstallDir "README.md") -Force
}

$license = Join-Path $SourceDir "LICENSE"
if (Test-Path -LiteralPath $license) {
    Copy-Item -LiteralPath $license -Destination (Join-Path $InstallDir "LICENSE") -Force
}

$shell = New-Object -ComObject WScript.Shell
$startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\WizLight"
New-Item -ItemType Directory -Force -Path $startMenuDir | Out-Null

$appShortcut = $shell.CreateShortcut((Join-Path $startMenuDir "WizLight.lnk"))
$appShortcut.TargetPath = Join-Path $InstallDir "WizLight.exe"
$appShortcut.WorkingDirectory = $InstallDir
$appShortcut.Description = "WizLight desktop controller"
$appShortcut.Save()

$desktopShortcut = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "WizLight.lnk"))
$desktopShortcut.TargetPath = Join-Path $InstallDir "WizLight.exe"
$desktopShortcut.WorkingDirectory = $InstallDir
$desktopShortcut.Description = "WizLight desktop controller"
$desktopShortcut.Save()

$uninstallScript = Join-Path $InstallDir "uninstall.ps1"
@'
$ErrorActionPreference = "SilentlyContinue"
Remove-Item -LiteralPath "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\WizLight" -Recurse -Force
Remove-Item -LiteralPath "$([Environment]::GetFolderPath("Desktop"))\WizLight.lnk" -Force
Remove-Item -LiteralPath "$env:LOCALAPPDATA\Programs\WizLight" -Recurse -Force
'@ | Set-Content -LiteralPath $uninstallScript -Encoding UTF8

Write-Host "WizLight installed to $InstallDir"
Start-Process -FilePath (Join-Path $InstallDir "WizLight.exe")
