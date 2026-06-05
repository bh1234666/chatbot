@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Cleanup Chatbot Generated Files

cd /d "%~dp0"

if /i "%~1"=="--check" (
    echo cleanup_generated.bat OK
    exit /b 0
)

if exist "%~dp0stop_all_services.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_all_services.ps1" -NoPause
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to stop related services before cleanup.
        pause
        exit /b 1
    )
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\cleanup_generated.ps1" %*
if errorlevel 1 (
    echo.
    echo [ERROR] Cleanup failed.
    pause
    exit /b 1
)

echo.
echo Cleanup completed. Generated files were moved under .\del\cleanup_*
endlocal
