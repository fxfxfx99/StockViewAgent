#!/usr/bin/env bash
# 并行启动后端（FastAPI）与前端（Vite）。依赖已由 setup.sh / start.sh 准备好。
set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(cd "$(dirname "$0")" && pwd)/_common.sh"

ROOT="$(sva_root)"
cd "$ROOT"

BACKEND_PORT="${BACKEND_PORT:-8001}"
FRONTEND_PORT="${FRONTEND_PORT:-5175}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
PID_DIR="$ROOT/pids"

sva_require_node
if ! sva_venv_ok "$ROOT"; then
  echo "后端环境未就绪，先运行安装..."
  bash "$ROOT/scripts/setup.sh"
fi
if ! sva_frontend_ok "$ROOT"; then
  echo "前端环境未就绪，先运行安装..."
  bash "$ROOT/scripts/setup.sh"
fi
sva_ensure_env_files "$ROOT"

cleanup() {
  kill "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
  rm -f "$PID_DIR/backend.pid" "$PID_DIR/frontend.pid"
}
trap cleanup EXIT INT TERM

export VITE_DEV_BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
export VITE_DEV_PORT="${FRONTEND_PORT}"

mkdir -p "$PID_DIR"

(cd "$ROOT/backend" && exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --reload) &
BACKEND_PID=$!
(cd "$ROOT/frontend" && exec npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT") &
FRONTEND_PID=$!

printf '%s\n' "$BACKEND_PID" > "$PID_DIR/backend.pid"
printf '%s\n' "$FRONTEND_PID" > "$PID_DIR/frontend.pid"

echo ""
echo "正在等待后端就绪..."
ready=0
for _ in $(seq 1 60); do
  if "$ROOT/backend/.venv/bin/python" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${BACKEND_PORT}/api/health', timeout=1)" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    sva_die "后端进程已退出，请查看上方日志"
  fi
  sleep 0.5
done

echo "前端  http://${FRONTEND_HOST}:${FRONTEND_PORT}/"
echo "后端  http://127.0.0.1:${BACKEND_PORT}/api/health"
if [[ "$ready" -eq 1 ]]; then
  echo "健康检查已通过。首次使用请打开配置台填写大模型与行情接口。"
else
  echo "后端仍在启动（首次打开本地新闻库可能较慢），请稍后刷新页面。"
fi
echo "文档  docs/LOCAL_SETUP.md"
echo "停止：Ctrl+C"
echo ""

while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done
echo "有进程退出，正在结束另一进程..."
