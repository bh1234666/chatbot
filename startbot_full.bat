@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Chatbot QQ Bot - Full
color 0B

cd /d "%~dp0"
set "PYRUN=scripts\repo_python.bat"

if /i "%~1"=="--check" (
    echo startbot_full.bat OK
    call "%PYRUN%" -HealthCheck || goto fail
    call startbot.bat --check || goto fail
    exit /b 0
)

echo.
echo Starting QQ Bot in FULL mode...
echo   OCR              : GPT-5.6 Sol model vision
echo   AI image helper  : image2 enabled
echo   Local GPU/voice  : enabled
echo.
call startbot.bat --gpu --model-vision --image-gen --voice
goto end

:fail
echo.
echo [ERROR] QQ Bot full startup failed.
pause

:end
endlocal
