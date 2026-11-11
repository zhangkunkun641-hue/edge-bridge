@echo off
rem ============================================================
rem
rem
rem ============================================================

set EDGE="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not exist %EDGE% set EDGE="C:\Program Files\Microsoft\Edge\Application\msedge.exe"
if not exist %EDGE% set EDGE=msedge.exe

start "" %EDGE% --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir="%LOCALAPPDATA%\Microsoft\Edge\User Data"
