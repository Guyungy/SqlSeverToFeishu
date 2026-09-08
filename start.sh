#!/usr/bin/env bash
# ============================================================
# 启动脚本 (macOS / Linux)
# 用法：先运行 ./install.sh 安装依赖，再 ./start.sh 启动
# ============================================================
set -e

cd "$(dirname "$0")"
PORT="${DASHBOARD_PORT:-5001}"

# 优先使用项目虚拟环境；没有则回退到系统 python3
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
    echo "使用当前项目的 Python 环境 ( .venv )"
else
    echo "未找到 .venv，回退使用系统 python3（请先运行 ./install.sh 安装依赖）"
    PY="python3"
fi

echo "启动面板: http://127.0.0.1:${PORT}"
DASHBOARD_PORT="${PORT}" exec "$PY" dashboard.py