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
if ! sva_venv_ok "$ROOT" || ! sva_frontend_ok "$ROOT"; then
  echo "运行环境未就绪，先运行安装..."
  bash "$ROOT/scripts/setup.sh"
fi
sva_ensure_env_files "$ROOT"

# 提前拒绝非法或已占用端口，避免把其它服务的健康响应当成本次启动成功。
"$ROOT/backend/.venv/bin/python" - "$BACKEND_PORT" "$FRONTEND_PORT" "$FRONTEND_HOST" <<'PY'
import socket
import sys

backend, frontend, host = sys.argv[1:]
listeners = []
try:
    for name, address, value in (("后端", "127.0.0.1", backend), ("前端", host, frontend)):
        if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 65535:
            raise ValueError(f"{name}端口必须为 1–65535 的整数")
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        listener = socket.socket(family)
        listeners.append(listener)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((address, int(value)))
        listener.listen(1)
except (OSError, ValueError) as exc:
    sys.exit(f"错误：{name}端口 {address}:{value} 不可用：{exc}；请设置 BACKEND_PORT / FRONTEND_PORT 后重试")
finally:
    for listener in listeners:
        listener.close()
PY

service_alive() {
  [[ -n "$1" ]] && { kill -0 -- "-$1" 2>/dev/null || kill -0 "$1" 2>/dev/null; }
}

signal_service() {
  # launch_service 为本次子进程单独建组；若信号早于建组，回退到该子进程自身。
  kill "-$1" -- "-$2" 2>/dev/null || kill "-$1" "$2" 2>/dev/null || true
}

cleanup() {
  trap '' INT TERM
  local name pid path attempt alive
  local pids=("${BACKEND_PID:-}" "${FRONTEND_PID:-}")
  for pid in "${pids[@]}"; do
    [[ -n "$pid" ]] && signal_service TERM "$pid"
  done
  # 请求最多优雅收尾 10 秒；额外给 lifespan / reloader 5 秒，然后只清理本次进程组。
  for ((attempt = 0; attempt < 30; attempt++)); do
    alive=0
    for pid in "${pids[@]}"; do
      if service_alive "$pid"; then alive=1; fi
    done
    [[ "$alive" -eq 0 ]] && break
    sleep 0.5
  done
  for name in backend frontend; do
    if [[ "$name" == backend ]]; then pid="${BACKEND_PID:-}"; else pid="${FRONTEND_PID:-}"; fi
    [[ -n "$pid" ]] || continue
    if service_alive "$pid"; then
      echo "$name 停止超时，强制结束本次启动的进程组 $pid" >&2
      signal_service KILL "$pid"
    fi
    wait "$pid" 2>/dev/null || true
    path="$PID_DIR/$name.pid"
    if [[ -f "$path" && "$(cat "$path")" == "$pid" ]]; then rm -f "$path"; fi
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

export VITE_DEV_BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
export VITE_DEV_PORT="${FRONTEND_PORT}"

mkdir -p "$PID_DIR"

launch_service() {
  # macOS 没有 setsid 命令，使用现有 Python；exec 保持 $! 为进程组 ID。
  exec "$ROOT/backend/.venv/bin/python" -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@"
}

(cd "$ROOT/backend" && launch_service ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --reload --timeout-graceful-shutdown 10) &
BACKEND_PID=$!
# 直接管理 Vite 进程，避免结束 npm 包装进程后遗留占用端口的子进程。
(cd "$ROOT/frontend" && launch_service node ./node_modules/vite/bin/vite.js --host "$FRONTEND_HOST" --port "$FRONTEND_PORT") &
FRONTEND_PID=$!

printf '%s\n' "$BACKEND_PID" > "$PID_DIR/backend.pid"
printf '%s\n' "$FRONTEND_PID" > "$PID_DIR/frontend.pid"

echo ""
echo "正在等待前后端就绪..."
ready=0
for ((attempt = 0; attempt < 60; attempt++)); do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    sva_die "后端进程已退出，请查看上方日志"
  fi
  if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    sva_die "前端进程已退出，请查看上方日志"
  fi
  if "$ROOT/backend/.venv/bin/python" - "$BACKEND_PORT" "$FRONTEND_PORT" "$FRONTEND_HOST" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

backend, frontend, host = sys.argv[1:]
host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
if ":" in host:
    host = f"[{host}]"
# 本地就绪探测不受终端 HTTP_PROXY / HTTPS_PROXY 影响。
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for url in (f"http://127.0.0.1:{backend}/api/ready", f"http://{host}:{frontend}/"):
    with client.open(url, timeout=1) as response:
        if response.status != 200:
            raise RuntimeError(f"服务尚未就绪：{url}")
PY
  then
    ready=1
    break
  fi
  sleep 0.5
done

if [[ "$ready" -ne 1 ]]; then
  sva_die "前后端未能在等待期内就绪，请检查上方日志"
fi
echo "前端  http://${FRONTEND_HOST}:${FRONTEND_PORT}/"
echo "后端  http://127.0.0.1:${BACKEND_PORT}/api/health"
echo "前后端就绪检查已通过。首次使用请打开配置台填写大模型与行情接口。"
echo "文档  docs/LOCAL_SETUP.md"
echo "停止：Ctrl+C"
echo ""

while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done
sva_die "服务进程意外退出，正在结束另一进程，请查看上方日志"
