@echo off
setlocal
set "PROJECT_ROOT=%~dp0.."
cd /d "%PROJECT_ROOT%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\scripts\stop.ps1"
if errorlevel 1 (
  echo.
  echo PatentViewer failed to stop.
  pause
)
endlocal
