@echo off
chcp 65001 >nul
title Chatbot QQ Bot - One Click Start
color 0A
set "PYRUN=%~dp0scripts\repo_python.bat"

if /i "%~1"=="--check" (
    echo startbot.bat OK
    call "%PYRUN%" -HealthCheck || exit /b 1
    exit /b 0
)

REM Independent feature switches. User-facing wrapper BAT files only compose these flags.
:parse_start_flags
if "%~1"=="" goto start_flags_done
if /i "%~1"=="--model-vision" (
    set "MODEL_VISION_ENABLED=true"
    set "VISION_ENABLED=false"
    set "STARTUP_OCR_WARM_ENABLED=false"
    shift
    goto parse_start_flags
)
if /i "%~1"=="--local-ocr" (
    set "MODEL_VISION_ENABLED=false"
    set "VISION_ENABLED=true"
    set "STARTUP_OCR_WARM_ENABLED=true"
    shift
    goto parse_start_flags
)
if /i "%~1"=="--no-model-vision" (
    set "MODEL_VISION_ENABLED=false"
    shift
    goto parse_start_flags
)
if /i "%~1"=="--image-gen" (
    set "IMAGE_GENERATION_ENABLED=true"
    shift
    goto parse_start_flags
)
if /i "%~1"=="--no-image-gen" (
    set "IMAGE_GENERATION_ENABLED=false"
    shift
    goto parse_start_flags
)
if /i "%~1"=="--no-gpu" (
    set "GPU_DISABLED=true"
    set "VISION_ENABLED=false"
    set "VOICE_ENABLED=false"
    set "STARTUP_OCR_WARM_ENABLED=false"
    set "CUDA_VISIBLE_DEVICES=-1"
    set "NVIDIA_VISIBLE_DEVICES=none"
    shift
    goto parse_start_flags
)
if /i "%~1"=="--gpu" (
    set "GPU_DISABLED=false"
    set "CUDA_VISIBLE_DEVICES="
    set "NVIDIA_VISIBLE_DEVICES="
    shift
    goto parse_start_flags
)
if /i "%~1"=="--voice" (
    set "VOICE_ENABLED=true"
    shift
    goto parse_start_flags
)
if /i "%~1"=="--no-voice" (
    set "VOICE_ENABLED=false"
    shift
    goto parse_start_flags
)
if /i "%~1"=="--show-features" (
    set "SHOW_START_FEATURES=1"
    shift
    goto parse_start_flags
)
echo [ERROR] Unknown startup flag: %~1
exit /b 2

:start_flags_done
call "%PYRUN%" "%~dp0scripts\qq_project_mode.py" legacy
if errorlevel 1 exit /b 1
if defined SHOW_START_FEATURES (
    echo GPU_DISABLED=%GPU_DISABLED%
    echo VISION_ENABLED=%VISION_ENABLED%
    echo MODEL_VISION_ENABLED=%MODEL_VISION_ENABLED%
    echo IMAGE_GENERATION_ENABLED=%IMAGE_GENERATION_ENABLED%
    echo VOICE_ENABLED=%VOICE_ENABLED%
    echo CUDA_VISIBLE_DEVICES=%CUDA_VISIBLE_DEVICES%
    echo NVIDIA_VISIBLE_DEVICES=%NVIDIA_VISIBLE_DEVICES%
    exit /b 0
)

echo.
echo   ==============================================
echo       Chatbot QQ Bot One-Click Launcher
echo   ==============================================
echo.

REM [1/5] Check Python
echo  [1/5] Checking Python...
call "%PYRUN%" -HealthCheck >nul 2>&1
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
    call "%PYRUN%" -m venv .venv
)
call "%PYRUN%" -m pip install -r requirements.txt --disable-pip-version-check
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

start "Chatbot API" cmd /k "chcp 65001 >nul && cd /d %~dp0 && title Chatbot API && call scripts\repo_python.bat -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
echo          Chatbot API launched on port 8000 [OK]

timeout /t 2 /nobreak >nul

start "NapCat Bridge" cmd /k "chcp 65001 >nul && cd /d %~dp0 && title NapCat Bridge && call scripts\repo_python.bat napcat_bridge.py"
echo          NapCat Bridge launched on port 8090 [OK]

REM [5/5] Start NapCat QQ
echo  [5/5] Starting NapCat QQ...

REM QQ account for the bot. Override via environment variable if needed.
if not defined QQ_BOT_NUM set /p "QQ_BOT_NUM=Bot QQ account: "

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
    call "%PYRUN%" botctl_helper.py quick %ARGS%
    goto waitloop
)
if /i "%ACT%"=="join" (
    call "%PYRUN%" botctl_helper.py join %ARGS%
    goto waitloop
)
if /i "%ACT%"=="leave" (
    call "%PYRUN%" botctl_helper.py leave %ARGS%
    goto waitloop
)
if /i "%ACT%"=="list" (
    call "%PYRUN%" botctl_helper.py list-groups
    goto waitloop
)
if /i "%ACT%"=="info" (
    curl -s "%API%/bot/groups/%ARGS%" | "%PYRUN%" -m json.tool 2>nul
    if errorlevel 1 curl -s "%API%/bot/groups/%ARGS%"
    echo.
    goto waitloop
)
if /i "%ACT%"=="sw" (
    call "%PYRUN%" botctl_helper.py switch %ARGS%
    goto waitloop
)
if /i "%ACT%"=="recent" (
    call "%PYRUN%" botctl_helper.py recent %ARGS%
    goto waitloop
)
if /i "%ACT%"=="admin" (
    call "%PYRUN%" botctl_helper.py admin %ARGS%
    goto waitloop
)
if /i "%ACT%"=="del" (
    call "%PYRUN%" botctl_helper.py del %ARGS%
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
    call "%PYRUN%" botctl_helper.py help
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
