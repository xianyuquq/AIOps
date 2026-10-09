@echo off
REM ============================================================
REM  AIOps Platform - stop
REM
REM  Stops the instance listening on AIOPS_PORT (default 8848).
REM  Passes -ExecutionPolicy Bypass for this launch only, so it
REM  works even when unsigned scripts are blocked by policy.
REM
REM  NOTE: keep this file ASCII-only. cmd.exe parses .cmd as ANSI,
REM        so non-ASCII comments would be mangled into bogus commands.
REM ============================================================

chcp 65001 >nul 2>&1
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
set RC=%ERRORLEVEL%

if not "%RC%"=="0" (
    echo.
    echo Stop failed with exit code %RC%.
    pause
)
endlocal
exit /b %RC%
