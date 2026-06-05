@echo off
chcp 65001 >nul
title Bot Controller
set PY=.venv\Scripts\python.exe botctl_helper.py

if "%1"=="" goto menu

if /i "%1"=="create"   goto cmd_create
if /i "%1"=="list"     goto cmd_list
if /i "%1"=="switch"   goto cmd_switch
if /i "%1"=="leave"    goto cmd_leave
if /i "%1"=="info"     goto cmd_info
if /i "%1"=="recent"   goto cmd_recent
if /i "%1"=="admin"    goto cmd_admin
if /i "%1"=="del"      goto cmd_del
if /i "%1"=="help"     goto cmd_help
if /i "%1"=="quick"    (echo quick is now: botctl create ^<name^> ^[group_id^] && goto end)
goto menu

REM ==========================================
REM  botctl create NAME [GROUP_ID]
REM  Creates a new archive + persona, optionally adds to group.
REM ==========================================
:cmd_create
if "%2"=="" (echo Usage: botctl create ^<name^> [group_id] && goto end)
%PY% create %2 %3 %4
goto end

REM ==========================================
REM  botctl list  OR  botctl list GROUP_ID
REM  No args: list all groups.
REM  With GROUP_ID: list personas in that group with summaries.
REM ==========================================
:cmd_list
if "%2"=="" (
    %PY% list-groups
) else (
    %PY% list-personas %2
)
goto end

REM ==========================================
REM  botctl switch GROUP_ID [ARCHIVE_ID]
REM  With AID: direct switch.
REM  Without AID: interactive selection with summaries.
REM ==========================================
:cmd_switch
if "%2"=="" (echo Usage: botctl switch ^<group_id^> [archive_id] && goto end)
%PY% switch %2 %3
goto end

REM ==========================================
REM  botctl leave GROUP_ID
REM ==========================================
:cmd_leave
if "%2"=="" (echo Usage: botctl leave ^<group_id^> && goto end)
%PY% leave %2
goto end

REM ==========================================
REM  botctl del GROUP_ID
REM  Interactive warm memory deletion.
REM ==========================================
:cmd_del
if "%2"=="" (echo Usage: botctl del ^<group_id^> && goto end)
%PY% del %2
goto end

REM ==========================================
REM  botctl info GROUP_ID
REM  Quick raw JSON dump of group config.
REM ==========================================
:cmd_info
if "%2"=="" (echo Usage: botctl info ^<group_id^> && goto end)
curl -s "http://localhost:8000/v1/bot/groups/%2" | .venv\Scripts\python.exe -m json.tool 2>nul
if errorlevel 1 curl -s "http://localhost:8000/v1/bot/groups/%2"
echo.
goto end

REM ==========================================
REM  botctl recent GROUP_ID [N]
REM  Show recent conversation events in a group.
REM ==========================================
:cmd_recent
if "%2"=="" (echo Usage: botctl recent ^<group_id^> [count] && goto end)
%PY% recent %2 %3
goto end

REM ==========================================
REM  botctl admin GROUP_ID
REM  Set the admin group for in-QQ botctl commands.
REM ==========================================
:cmd_admin
if "%2"=="" (echo Usage: botctl admin ^<group_id^> && goto end)
%PY% admin %2
goto end

REM ==========================================
REM  botctl help
REM  Show all available commands.
REM ==========================================
:cmd_help
%PY% help
goto end

REM ==========================================
REM  Interactive menu
REM ==========================================
:menu
echo.
echo   ==============================================
echo       Bot Controller — Persona Version Manager
echo   ==============================================
echo.
echo   Commands:
echo     botctl create  NAME [GROUP_ID]   Create new persona archive
echo     botctl list                      List all groups
echo     botctl list    GROUP_ID          List personas in group (with summaries)
echo     botctl switch  GROUP_ID [AID]    Switch active persona (interactive w/o AID)
echo     botctl recent  GROUP_ID [N]      Show recent conversations in a group
echo     botctl leave   GROUP_ID          Leave a group
echo     botctl del     GROUP_ID          Delete warm memories (interactive)
echo     botctl admin   GROUP_ID          Set admin group for QQ botctl
echo     botctl help                      Show full command list
echo     botctl info    GROUP_ID          Show group config (raw JSON)
echo.
echo   Examples:
echo     botctl create  "My Bot v2" 123456789
echo     botctl admin   123456789
echo     botctl list    123456789
echo     botctl switch  123456789
echo     botctl recent  123456789
echo.
set /p CMD="botctl^> "
if "%CMD%"=="" goto end
call "%0" %CMD%
goto menu

:end
