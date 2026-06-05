@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
title Switch Chatbot Model Pool

cd /d "%~dp0"

set "POOL_DIR=app\llm\model_pool_variants"
set "ACTIVE_POOL=app\llm\model_pool.py"
set "POOL_DS=%POOL_DIR%\model_pool_deepseek.py"
set "POOL_GPT=%POOL_DIR%\model_pool_all_gpt.py"
set "POOL_MIXED=%POOL_DIR%\model_pool_mixed.py"

if /i "%~1"=="--check" (
    call :check_files || exit /b 1
    echo switch_model_pool.bat OK
    exit /b 0
)

call :check_files || goto fail

set "SELECT=%~1"
if "%SELECT%"=="" (
    echo.
    echo Select model pool:
    echo   1. DeepSeek only
    echo   2. GPT-5.5 only
    echo   3. Mixed ^(Round1/Round3/lowest Round2 DeepSeek, others GPT-5.5^)
    echo.
    choice /c 123 /n /m "Input 1/2/3: "
    set "SELECT=!ERRORLEVEL!"
)

if "%SELECT%"=="1" (
    copy /y "%POOL_DS%" "%ACTIVE_POOL%" >nul || goto fail
    echo Switched model pool to: DeepSeek only
    goto end
)

if "%SELECT%"=="2" (
    copy /y "%POOL_GPT%" "%ACTIVE_POOL%" >nul || goto fail
    echo Switched model pool to: GPT-5.5 only
    goto end
)

if "%SELECT%"=="3" (
    copy /y "%POOL_MIXED%" "%ACTIVE_POOL%" >nul || goto fail
    echo Switched model pool to: Mixed
    goto end
)

echo [ERROR] Invalid selection: %SELECT%
echo Use 1, 2, or 3.
goto fail

:check_files
if not exist "%POOL_DS%" (
    echo [ERROR] Missing %POOL_DS%
    exit /b 1
)
if not exist "%POOL_GPT%" (
    echo [ERROR] Missing %POOL_GPT%
    exit /b 1
)
if not exist "%POOL_MIXED%" (
    echo [ERROR] Missing %POOL_MIXED%
    exit /b 1
)
exit /b 0

:fail
echo.
echo [ERROR] Model pool switch failed.
pause
exit /b 1

:end
echo Active file: %ACTIVE_POOL%
endlocal
