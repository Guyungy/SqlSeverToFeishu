#!/usr/bin/env bash
# ============================================================
# 一键安装依赖脚本 (macOS / Linux)
# 用法：
#   双击运行，或在终端执行：  ./install.sh
#
# 作用：
#   1. 自动检查 Python 是否安装
#   2. 在项目目录创建独立的虚拟环境 .venv（不污染系统）
#   3. 自动安装本项目所需的全部依赖
#   4. 安装完成后提示如何启动
# ============================================================
set -e

cd "$(dirname "$0")"

# ANSI 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}   SQL Server → 飞书 依赖安装工具${NC}"
echo -e "${GREEN}==========================================${NC}"

# ---------- 1. 检查 python3 ----------
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo -e "${RED}[错误] 未检测到 Python。${NC}"
    echo "请先安装 Python 3.10 或更高版本，然后重新运行本脚本。"
    echo "下载地址（任选其一）："
    echo "  官网:   https://www.python.org/downloads/"
    echo "  Homebrew: brew install python3"
    exit 1
fi

echo -e "${YELLOW}[1/3] 检测到 Python: $(command -v "$PY")${NC}"
"$PY" --version
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo -e "${RED}[错误] 需要 Python 3.10 或更高版本。${NC}"
    exit 1
fi

# ---------- 2. 创建虚拟环境 ----------
VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
    echo -e "${YELLOW}[2/3] 正在创建独立环境 .venv ...${NC}"
    "$PY" -m venv "$VENV_DIR"
else
    echo -e "${YELLOW}[2/3] 检测到已有环境 .venv，跳过创建${NC}"
fi

# ---------- 3. 安装依赖 ----------
echo -e "${YELLOW}[3/3] 正在安装依赖（第一次需要几分钟，请耐心等待）...${NC}"
"$VENV_DIR/bin/python" -m pip install --upgrade pip -q
"$VENV_DIR/bin/python" -m pip install -r requirements.txt

echo ""
echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}  安装完成！${NC}"
echo -e "${GREEN}==========================================${NC}"
echo ""
echo "接下来请启动程序："
if [ "$(uname)" = "Darwin" ]; then
    echo "  1. 双击 start.command（推荐小白用）"
else
    echo "  1. 双击 start.sh，或执行 ./start.sh"
fi
echo "  2. 浏览器打开 http://127.0.0.1:5001"
echo ""
if [ "${AUTO_START:-}" = "1" ]; then
    exec ./start.sh
fi

read -r -p "是否现在直接启动程序？(y/n) " ans
if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    exec ./start.sh
fi
