@echo off
chcp 65001 >nul
title Invoice Hub - Stop

echo ========================================
echo   Invoice Hub - Stopping...
echo ========================================
echo.

set "found=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8000 "') do (
    echo [+] Killing process PID: %%a
    taskkill /PID %%a /F >nul 2>&1
    set "found=1"
)

if "%found%"=="0" (
    echo [i] No service running on port 8000
) else (
    echo [ok] Service stopped
)

echo.
pause