@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys, flask, pymssql, requests, dotenv; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 goto :start
)

echo 首次运行，正在自动安装所需环境……
call install.bat /auto
exit /b %errorlevel%

:start
call start.bat
