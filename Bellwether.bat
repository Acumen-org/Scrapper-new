@echo off
rem Bellwether launcher.
rem
rem   Bellwether.bat            start it if it is not already up, then open it
rem   Bellwether.bat /silent    start it if it is not already up, no browser
rem
rem The /silent form is what runs at sign in. There is no stop script on
rem purpose: Bellwether is stopped from inside itself, via Quit in the sidebar.
cd /d "%~dp0"

rem Already serving? Then never start a second copy.
powershell -NoProfile -Command "try{$null=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8787/healthz;exit 0}catch{exit 1}" >nul 2>&1
if %errorlevel%==0 goto open

if not exist data mkdir data
rem -PassThru records the supervisor PID. The app rewrites this file at startup
rem too, so Quit always has a live PID to stop even if this file went stale.
powershell -NoProfile -Command "$p = Start-Process -WindowStyle Hidden -FilePath 'python' -ArgumentList '-m','uvicorn','prospect.webapp:app','--port','8787','--workers','2','--log-level','warning' -WorkingDirectory '%~dp0' -RedirectStandardOutput 'data\server.log' -RedirectStandardError 'data\server.err.log' -PassThru; Set-Content -Path 'data\server.pid' -Value $p.Id"

rem Wait until it actually answers, rather than guessing a number of seconds.
rem This replaces a fixed timeout, which also failed whenever stdin was
rem redirected, so launching from a script did nothing at all.
powershell -NoProfile -Command "foreach($i in 1..25){try{$null=Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 http://127.0.0.1:8787/healthz;exit 0}catch{Start-Sleep -Milliseconds 400}};exit 1" >nul 2>&1

:open
if /i "%~1"=="/silent" exit /b
start "" "http://127.0.0.1:8787/"
exit /b
