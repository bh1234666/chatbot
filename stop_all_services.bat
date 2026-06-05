@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Chatbot - Stop All Services

set "ROOT=%~dp0"
set "PS1=%ROOT%stop_all_services.ps1"
set "PSARGS="

:parse_args
if "%~1"=="" goto run_ps
if /i "%~1"=="--no-pause" (
    set "PSARGS=%PSARGS% -NoPause"
) else if /i "%~1"=="--check" (
    set "PSARGS=%PSARGS% -Check -NoPause"
) else if /i "%~1"=="-Check" (
    set "PSARGS=%PSARGS% -Check -NoPause"
) else if /i "%~1"=="-NoPause" (
    set "PSARGS=%PSARGS% -NoPause"
) else if /i "%~1"=="--dryrun" (
    set "PSARGS=%PSARGS% -DryRun"
) else if /i "%~1"=="--dry-run" (
    set "PSARGS=%PSARGS% -DryRun"
) else if /i "%~1"=="-DryRun" (
    set "PSARGS=%PSARGS% -DryRun"
) else (
    set "PSARGS=%PSARGS% %~1"
)
shift
goto parse_args

if not exist "%PS1%" (
    echo [ERROR] stop_all_services.ps1 not found.
    pause
    exit /b 1
)

:run_ps
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %PSARGS%
set "RC=%ERRORLEVEL%"

echo %PSARGS% | findstr /i /c:"-NoPause" >nul
if errorlevel 1 (
    echo.
    pause
)

exit /b %RC%
