@echo off
chcp 65001 >nul
setlocal

title Export Public Snapshot

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "PY=%ROOT%\.venv\Scripts\python.exe"
set "SCRIPT=%ROOT%\scripts\export_public_snapshot.py"
set "OUT=%ROOT%\..\chatbot-public"

if not exist "%PY%" (
    echo [ERROR] venv Python not found at "%PY%"
    echo Run start.bat once to create the venv, then re-run this script.
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo [ERROR] export script not found at "%SCRIPT%"
    pause
    exit /b 1
)

set "MODE=%~1"
if /I "%MODE%"=="--check" (
    echo export_public_snapshot.bat OK
    exit /b 0
)
if /I "%MODE%"=="dry" goto :dry
if /I "%MODE%"=="--dry-run" goto :dry
if /I "%MODE%"=="check" goto :dry

echo.
echo ==============================================
echo   Export Public Snapshot
echo ==============================================
echo   Source : %ROOT%
echo   Target : %OUT%
echo.
echo This will WIPE the target directory and re-export
echo a sanitized snapshot for open-sourcing.
echo.
choice /C YN /N /M "Proceed? [Y/N] "
if errorlevel 2 (
    echo Cancelled.
    exit /b 0
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%PY%" "%SCRIPT%" --out "%OUT%" --force
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [OK] Snapshot ready at %OUT%
    echo.
    echo Next steps (manual):
    echo   cd /d "%OUT%"
    echo   git init
    echo   git add -A
    echo   git commit -m "Initial public snapshot"
    echo   git remote add origin ^<your-github-url^>
    echo   git branch -M main
    echo   git push -u origin main
) else (
    echo [FAIL] Export aborted with exit code %RC%.
    echo Review the messages above, fix the source, then re-run.
)
echo.
pause
exit /b %RC%

:dry
echo.
echo ==============================================
echo   Export Public Snapshot ^(dry-run^)
echo ==============================================
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%PY%" "%SCRIPT%" --out "%OUT%" --dry-run
echo.
pause
exit /b %ERRORLEVEL%
