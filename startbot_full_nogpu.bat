@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Chatbot QQ Bot - Full No GPU
color 07

cd /d "%~dp0"
set "PYRUN=scripts\repo_python.bat"

if /i "%~1"=="--check" (
    echo startbot_full_nogpu.bat OK
    call "%PYRUN%" -HealthCheck || goto fail
    call startbot.bat --check || goto fail
    exit /b 0
)

echo.
echo Starting QQ Bot in FULL NO-GPU mode...
echo   OCR              : GPT-5.6 Sol model vision
echo   AI image helper  : image2 enabled
echo   Local GPU/voice  : disabled
echo.
call startbot.bat --no-gpu --model-vision --image-gen
goto end

:fail
echo.
echo [ERROR] QQ Bot full no-GPU startup failed.
pause

:end
endlocal
