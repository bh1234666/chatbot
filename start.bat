@echo off
title Chatbot - One Click Start
color 0E

echo.
echo   ==============================================
echo          Chatbot One-Click Launcher
echo          Author: bh1234666
echo   ==============================================
echo.

REM [1/4] Check Python
echo  [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Python not found. Please install Python 3.11+
    pause
    exit /b 1
)
echo          Python is available [OK]

REM [2/4] Config
echo  [2/4] Checking .env...
if not exist .env (
    copy .env.example .env >nul 2>&1
    echo         .env created from template [OK]
) else (
    echo         .env exists [OK]
)

REM [3/4] Venv + deps
echo  [3/4] Setting up virtual environment...
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

REM [4/4] Start services
echo  [4/4] Starting services...
echo.
echo   Database: SQLite (chatbot.db) - zero config
echo.

start "Chatbot API" cmd /c "cd /d %~dp0 && title Chatbot API && .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
echo          Chatbot API launched on port 8000 [OK]

start "NapCat Bridge" cmd /c "cd /d %~dp0 && title NapCat Bridge && .venv\Scripts\python.exe napcat_bridge.py"
echo          NapCat Bridge launched on port 8090 [OK]

echo.
echo   ==============================================
echo           All services started
echo.
echo       Chatbot API : http://localhost:8000/docs
echo       Bridge      : http://localhost:8090/health
echo       Database    : chatbot.db (SQLite)
echo.
echo   Next: Configure NapCat WebUI callback URL
echo       http://localhost:6099/webui
echo       Set HTTP callback to:
echo       http://localhost:8090/napcat/callback
echo.
echo   Then start NapCat QQ separately:
echo       cd napcat\NapCat.44498.Shell
echo       napcat.bat
echo   ==============================================
echo.

pause
