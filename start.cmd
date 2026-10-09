@echo off
REM ============================================================
REM  AIOps Platform - one-click start (RECOMMENDED)
REM
REM  Why this file exists:
REM    Running .\start.ps1 directly may be refused by the Windows
REM    execution policy for unsigned scripts:
REM      "start.ps1 cannot be loaded. The file is not digitally signed."
REM    That is Windows security behaviour, not a bug in the script.
REM    This wrapper passes -ExecutionPolicy Bypass for THIS launch only.
REM    It does not change your system execution policy.
REM
REM  NOTE: keep this file ASCII-only. cmd.exe parses .bat/.cmd as ANSI,
REM        so non-ASCII comments here would be mangled into bogus commands.
REM ============================================================

chcp 65001 >nul 2>&1
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set RC=%ERRORLEVEL%

REM When double-clicked the window closes immediately, hiding any error.
if not "%RC%"=="0" (
    echo.
    echo Startup failed with exit code %RC%. Please report the messages above.
    pause
)
endlocal
exit /b %RC%
