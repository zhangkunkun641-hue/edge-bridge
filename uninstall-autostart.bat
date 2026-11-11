@echo off
title Edge Bridge - remove autostart

echo Removing the EdgeBridge autostart task...
schtasks /Delete /TN "EdgeBridge" /F
echo.
echo Task removed. The background bridge keeps running
echo until you log off or restart; after that it will
echo never start again on its own.
echo.
pause
