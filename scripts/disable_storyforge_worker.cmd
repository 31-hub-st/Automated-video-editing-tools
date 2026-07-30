@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0disable_storyforge_worker.ps1"
echo.
pause
