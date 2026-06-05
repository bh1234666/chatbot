@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Chatbot QQ Bot
color 0B

cd /d "%~dp0"

if /i "%~1"=="--check" (
    echo start_qqbot.bat OK
    exit /b 0
)

REM QQ account for the bot. Override via env or by passing as the first arg.
if not defined QQ_BOT_NUM set "QQ_BOT_NUM=1042414563"
if not "%~1"=="" set "QQ_BOT_NUM=%~1"

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || goto fail
)

echo Installing QQ bot dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt --disable-pip-version-check || goto fail

if not exist "napcat_bridge.py" (
    echo [ERROR] napcat_bridge.py not found.
    goto fail
)

REM Auto-detect NapCat shell directory (any version), e.g. napcat\NapCat.44498.Shell
REM We always launch via napcat.bat because it sets NAPCAT_DISABLE_MULTI_PROCESS=1
REM (the proven workaround for Worker crash on this box).
set "NAPCAT_BAT="
for /d %%D in ("napcat\NapCat.*.Shell") do (
    if exist "%%D\napcat.bat" set "NAPCAT_BAT=%%D\napcat.bat"
)
if not defined NAPCAT_BAT (
    echo [ERROR] napcat\NapCat.*.Shell\napcat.bat not found.
    echo Run NapCatInstaller.exe or unpack NapCat.Shell.zip into the napcat\ folder first.
    goto fail
)
for %%D in ("%NAPCAT_BAT%") do set "NAPCAT_DIR=%%~dpD"
if "%NAPCAT_DIR:~-1%"=="\" set "NAPCAT_DIR=%NAPCAT_DIR:~0,-1%"

echo.
echo Starting NapCat Bridge on port 8090...
start "NapCat Bridge" /D "%~dp0" cmd /k "chcp 65001 >nul && title NapCat Bridge && .venv\Scripts\python.exe napcat_bridge.py"

timeout /t 1 /nobreak >nul

echo Starting NapCat QQ from "%NAPCAT_DIR%" (account=%QQ_BOT_NUM%) ...
start "NapCat QQ" /D "%NAPCAT_DIR%" cmd /k "chcp 65001 >nul && set QQ_BOT_NUM=%QQ_BOT_NUM% && napcat.bat"

echo.
echo QQ bot processes were started in separate windows.
echo Backend API is not started by this script. Start start_backend.bat separately if needed.
echo.
pause
goto end

:fail
echo.
echo [ERROR] QQ bot startup failed.
pause

:end
endlocal
