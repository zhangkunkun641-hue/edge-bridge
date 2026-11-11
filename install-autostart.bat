@echo off
cd /d "%~dp0"
title Edge Bridge - install autostart

echo ============================================
echo   Edge Bridge autostart installer
echo.
echo   After install:
echo   - the bridge starts silently at logon
echo     (no black window, Task Scheduler task)
echo   - the small Edge opens by itself whenever
echo     the assistant needs it
echo   Remove it any time with uninstall-autostart.bat
echo ============================================
echo.

rem --- find python.exe, derive pythonw.exe next to it ---
set PYW=
for /f "delims=" %%P in ('where python 2^>nul') do (
  if not defined PYW (
    set "PYEXE=%%P"
    for %%F in ("%%P") do set "PYDIR=%%~dpF"
    if exist "%%~dpFpythonw.exe" set "PYW=%%~dpFpythonw.exe"
  )
)
if not defined PYW (
  where pyw >nul 2>nul && set PYW=pyw
)
if not defined PYW (
  echo [ERROR] Could not find pythonw.exe / pyw.
  echo         Install Python first, then run this again.
  pause
  exit /b 1
)
echo Python (windowless): %PYW%

rem --- generate the supervisor script bridge-autostart.vbs ---
set VBS=%~dp0bridge-autostart.vbs
> "%VBS%" echo Set shell = CreateObject("WScript.Shell")
>> "%VBS%" echo shell.CurrentDirectory = "%~dp0"
>> "%VBS%" echo shell.Environment("PROCESS")("BRIDGE_MODE") = "autostart"
>> "%VBS%" echo Do
>> "%VBS%" echo   shell.Run """%PYW%"" ""%~dp0bridge.py""", 0, True
>> "%VBS%" echo   WScript.Sleep 60000
>> "%VBS%" echo Loop
echo Wrote %VBS%

rem --- register the logon task ---
schtasks /Create /TN "EdgeBridge" /SC ONLOGON /TR "wscript.exe \"%VBS%\"" /F
if errorlevel 1 (
  echo [ERROR] schtasks failed to create the task.
  pause
  exit /b 1
)

rem --- start it right now ---
schtasks /Run /TN "EdgeBridge" >nul 2>nul

echo.
echo [DONE] Autostart installed and started.
echo You can close the old black bridge window now -
echo the background bridge takes over within a minute.
echo.
pause
