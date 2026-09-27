#!/usr/bin/env bash
# 开发用启动脚本。生产建议用 systemd / supervisor 直接跑 uvicorn 命令。
set -euo pipefail

cd "$(dirname "$0")"

# 默认只监听回环：这是开发脚本，绑 0.0.0.0 会让同网段（甚至公网）都能直接摸到后台。
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8123}"

command -v python3 >/dev/null 2>&1 || { echo "找不到 python3，请先安装或加入 PATH" >&2; exit 1; }
python3 -c 'import uvicorn' 2>/dev/null || {
  echo "未安装 uvicorn，请先执行：python3 -m pip install -r requirements.txt" >&2
  exit 1
}

# RELOAD 接受 1/true/yes/on（大小写不敏感）。
case "$(printf '%s' "${RELOAD:-0}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) exec python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT" --reload ;;
  *)             exec python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT" ;;
esac
