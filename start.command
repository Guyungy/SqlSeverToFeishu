#!/usr/bin/env bash
# macOS 双击启动入口：统一复用 start.sh，避免两套启动逻辑不一致。
cd "$(dirname "$0")"
exec ./start.sh
