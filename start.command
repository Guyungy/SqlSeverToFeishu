#!/usr/bin/env bash
# ============================================================
# macOS 双击启动文件（无需打开终端）
# 自动挑选可用 Python：.venv 优先，否则找装了依赖的系统 Python
# ============================================================
cd "$(dirname "$0")"
PORT=5001
PY=""

CANDIDATES=(
    ".venv/bin/python:项目独立环境 (.venv)"
    "python3:本机 Python (python3)"
    "/usr/bin/python3:系统 Python (/usr/bin/python3)"
    "/opt/homebrew/bin/python3:Homebrew Python"
)

for entry in "${CANDIDATES[@]}"; do
    [ -n "$PY" ] && break
    cmd="${entry%%:*}"
    name="${entry#*:}"
    if command -v "$cmd" >/dev/null 2>&1 || [ -x "$cmd" ]; then
        if "$cmd" -c "import flask, pymssql, requests, dotenv" >/dev/null 2>&1; then
            echo "[环境] 使用 $name ($cmd)"
            PY="$cmd"
        else
            echo "[跳过] $name ($cmd) 未安装全部依赖"
        fi
    fi
done

if [ -z "$PY" ]; then
    echo ""
    echo "[错误] 没有找到可用的 Python 环境。"
    echo "点击 install.command 可自动创建独立环境并安装依赖。"
    read -r -p "按回车键退出" _
    exit 1
fi

echo "启动面板: http://127.0.0.1:${PORT}"
echo "（启动后请勿关闭此窗口）"
echo ""
DASHBOARD_PORT="$PORT" exec "$PY" dashboard.py