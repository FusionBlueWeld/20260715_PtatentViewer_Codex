@echo off
setlocal
set "PROJECT_ROOT=%~dp0.."
cd /d "%PROJECT_ROOT%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\start.ps1" %*
if errorlevel 1 (
  echo.
  echo PatentViewer failed to start. See runtime\server.stderr.log
  pause
)
endlocal
