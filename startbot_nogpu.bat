@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Chatbot QQ Bot - No GPU
color 07

cd /d "%~dp0"
set "PYRUN=scripts\repo_python.bat"

if /i "%~1"=="--check" (
    echo startbot_nogpu.bat OK
    call "%PYRUN%" -HealthCheck || goto fail
    call startbot.bat --check || goto fail
    exit /b 0
)

echo.
echo Starting QQ Bot without GPU...
echo   Image recognition: disabled
echo   Voice/TTS       : disabled
echo   Agent GPU jobs  : disabled
echo.

call startbot.bat --no-gpu --no-model-vision --no-image-gen
goto end

:fail
echo.
echo [ERROR] QQ Bot no-GPU startup failed.
pause

:end
endlocal
