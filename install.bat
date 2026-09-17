@echo off
chcp 65001 >nul
REM ============================================================
REM  一键安装依赖脚本 (Windows)
REM  用法：双击运行本文件
REM
REM  作用：
REM   1. 自动检查 Python 是否安装
REM   2. 在项目目录创建独立的虚拟环境 .venv（不污染系统）
REM   3. 自动安装本项目所需的全部依赖
REM   4. 安装完成后询问是否启动
REM ============================================================
setlocal
cd /d "%~dp0"

echo ==========================================
echo    SQL Server ? 飞书 依赖安装工具
echo ==========================================

REM ---------- 1. 检查 python ----------
where python >nul 2>nul
if %errorlevel%==0 (
    set "PY=python"
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        set "PY=py"
    ) else (
        echo [错误] 未检测到 Python。
        echo 请先安装 Python 3.10 或更高版本，然后重新运行本脚本。
        echo 下载地址：https://www.python.org/downloads/
        echo 安装时务必勾选 "Add Python to PATH"！
        pause
        exit /b 1
    )
)

echo [1/3] 检测到 Python: %PY%
%PY% --version
%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo [错误] 需要 Python 3.10 或更高版本。
    pause
    exit /b 1
)

REM ---------- 2. 创建虚拟环境 ----------
if not exist ".venv" (
    echo [2/3] 正在创建独立环境 .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
) else (
    echo [2/3] 检测到已有环境 .venv，跳过创建
)

REM ---------- 3. 安装依赖 ----------
echo [3/3] 正在安装依赖（第一次需要几分钟，请耐心等待）...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
)

echo.
echo ==========================================
echo    安装完成！
echo ==========================================
echo.
echo 请双击 start.bat 启动程序，然后浏览器打开 http://127.0.0.1:5001
echo.

if /I "%~1"=="/auto" (
    call start.bat
    exit /b %errorlevel%
)

choice /c YN /m "是否现在直接启动程序 (Y/N)"
if errorlevel 2 goto :end
call start.bat

:end
pause
