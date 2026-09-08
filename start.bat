@echo off
chcp 65001 >nul
REM ============================================================
REM  启动脚本 (Windows)
REM  自动挑选可用的 Python：
REM   1. 项目独立环境 .venv（若存在）
REM   2. 逐个尝试本机 python / py，找到装了依赖的那个
REM  全都不可用则给友好提示
REM ============================================================
setlocal
cd /d "%~dp0"
set "DASHBOARD_PORT=5001"
set "PY="

REM ---------- 依次测试候选 Python ----------
call :try_py ".venv\Scripts\python.exe"   "项目独立环境 (.venv)"
call :try_py "python"                     "本机系统 Python (python)"
call :try_py "py"                         "本机系统 Python (py)"

if not defined PY (
    echo.
    echo [错误] 没有找到可用的 Python 环境。
    echo   - 若未安装依赖：请先运行 install.bat
    echo   - 若已装到系统 Python：请确认网络/安装正确后重试
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
exit /b 0

REM ---------- 子过程：测试某个 python 是否可用 ----------
:try_py
if defined PY exit /b 0
set "CAND=%~1"
set "CAND_NAME=%~2"
where "%CAND%" >nul 2>nul
if errorlevel 1 exit /b 0
%CAND% -c "import flask, pymssql, requests, dotenv" >nul 2>nul
if errorlevel 1 (
    echo [跳过] %CAND_NAME% (%CAND%) 未安装全部依赖
    exit /b 0
)
echo [环境] 使用 %CAND_NAME% (%CAND%)
set "PY=%CAND%"
exit /b 0