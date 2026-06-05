@echo off
chcp 65001 >nul
title Chatbot QQ Bot - One Click Start
color 0A

if /i "%~1"=="--check" (
    echo startbot.bat OK
    exit /b 0
)

echo.
echo   ==============================================
echo       Chatbot QQ Bot One-Click Launcher
echo   ==============================================
echo.

REM [1/5] Check Python
echo  [1/5] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Python not found. Please install Python 3.11+
    pause
    exit /b 1
)
echo          Python is available [OK]

REM [2/5] Config
echo  [2/5] Checking .env...
if not exist .env (
    copy .env.example .env >nul 2>&1
    echo         .env created from template [OK]
) else (
    echo         .env exists [OK]
)

REM [3/5] Venv + deps
echo  [3/5] Setting up virtual environment...
if not exist .venv\Scripts\python.exe (
    echo         Creating venv...
    python -m venv .venv
)
.venv\Scripts\python.exe -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo   [ERROR] Failed to install Python dependencies.
    pause
    exit /b 1
)
echo          Environment ready [OK]

REM [4/5] Start services
echo  [4/5] Starting backend services...
echo.
echo   Database: SQLite (chatbot.db) - zero config
echo.

start "Chatbot API" cmd /k "chcp 65001 >nul && cd /d %~dp0 && title Chatbot API && .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
echo          Chatbot API launched on port 8000 [OK]

timeout /t 2 /nobreak >nul

start "NapCat Bridge" cmd /k "chcp 65001 >nul && cd /d %~dp0 && title NapCat Bridge && .venv\Scripts\python.exe napcat_bridge.py"
echo          NapCat Bridge launched on port 8090 [OK]

REM [5/5] Start NapCat QQ
echo  [5/5] Starting NapCat QQ...

REM QQ account for the bot. Override via environment variable if needed.
if not defined QQ_BOT_NUM set "QQ_BOT_NUM=1042414563"

set "NAPCAT_BAT="
for /d %%D in ("%~dp0napcat\NapCat.*.Shell") do (
    if exist "%%D\napcat.bat" set "NAPCAT_BAT=%%D\napcat.bat"
)
if not defined NAPCAT_BAT (
    echo   [ERROR] napcat\NapCat.*.Shell\napcat.bat not found
    echo          Run NapCatInstaller.exe or unpack NapCat.Shell.zip first.
    pause
    exit /b 1
)
for %%D in ("%NAPCAT_BAT%") do set "NAPCAT_DIR=%%~dpD"
if "%NAPCAT_DIR:~-1%"=="\" set "NAPCAT_DIR=%NAPCAT_DIR:~0,-1%"

REM Always launch via napcat.bat — it sets NAPCAT_DISABLE_MULTI_PROCESS=1
REM (the proven workaround for Worker crash on this box).
start "NapCat QQ" cmd /k "chcp 65001 >nul && cd /d %NAPCAT_DIR% && set QQ_BOT_NUM=%QQ_BOT_NUM% && napcat.bat"
echo          NapCat QQ launched [OK] account=%QQ_BOT_NUM% (via napcat.bat)

echo.
echo   ==============================================
echo           All services started
echo.
echo       Chatbot API : http://localhost:8000/docs
echo       Bridge      : http://localhost:8090/health
echo       Bot API     : http://localhost:8000/docs#/bot
echo       Database    : chatbot.db (SQLite)
echo.
echo   ==============================================
echo     Quick Commands (type in this window):
echo.
echo       quick GROUP_ID         One-step: create + join
echo       join  GROUP_ID [AID]   Join a QQ group
echo       leave GROUP_ID         Leave a QQ group
echo       list                   List all joined groups
echo       info  GROUP_ID         Show group detail
echo       sw    GROUP_ID AID     Switch active persona
echo       recent GROUP_ID [N]    Show recent conversations
echo       admin GROUP_ID         Set admin group for QQ botctl
echo       del   GROUP_ID         Delete warm memories (interactive)
echo       help                   Show full command list
echo       quit                  Stop all services and exit
echo.
echo     For persona version management, open a new
echo     terminal and use botctl.bat:
echo       botctl create NAME [GID]   New persona archive
echo       botctl list GROUP_ID       List personas w/ summaries
echo       botctl switch GROUP_ID     Interactive persona switch
echo.
echo   Bot is SILENT until you join a group.
echo   ==============================================

:waitloop
set API=http://localhost:8000/v1
set PY=.venv\Scripts\python.exe botctl_helper.py
set CMD=
set /p CMD="> "
if "%CMD%"=="" goto waitloop

REM Parse: first word = action, rest = args
for /f "tokens=1,* delims= " %%a in ("%CMD%") do (
    set ACT=%%a
    set ARGS=%%b
)

if /i "%ACT%"=="Q" goto quit
if /i "%ACT%"=="quit" goto quit
if /i "%ACT%"=="exit" goto quit

if /i "%ACT%"=="quick" (
    %PY% quick %ARGS%
    goto waitloop
)
if /i "%ACT%"=="join" (
    %PY% join %ARGS%
    goto waitloop
)
if /i "%ACT%"=="leave" (
    %PY% leave %ARGS%
    goto waitloop
)
if /i "%ACT%"=="list" (
    %PY% list-groups
    goto waitloop
)
if /i "%ACT%"=="info" (
    curl -s "%API%/bot/groups/%ARGS%" | .venv\Scripts\python.exe -m json.tool 2>nul
    if errorlevel 1 curl -s "%API%/bot/groups/%ARGS%"
    echo.
    goto waitloop
)
if /i "%ACT%"=="sw" (
    %PY% switch %ARGS%
    goto waitloop
)
if /i "%ACT%"=="recent" (
    %PY% recent %ARGS%
    goto waitloop
)
if /i "%ACT%"=="admin" (
    %PY% admin %ARGS%
    goto waitloop
)
if /i "%ACT%"=="del" (
    %PY% del %ARGS%
    goto waitloop
)
if /i "%ACT%"=="stop" (
    call "%~dp0stop_all_services.bat"
    goto waitloop
)
if /i "%ACT%"=="cleanup" (
    call "%~dp0stop_all_services.bat"
    goto waitloop
)
if /i "%ACT%"=="help" (
    %PY% help
    goto waitloop
)
echo   Unknown: %CMD%  (type quit to stop all services)
goto waitloop

REM ==========================================
REM  All heavy lifting is in botctl_helper.py
REM  Subroutines removed — Python handles JSON reliably.
REM ==========================================

:quit
echo   Shutting down visible bot service windows...

REM Only close windows launched by this script. No PID files and no hidden processes.
call "%~dp0stop_all_services.bat"

echo   Goodbye.
exit /b 0
