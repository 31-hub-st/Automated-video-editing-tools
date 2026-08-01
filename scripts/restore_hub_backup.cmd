@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restore_hub_backup.ps1"
if errorlevel 1 pause
endlocal
