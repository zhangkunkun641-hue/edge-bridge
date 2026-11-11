@echo off
cd /d "%~dp0"
title Edge Bridge (small profile)

echo ============================================
echo   Edge Bridge - small profile mode
echo   (separate clean Edge, not your main one)
echo ============================================
echo.

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo [ERROR] Python not found.
  pause
  exit /b 1
)

echo Close the OTHER Edge windows first if any are open.
echo Press any key to start the small-profile Edge and the bridge...
pause >nul

call start-edge.bat
timeout /t 5 /nobreak >nul
%PY% bridge.py
pause
