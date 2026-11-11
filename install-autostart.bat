@echo off
cd /d "%~dp0"
title Edge Bridge - install autostart

echo ============================================
echo   Edge Bridge autostart installer v5
echo.
echo   After install:
echo   - the bridge starts silently at logon
echo     (no black window)
echo   - the small Edge opens by itself whenever
echo     the assistant needs it
echo   Remove it any time with uninstall-autostart.bat
echo ============================================
echo.

rem ============================================================
rem  Find a windowless Python launcher (pythonw.exe / pyw.exe).
rem  Ask the working interpreter for its own real path first -
rem  that is the same interpreter run-small.bat already uses.
rem ============================================================
set PYW=
set ARGS=
set PYEXE=

for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
  if not defined PYEXE set "PYEXE=%%P"
)
if defined PYEXE (
  for %%F in ("%PYEXE%") do (
    if exist "%%~dpFpythonw.exe" set "PYW=%%~dpFpythonw.exe"
    if exist "%%~dpFpyw.exe" if not defined PYW (
      set "PYW=%%~dpFpyw.exe"
      set "ARGS=-3"
    )
  )
)
if not defined PYW (
  for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%P"
  )
  if defined PYEXE (
    for %%F in ("%PYEXE%") do (
      if exist "%%~dpFpythonw.exe" set "PYW=%%~dpFpythonw.exe"
    )
  )
)
if not defined PYW (
  for /f "delims=" %%P in ('where pyw 2^>nul') do (
    if not defined PYW (
      set "PYW=%%P"
      set "ARGS=-3"
    )
  )
)
if not defined PYW (
  for /f "delims=" %%P in ('where pythonw 2^>nul') do (
    if not defined PYW set "PYW=%%P"
  )
)
if not defined PYW (
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\pythonw.exe" set "PYW=%%D\pythonw.exe"
  )
)
if not defined PYW (
  for /d %%D in ("%ProgramFiles%\Python3*") do (
    if exist "%%D\pythonw.exe" set "PYW=%%D\pythonw.exe"
  )
)
if not defined PYW (
  if exist "%LOCALAPPDATA%\Programs\Python\Launcher\pyw.exe" (
    set "PYW=%LOCALAPPDATA%\Programs\Python\Launcher\pyw.exe"
    set "ARGS=-3"
  )
)
if not defined PYW (
  if exist "%SystemRoot%\pyw.exe" (
    set "PYW=%SystemRoot%\pyw.exe"
    set "ARGS=-3"
  )
)
if not defined PYW (
  if defined PYEXE (
    set "PYW=%PYEXE%"
    set "ARGS="
    echo [NOTE] pythonw.exe not found - using python.exe hidden instead.
  )
)
if not defined PYW (
  echo [ERROR] No Python interpreter found at all.
  echo         Screenshot this window and send it to the assistant.
  pause
  exit /b 1
)
echo Python (windowless): %PYW% %ARGS%

rem ============================================================
rem  Generate the supervisor script bridge-autostart.vbs.
rem  v4: the supervisor now writes heartbeat lines into
rem  bridge.log (start / exit code / error), so a silent
rem  failure leaves evidence instead of nothing.
rem ============================================================
set VBS=%~dp0bridge-autostart.vbs
> "%VBS%" echo On Error Resume Next
>> "%VBS%" echo Set shell = CreateObject("WScript.Shell")
>> "%VBS%" echo Set fso = CreateObject("Scripting.FileSystemObject")
>> "%VBS%" echo shell.CurrentDirectory = "%~dp0"
>> "%VBS%" echo shell.Environment("PROCESS")("BRIDGE_MODE") = "autostart"
>> "%VBS%" echo Sub L(m)
>> "%VBS%" echo   On Error Resume Next
>> "%VBS%" echo   Set f = fso.OpenTextFile("%~dp0bridge.log", 8, True)
>> "%VBS%" echo   f.WriteLine Now ^& " [supervisor] " ^& m
>> "%VBS%" echo   f.Close
>> "%VBS%" echo End Sub
>> "%VBS%" echo L "supervisor up, launcher=%PYW% %ARGS%"
>> "%VBS%" echo Do
>> "%VBS%" echo   L "starting bridge"
>> "%VBS%" echo   rc = shell.Run("""%PYW%"" %ARGS% ""%~dp0bridge.py""", 0, True)
>> "%VBS%" echo   L "bridge exited rc=" ^& rc ^& " err=" ^& Err.Number ^& " " ^& Err.Description
>> "%VBS%" echo   Err.Clear
>> "%VBS%" echo   WScript.Sleep 15000
>> "%VBS%" echo Loop
echo Wrote %VBS%
echo.

rem ============================================================
rem  Register autostart, three no-admin routes in order:
rem    A. Task Scheduler (ONLOGON)
rem    B. HKCU Run registry key
rem    C. Startup folder
rem ============================================================
set METHOD=

schtasks /Create /TN "EdgeBridge" /SC ONLOGON /TR "wscript.exe \"%VBS%\"" /F >nul 2>nul
if not errorlevel 1 (
  set METHOD=schtasks
  schtasks /Run /TN "EdgeBridge" >nul 2>nul
)

if not defined METHOD (
  reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v EdgeBridge /t REG_SZ /d "wscript.exe \"%VBS%\"" /f >nul 2>nul
  if not errorlevel 1 (
    set METHOD=registry
    start "" wscript.exe "%VBS%"
  )
)

if not defined METHOD (
  copy /y "%VBS%" "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\bridge-autostart.vbs" >nul 2>nul
  if not errorlevel 1 (
    set METHOD=startup-folder
    start "" wscript.exe "%VBS%"
  )
)

if not defined METHOD (
  echo [ERROR] All three autostart methods failed.
  echo         Screenshot this window and send it to the assistant.
  pause
  exit /b 1
)
echo Autostart registered (method: %METHOD%) and started.
echo.

rem ============================================================
rem  Self-check (new in v4): wait for the bridge to answer
rem  /health before declaring success. If it never answers,
rem  print the tail of bridge.log right here so one
rem  screenshot carries the crash reason.
rem ============================================================
set TSIP=
for /f "delims=" %%I in ('tailscale ip -4 2^>nul') do if not defined TSIP set "TSIP=%%I"
if not defined TSIP (
  for /f "delims=" %%I in ('"C:\Program Files\Tailscale\tailscale.exe" ip -4 2^>nul') do if not defined TSIP set "TSIP=%%I"
)

echo Waiting for the bridge to answer (up to ~60s)...
set OKAT=
for /L %%I in (1,1,20) do (
  if not defined OKAT (
    curl -s -m 3 http://127.0.0.1:8765/health 2>nul | find """ok""" >nul 2>nul && set OKAT=127.0.0.1
    if not defined OKAT if defined TSIP (
      curl -s -m 3 http://%TSIP%:8765/health 2>nul | find """ok""" >nul 2>nul && set OKAT=%TSIP%
    )
    if not defined OKAT timeout /t 3 /nobreak >nul
  )
)

if defined OKAT (
  echo.
  echo [DONE + VERIFIED] Autostart works - the bridge answered at %OKAT%.
  echo You can close the old black bridge window now -
  echo the background bridge has taken over.
  echo.
  pause
  exit /b 0
)

echo.
echo [NOT VERIFIED] The bridge did not answer within ~60s.
echo ----- bridge.log (last 30 lines) -----
powershell -NoProfile -Command "Get-Content '%~dp0bridge.log' -Tail 30" 2>nul
echo --------------------------------------
echo Screenshot this WHOLE window and send it to the assistant.
echo.
pause
