param(
    [string]$SourceDir = $PSScriptRoot,
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\WizLight",
    [string]$StartMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\WizLight",
    [string]$DesktopDir = [Environment]::GetFolderPath("Desktop"),
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
if ($InstallDir -eq [System.IO.Path]::GetPathRoot($InstallDir) -or
    $InstallDir -eq [Environment]::GetFolderPath('UserProfile') -or
    $InstallDir -eq [Environment]::GetFolderPath('Desktop') -or
    $InstallDir -eq [Environment]::GetFolderPath('Windows')) {
    throw 'Choose a dedicated application directory.'
}

$payloadZip = Join-Path $SourceDir "WizLight-app.zip"
if (-not (Test-Path -LiteralPath $payloadZip -PathType Leaf)) {
    throw "WizLight-app.zip was not found next to the installer script."
}

$installedExe = Join-Path $InstallDir "WizLight.exe"
$running = @(Get-Process -Name WizLight -ErrorAction SilentlyContinue | Where-Object {
    try { $_.Path -and ([System.IO.Path]::GetFullPath($_.Path) -eq $installedExe) } catch { $false }
})
if ($running.Count -gt 0) {
    throw "WizLight is running. Close it (also from the system tray) and run the installer again."
}

# Extract to a scratch folder first so a failed or corrupt extraction leaves any existing install untouched.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ("WizLight-setup-" + [guid]::NewGuid().ToString("N"))
try {
    [System.IO.Compression.ZipFile]::ExtractToDirectory($payloadZip, $scratch)
    $payloadRoot = Join-Path $scratch "WizLight"
    if (-not (Test-Path -LiteralPath (Join-Path $payloadRoot "WizLight.exe") -PathType Leaf)) {
        throw "The installer package is missing WizLight.exe."
    }

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    # Replace our own files cleanly (also removes stale files from older builds, including the old
    # single-file layout); anything else the user keeps in this folder is left alone.
    foreach ($name in @('WizLight.exe', '_internal', 'README.md', 'LICENSE', 'uninstall.cmd', 'uninstall.ps1')) {
        $old = Join-Path $InstallDir $name
        if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
    }
    Copy-Item -Path (Join-Path $payloadRoot "*") -Destination $InstallDir -Recurse -Force
}
finally {
    if (Test-Path -LiteralPath $scratch) { Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue }
}

$uninstallCmdSource = Join-Path $SourceDir "uninstall.cmd"
if (Test-Path -LiteralPath $uninstallCmdSource -PathType Leaf) {
    Copy-Item -LiteralPath $uninstallCmdSource -Destination (Join-Path $InstallDir "uninstall.cmd") -Force
}

$readme = Join-Path $SourceDir "README.md"
if ((Test-Path -LiteralPath $readme) -and -not (Test-Path -LiteralPath (Join-Path $InstallDir "README.md"))) {
    Copy-Item -LiteralPath $readme -Destination (Join-Path $InstallDir "README.md") -Force
}

$license = Join-Path $SourceDir "LICENSE"
if ((Test-Path -LiteralPath $license) -and -not (Test-Path -LiteralPath (Join-Path $InstallDir "LICENSE"))) {
    Copy-Item -LiteralPath $license -Destination (Join-Path $InstallDir "LICENSE") -Force
}

$shell = New-Object -ComObject WScript.Shell
New-Item -ItemType Directory -Force -Path $StartMenuDir | Out-Null

$appShortcut = $shell.CreateShortcut((Join-Path $StartMenuDir "WizLight.lnk"))
$appShortcut.TargetPath = $installedExe
$appShortcut.WorkingDirectory = $InstallDir
$appShortcut.Description = "WizLight desktop controller"
$appShortcut.Save()

$desktopShortcut = $shell.CreateShortcut((Join-Path $DesktopDir "WizLight.lnk"))
$desktopShortcut.TargetPath = $installedExe
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
$running = @(Get-Process -Name WizLight -ErrorAction SilentlyContinue | Where-Object {
    try { $_.Path -and ([System.IO.Path]::GetFullPath($_.Path) -eq $exe) } catch { $false }
})
if ($running.Count -gt 0) {
    throw 'WizLight is running. Close it (also from the system tray) and run the uninstaller again.'
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
foreach ($name in @('WizLight.exe', '_internal', 'README.md', 'LICENSE', 'uninstall.cmd', 'uninstall.ps1')) {
    $item = Join-Path $target $name
    if (Test-Path -LiteralPath $item) { Remove-Item -LiteralPath $item -Recurse -Force }
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
if (-not $NoLaunch) {
    Start-Process -FilePath $installedExe -WorkingDirectory $InstallDir
}
