@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Chatbot Agent - No GPU
color 07

cd /d "%~dp0"
set "PYRUN=scripts\repo_python.bat"

if /i "%~1"=="--check" (
    echo start_no_gpu.bat OK
    call "%PYRUN%" -HealthCheck || goto fail
    exit /b 0
)

REM Disable every local GPU route for this launcher and its child processes.
set "GPU_DISABLED=true"
set "VISION_ENABLED=false"
set "VOICE_ENABLED=false"
set "MODEL_VISION_ENABLED=false"
set "IMAGE_GENERATION_ENABLED=false"
set "STARTUP_OCR_WARM_ENABLED=false"
set "CUDA_VISIBLE_DEVICES=-1"
set "NVIDIA_VISIBLE_DEVICES=none"

echo.
echo Starting Chatbot Agent without GPU...
echo   Image recognition: disabled
echo   Voice/TTS       : disabled
echo   Agent GPU jobs  : disabled
echo.

start "Chatbot Backend - No GPU" cmd /k "chcp 65001 >nul && cd /d %~dp0 && call start_backend.bat"
timeout /t 3 /nobreak >nul
start "Chatbot Agent - No GPU" cmd /k "chcp 65001 >nul && cd /d %~dp0 && call start_agent.bat"
timeout /t 2 /nobreak >nul
call open_agent.bat
goto end

:fail
echo.
echo [ERROR] No-GPU startup failed.
pause

:end
endlocal
