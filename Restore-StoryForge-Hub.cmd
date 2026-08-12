@echo off
setlocal
set "RECOVERY_SCRIPT=%~dp0scripts\restore_storyforge_hub_new_machine.ps1"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%RECOVERY_SCRIPT%" %*
set "RECOVERY_EXIT=%ERRORLEVEL%"
endlocal & exit /b %RECOVERY_EXIT%
