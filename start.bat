@echo off
chcp 65001 >nul
REM ============================================================
REM  启动脚本 (Windows)
REM  用法：先运行 install.bat 安装依赖，再双击本文件启动
REM ============================================================
setlocal
cd /d "%~dp0"
set "DASHBOARD_PORT=5001"

REM 优先使用项目虚拟环境；没有则提示先安装
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    echo 使用当前项目的 Python 环境 ( .venv )
) else (
    echo 未找到 .venv，请先双击 install.bat 安装依赖！
    echo 按任意键退出...
    pause
    exit /b 1
)

echo 启动面板: http://127.0.0.1:%DASHBOARD_PORT%
echo （启动后请勿关闭本窗口）
echo.
%PY% dashboard.py

if errorlevel 1 (
    echo.
    echo 程序出错退出，按任意键关闭窗口...
    pause
)