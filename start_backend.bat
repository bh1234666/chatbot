@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Chatbot Backend API
color 0E

cd /d "%~dp0"

if /i "%~1"=="--check" (
    echo start_backend.bat OK
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

echo Installing backend dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt --disable-pip-version-check || goto fail

echo.
echo Backend API: http://localhost:8000/docs
echo Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
goto end

:fail
echo.
echo [ERROR] Backend startup failed.
pause

:end
endlocal
