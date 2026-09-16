@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Chatbot Agent Frontend
color 0A

cd /d "%~dp0"
set "PYRUN=scripts\repo_python.bat"

if /i "%~1"=="--check" (
    echo start_agent.bat OK
    call "%PYRUN%" -HealthCheck || goto fail
    exit /b 0
)

if not exist "agent_frontend\serve_frontend.py" (
    echo [ERROR] agent_frontend\serve_frontend.py not found.
    goto fail
)

echo.
echo Agent frontend: http://127.0.0.1:8765/
echo Backend target: 127.0.0.1:8000
echo Backend API is not started by this script. Start start_backend.bat separately if needed.
echo Press Ctrl+C to stop.
echo.
call "%PYRUN%" agent_frontend\serve_frontend.py --host 127.0.0.1 --port 8765 --backend 127.0.0.1:8000
goto end

:fail
echo.
echo [ERROR] Agent frontend startup failed.
pause

:end
endlocal
