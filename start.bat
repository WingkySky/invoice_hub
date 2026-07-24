@echo off
chcp 65001 >nul
title Invoice Hub Server

set "PY=C:\Users\Ifesco\AppData\Local\Python\pythoncore-3.14-64\python.exe"

netstat -ano | findstr "LISTENING" | findstr ":8000 " >nul 2>&1
if %errorlevel% equ 0 (
    echo [!] Port 8000 is already in use. Run stop.bat first.
    pause
    exit /b 1
)

echo ========================================
echo   Invoice Hub - Starting...
echo ========================================
echo.

cd /d "%~dp0"
"%PY%" hub.py serve --port 8000

pause