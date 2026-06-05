@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Open Chatbot Agent

if /i "%~1"=="--check" (
    echo open_agent.bat OK
    exit /b 0
)

set "AGENT_URL=http://127.0.0.1:8765/"

echo Opening %AGENT_URL%
start "" "%AGENT_URL%"

endlocal
