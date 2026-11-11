@echo off
cd /d "%~dp0"
title Edge Bridge Launcher

echo ============================================
echo   Edge Bridge Launcher (main browser mode)
echo ============================================
echo.

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo [ERROR] Python not found.
  echo Install Python 3 from the Microsoft Store, then run this again.
  pause
  exit /b 1
)
echo [1/3] Python ready: %PY%

%PY% -c "import websocket" >nul 2>nul
if errorlevel 1 (
  echo Installing dependency websocket-client...
  %PY% -m pip install --no-index --find-links vendor websocket-client
  if errorlevel 1 %PY% -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple websocket-client
  %PY% -c "import websocket" >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Dependency still missing. Screenshot this window and send it to the assistant.
    pause
    exit /b 1
  )
)
echo [2/3] Dependency OK.
echo.
echo Next, Edge will restart in debug mode using your MAIN profile.
echo Please CLOSE all Edge windows now, and check the system tray
echo (bottom-right) to make sure Edge is not running in background.
echo Press any key when Edge is fully closed...
pause >nul

echo [3/3] Starting Edge and the bridge...
call start-edge-main.bat
timeout /t 5 /nobreak >nul
echo.
echo ============================================
echo  The bridge is starting. A "connect address" and
echo  a "Token" will be printed below.
echo  Send those two lines to the assistant.
echo  Do NOT close this window - closing it stops the bridge.
echo ============================================
echo.
%PY% bridge.py
pause
