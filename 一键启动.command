#!/usr/bin/env bash
# macOS 小白入口：首次自动安装，以后直接启动。
set -e
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ] && \
   ".venv/bin/python" -c 'import sys, flask, pymssql, requests, dotenv; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    exec ./start.sh
fi

echo "首次运行，正在自动安装所需环境……"
AUTO_START=1 exec ./install.sh
