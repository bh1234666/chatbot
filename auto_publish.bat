@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

title Auto Publish Public Snapshot

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

if /I "%~1"=="--check" (
    echo auto_publish.bat OK
    exit /b 0
)

set "PY=%ROOT%\.venv\Scripts\python.exe"
set "EXPORT=%ROOT%\scripts\export_public_snapshot.py"
set "OUT=%ROOT%\..\chatbot-public"

REM -- locate git (PATH first, then default install dirs) --
set "GIT="
for /f "delims=" %%G in ('where git 2^>nul') do (
    if not defined GIT set "GIT=%%G"
)
if not defined GIT if exist "C:\Program Files\Git\cmd\git.exe" set "GIT=C:\Program Files\Git\cmd\git.exe"
if not defined GIT if exist "C:\Program Files (x86)\Git\cmd\git.exe" set "GIT=C:\Program Files (x86)\Git\cmd\git.exe"
if not defined GIT (
    echo [ERROR] git.exe not found on PATH or default install dir.
    echo Install Git for Windows or add it to PATH, then re-run.
    pause
    exit /b 1
)

if not exist "%PY%" (
    echo [ERROR] venv Python not found at "%PY%"
    pause
    exit /b 1
)
if not exist "%EXPORT%" (
    echo [ERROR] export script missing: %EXPORT%
    pause
    exit /b 1
)

echo.
echo ==============================================
echo   Auto Publish: Source -^> Snapshot -^> Push
echo ==============================================
echo   Source : %ROOT%
echo   Target : %OUT%
echo   Git    : %GIT%
echo.

REM -- 1. export sanitized snapshot (preserves .git/) --
echo [1/5] Exporting sanitized snapshot ...
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%PY%" "%EXPORT%" --out "%OUT%" --force
if errorlevel 1 (
    echo.
    echo [FAIL] Export aborted. Fix the source and re-run.
    pause
    exit /b 1
)

REM -- 2. ensure target is a git repo --
if not exist "%OUT%\.git" (
    echo.
    echo [INFO] %OUT% is not a git repo yet. First-time setup:
    echo   cd /d "%OUT%"
    echo   "%GIT%" init -b main
    echo   "%GIT%" config user.name "bh1234666"
    echo   "%GIT%" config user.email "1137154011@qq.com"
    echo   "%GIT%" remote add origin https://github.com/bh1234666/chatbot.git
    echo   "%GIT%" add -A ^&^& "%GIT%" commit -m "Initial public snapshot"
    echo   "%GIT%" push -u origin main
    pause
    exit /b 1
)

pushd "%OUT%" >nul

REM -- 3. stage + show diff summary --
echo.
echo [2/5] Staging changes ...
"%GIT%" add -A
echo.
echo [3/5] Diff summary:
"%GIT%" status --short
"%GIT%" diff --cached --shortstat

REM -- 4. second-line secret scan on staged content --
echo.
echo [4/5] Secret scan on staged content ...
set "SCAN_HIT="
for %%P in ("sk-[A-Za-z0-9]\{32,\}" "chat\.ekti\.cc" "DEEPSEEK_API_KEY=sk-" "GPT55_API_KEY=sk-") do (
    "%GIT%" diff --cached -G %%P --name-only > "%TEMP%\auto_publish_hit.txt" 2>nul
    for /f "usebackq delims=" %%F in ("%TEMP%\auto_publish_hit.txt") do (
        if /I not "%%F"=="scripts/export_public_snapshot.py" (
            if /I not "%%F"=="auto_publish.bat" (
                echo   [HIT] pattern=%%P  file=%%F
                set "SCAN_HIT=1"
            )
        )
    )
)
del /q "%TEMP%\auto_publish_hit.txt" >nul 2>&1
if defined SCAN_HIT (
    echo.
    echo [FAIL] Suspicious content in staged diff. Aborting.
    popd >nul
    pause
    exit /b 2
)
echo   clean.

REM -- 5. anything to commit? --
"%GIT%" diff --cached --quiet
if not errorlevel 1 (
    echo.
    echo [INFO] No staged changes. Nothing to commit.
    popd >nul
    pause
    exit /b 0
)

echo.
set /p "MSG=[5/5] Commit message (blank = 'Update snapshot YYYY-MM-DD'): "
if "!MSG!"=="" (
    for /f "tokens=1-3 delims=/-. " %%a in ("%date%") do set "TODAY=%%c-%%a-%%b"
    set "MSG=Update snapshot !TODAY!"
)

echo.
echo Commit: !MSG!
choice /C YN /N /M "Confirm commit + push? [Y/N] "
if errorlevel 2 (
    echo Cancelled. Staged changes remain in %OUT%.
    popd >nul
    exit /b 0
)

"%GIT%" commit -m "!MSG!"
if errorlevel 1 (
    echo [FAIL] commit failed.
    popd >nul
    pause
    exit /b 3
)

echo.
echo Pushing ...
"%GIT%" push
set "PUSH_RC=!ERRORLEVEL!"
popd >nul

echo.
if "!PUSH_RC!"=="0" (
    echo [OK] Published.
    echo   https://github.com/bh1234666/chatbot
) else (
    echo [WARN] commit recorded locally but push failed (rc=!PUSH_RC!).
    echo Check network / proxy, then run:
    echo   cd /d "%OUT%" ^&^& "%GIT%" push
)
echo.
pause
exit /b !PUSH_RC!
