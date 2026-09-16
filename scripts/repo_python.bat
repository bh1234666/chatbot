@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0repo_python.ps1" %*
set "EXITCODE=%ERRORLEVEL%"
endlocal & exit /b %EXITCODE%
