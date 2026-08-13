#!/usr/bin/env bash
# Claude-Control 电脑端一键启动（macOS / Linux）
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "[setup] 创建虚拟环境 .venv ..."
  python3 -m venv .venv
fi

echo "[setup] 安装依赖 ..."
.venv/bin/python -m pip install -q -r requirements.txt

echo "[run] 启动 relay_server.py ..."
exec .venv/bin/python relay_server.py "$@"
