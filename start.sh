#!/usr/bin/env bash
# ============================================================
# 启动脚本 (macOS / Linux)
# 自动挑选可用的 Python：
#   1. 项目独立环境 .venv/bin/python
#   2. 逐一尝试 python3 / /usr/bin/python3 等，找装了依赖的
# 全都不可用则友好提示
# ============================================================

cd "$(dirname "$0")"
PORT="${DASHBOARD_PORT:-5001}"
PY=""

# 候选列表：.venv 优先，再试常见系统 python3
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
    echo "  - 若未安装依赖：请先运行 ./install.sh（创建独立环境并装依赖）"
    echo "  - 若已装到系统 Python：确认 `python3 -c \"import flask\"` 能成功"
    exit 1
fi

echo "启动面板: http://127.0.0.1:${PORT}"
DASHBOARD_PORT="${PORT}" exec "$PY" dashboard.py