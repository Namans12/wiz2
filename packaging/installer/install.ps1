param(
    [string]$SourceDir = $PSScriptRoot,
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\WizLight"
)

$ErrorActionPreference = "Stop"
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
if ($InstallDir -eq [System.IO.Path]::GetPathRoot($InstallDir) -or
    $InstallDir -eq [Environment]::GetFolderPath('UserProfile') -or
    $InstallDir -eq [Environment]::GetFolderPath('Desktop') -or
    $InstallDir -eq [Environment]::GetFolderPath('Windows')) {
    throw 'Choose a dedicated application directory.'
}

$appSource = Join-Path $SourceDir "WizLight.exe"
if (-not (Test-Path -LiteralPath $appSource)) {
    throw "WizLight.exe was not found next to the installer script."
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -LiteralPath $appSource -Destination (Join-Path $InstallDir "WizLight.exe") -Force
$uninstallCmdSource = Join-Path $SourceDir "uninstall.cmd"
if (Test-Path -LiteralPath $uninstallCmdSource -PathType Leaf) {
    Copy-Item -LiteralPath $uninstallCmdSource -Destination (Join-Path $InstallDir "uninstall.cmd") -Force
}

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
param(
    [string]$StartMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\WizLight",
    [string]$DesktopDir = [Environment]::GetFolderPath("Desktop")
)
$ErrorActionPreference = "Stop"
$target = [System.IO.Path]::GetFullPath($PSScriptRoot)
$exe = Join-Path $target 'WizLight.exe'
if ($target -eq [System.IO.Path]::GetPathRoot($target) -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw 'Refusing to remove an invalid application directory.'
}
$shell = New-Object -ComObject WScript.Shell
foreach ($shortcut in @((Join-Path $StartMenuDir 'WizLight.lnk'), (Join-Path $DesktopDir 'WizLight.lnk'))) {
    if (Test-Path -LiteralPath $shortcut -PathType Leaf) {
        $linkedTarget = $shell.CreateShortcut($shortcut).TargetPath
        if ($linkedTarget -and [System.IO.Path]::GetFullPath($linkedTarget) -eq $exe) {
            Remove-Item -LiteralPath $shortcut -Force
        }
    }
}
foreach ($name in @('WizLight.exe', 'uninstall.cmd', 'uninstall.ps1')) {
    $file = Join-Path $target $name
    if (Test-Path -LiteralPath $file -PathType Leaf) { Remove-Item -LiteralPath $file -Force }
}
if ((Test-Path -LiteralPath $StartMenuDir -PathType Container) -and
    @(Get-ChildItem -LiteralPath $StartMenuDir -Force).Count -eq 0) {
    Remove-Item -LiteralPath $StartMenuDir -Force
}
if (@(Get-ChildItem -LiteralPath $target -Force).Count -eq 0) {
    Remove-Item -LiteralPath $target -Force
}
'@ | Set-Content -LiteralPath $uninstallScript -Encoding UTF8

Write-Host "WizLight installed to $InstallDir"
Start-Process -FilePath (Join-Path $InstallDir "WizLight.exe")
