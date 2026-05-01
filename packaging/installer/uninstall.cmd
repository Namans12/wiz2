@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\Programs\WizLight\uninstall.ps1"
exit /b %ERRORLEVEL%
