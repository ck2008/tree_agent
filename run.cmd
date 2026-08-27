@echo off
rem One-click local shared-workspace mode.
rem The SQLite service stays on this PC (loopback only); the desktop app then
rem connects to it over HTTP.  A reverse proxy is required for remote access.
setlocal
cd /d "%~dp0"

set "TREE_AGENT_URL=http://127.0.0.1:8765"
rem Keep the local data path free of spaces so cmd.exe, PowerShell and Python
rem receive the same database argument on every supported Windows install.
set "TREE_AGENT_DATA=%LOCALAPPDATA%\TreeAgent"
set "TREE_AGENT_DB=%TREE_AGENT_DATA%\tree-agent.db"
set "TREE_AGENT_LOG=%TREE_AGENT_DATA%\service.stdout.log"
set "TREE_AGENT_ERROR_LOG=%TREE_AGENT_DATA%\service.stderr.log"
set "TREE_AGENT_GUI_LOG=%TREE_AGENT_DATA%\desktop.stdout.log"
set "TREE_AGENT_GUI_ERROR_LOG=%TREE_AGENT_DATA%\desktop.stderr.log"
set "TREE_AGENT_BOOTSTRAP_TOKEN_FILE=%TREE_AGENT_DATA%\bootstrap-token.txt"

if not exist "%TREE_AGENT_DATA%" mkdir "%TREE_AGENT_DATA%"

rem Give a useful error instead of launching a hidden process that exits because
rem the server-only dependencies have not been installed yet.
python -c "import fastapi, uvicorn, argon2, cryptography" >nul 2>nul
if errorlevel 1 (
  echo.
  echo Missing local service dependencies.
  echo Run: python -m pip install -r requirements-server.txt
  echo.
  pause
  exit /b 1
)

rem Do not start a second service if one is already listening.  The sole
rem exception is an uninitialised local service whose retry token disappeared:
rem it is safe to restart because it has no accounts yet.
set "TREE_AGENT_START_SERVICE="
curl.exe --silent --fail --max-time 2 "%TREE_AGENT_URL%/api/health" >nul 2>nul
if errorlevel 1 set "TREE_AGENT_START_SERVICE=1"
if not defined TREE_AGENT_START_SERVICE (
  curl.exe --silent --fail --max-time 2 "%TREE_AGENT_URL%/api/health" | findstr /c:true >nul
  if not errorlevel 1 if not exist "%TREE_AGENT_BOOTSTRAP_TOKEN_FILE%" (
    powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen -ErrorAction Stop; Stop-Process -Id $c.OwningProcess -Force"
    set "TREE_AGENT_START_SERVICE=1"
  )
)
if defined TREE_AGENT_START_SERVICE (
  del /q "%TREE_AGENT_LOG%" "%TREE_AGENT_ERROR_LOG%" >nul 2>nul
  rem Keep the one-time token only until the first administrator is created.
  rem Retaining it in the current user's LocalAppData makes a failed GUI launch
  rem safely retryable; app.py deletes the file after a successful bootstrap.
  if not exist "%TREE_AGENT_BOOTSTRAP_TOKEN_FILE%" powershell -NoProfile -Command "[guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N') | Set-Content -NoNewline -Encoding ascii '%TREE_AGENT_BOOTSTRAP_TOKEN_FILE%'"
  set /p TREE_AGENT_BOOTSTRAP_TOKEN=<"%TREE_AGENT_BOOTSTRAP_TOKEN_FILE%"
  rem Use python.exe in a hidden process, not pythonw: stdout/stderr are kept
  rem beside the database so a startup failure is diagnosable.
  powershell -NoProfile -Command "$python = (Get-Command python -ErrorAction Stop).Source; Start-Process -WindowStyle Hidden -WorkingDirectory (Get-Location).Path -FilePath $python -ArgumentList @('-m','tree_agent.server','--db',$env:TREE_AGENT_DB,'--host','127.0.0.1','--port','8765') -RedirectStandardOutput $env:TREE_AGENT_LOG -RedirectStandardError $env:TREE_AGENT_ERROR_LOG"
  if errorlevel 1 (
    echo.
    echo Unable to launch the local Tree Agent service.
    pause
    exit /b 1
  )
)

rem An already-running but uninitialised local service still needs the token
rem for a retry of the first-admin dialog.
if not defined TREE_AGENT_BOOTSTRAP_TOKEN if exist "%TREE_AGENT_BOOTSTRAP_TOKEN_FILE%" set /p TREE_AGENT_BOOTSTRAP_TOKEN=<"%TREE_AGENT_BOOTSTRAP_TOKEN_FILE%"

rem Wait briefly for migrations and the HTTP listener before opening the GUI.
set "TREE_AGENT_READY="
for /l %%I in (1,1,45) do (
  curl.exe --silent --fail --max-time 1 "%TREE_AGENT_URL%/api/health" >nul 2>nul
  if not errorlevel 1 set "TREE_AGENT_READY=1"
  if defined TREE_AGENT_READY goto :open_gui
  rem `timeout` fails when run.cmd is launched without an interactive stdin.
  powershell -NoProfile -Command "Start-Sleep -Seconds 1"
)

echo.
echo Tree Agent local service could not start. Check port 8765 and these logs:
echo   %TREE_AGENT_LOG%
echo   %TREE_AGENT_ERROR_LOG%
if exist "%TREE_AGENT_ERROR_LOG%" type "%TREE_AGENT_ERROR_LOG%"
echo.
pause
exit /b 1

:open_gui
rem Once an administrator exists, the bootstrap token has no purpose and must
rem not remain on disk or be passed to the desktop client.
curl.exe --silent --fail --max-time 2 "%TREE_AGENT_URL%/api/health" | findstr /c:false >nul
if not errorlevel 1 (
  del /q "%TREE_AGENT_BOOTSTRAP_TOKEN_FILE%" >nul 2>nul
  set "TREE_AGENT_BOOTSTRAP_TOKEN="
)
del /q "%TREE_AGENT_GUI_LOG%" "%TREE_AGENT_GUI_ERROR_LOG%" >nul 2>nul
rem Use python.exe with redirected logs so a GUI startup exception is visible;
rem WindowStyle hides only the console, never the Tk windows.
powershell -NoProfile -Command "$python = (Get-Command python -ErrorAction Stop).Source; $arguments = @('-m','tree_agent','--server',$env:TREE_AGENT_URL); if ($env:TREE_AGENT_BOOTSTRAP_TOKEN) { $arguments += @('--bootstrap-token',$env:TREE_AGENT_BOOTSTRAP_TOKEN) }; Start-Process -WindowStyle Hidden -WorkingDirectory (Get-Location).Path -FilePath $python -ArgumentList $arguments -RedirectStandardOutput $env:TREE_AGENT_GUI_LOG -RedirectStandardError $env:TREE_AGENT_GUI_ERROR_LOG"
if errorlevel 1 (
  echo Unable to start the Tree Agent desktop program.
  echo Check: %TREE_AGENT_GUI_ERROR_LOG%
  pause
  exit /b 1
)
endlocal
