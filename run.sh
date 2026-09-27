#!/usr/bin/env bash
# 开发用启动脚本。生产建议用 systemd / supervisor 直接跑 uvicorn 命令。
set -euo pipefail

cd "$(dirname "$0")"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

if [[ "${RELOAD:-0}" == "1" ]]; then
  exec python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
else
  exec python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT"
fi
