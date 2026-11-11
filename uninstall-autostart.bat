@echo off
title Edge Bridge - remove autostart

echo Removing the EdgeBridge autostart (all methods)...

rem A. Task Scheduler task
schtasks /Delete /TN "EdgeBridge" /F >nul 2>nul

rem B. HKCU Run registry key
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v EdgeBridge /f >nul 2>nul

rem C. Startup folder copy
del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\bridge-autostart.vbs" >nul 2>nul

echo.
echo Autostart removed. The background bridge keeps running
echo until you log off or restart; after that it will
echo never start again on its own.
echo.
pause
