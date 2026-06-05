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

if not exist "napcat\NapCat.44498.Shell\napcat.bat" (
    echo [ERROR] napcat\NapCat.44498.Shell\napcat.bat not found.
    goto fail
)

echo.
echo Starting NapCat Bridge on port 8090...
start "NapCat Bridge" /D "%~dp0" cmd /k "title NapCat Bridge && .venv\Scripts\python.exe napcat_bridge.py"

timeout /t 1 /nobreak >nul

echo Starting NapCat QQ...
start "NapCat QQ" /D "%~dp0napcat\NapCat.44498.Shell" cmd /k "title NapCat QQ && napcat.bat"

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
