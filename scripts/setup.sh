#!/usr/bin/env bash
# 克隆后首次运行：创建 venv、安装依赖、生成 .env。
set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(cd "$(dirname "$0")" && pwd)/_common.sh"

ROOT="$(sva_root)"
cd "$ROOT"

PY="$(sva_resolve_python)"
STOCKVIEW_DATA_DIR="$("$PY" "$ROOT/backend/app/runtime_paths.py" --backend-dir "$ROOT/backend")"
export STOCKVIEW_DATA_DIR
# setup.sh 也可能被单独或并发调用；仅锁依赖安装，不锁业务数据。
if [[ "${SVA_SETUP_LOCKED:-}" != "1" ]]; then
  mkdir -p "$STOCKVIEW_DATA_DIR/runtime/pids"
  exec "$PY" - "$ROOT" "$STOCKVIEW_DATA_DIR" "$0" "$@" <<'PY'
import fcntl
import os
from pathlib import Path
import subprocess
import sys

root, data_dir, script, *args = sys.argv[1:]
with (Path(data_dir) / "runtime/pids/setup.lock").open("a+") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    # Separate data roots still share this checkout's virtualenv/node_modules.
    # Lock the directory inode too, without adding runtime files outside data/.
    project_fd = os.open(Path(root) / "backend", os.O_RDONLY)
    try:
        fcntl.flock(project_fd, fcntl.LOCK_EX)
        env = {**os.environ, "SVA_SETUP_LOCKED": "1"}
        raise SystemExit(subprocess.call(["bash", script, *args], env=env))
    finally:
        os.close(project_fd)
PY
fi

echo "== StockViewAgent 环境初始化 =="
echo "项目目录: $ROOT"
echo "数据目录: $STOCKVIEW_DATA_DIR"

echo "使用 Python: $PY ($("$PY" -V 2>&1))"
sva_require_node
echo "使用 Node:   $(node -v) / npm $(npm -v)"

mkdir -p "$STOCKVIEW_DATA_DIR/runtime/pids"

if [[ -d "$ROOT/backend/.venv" ]] && ! sva_python_ok "$ROOT/backend/.venv/bin/python"; then
  echo "现有虚拟环境解释器不可用（常见于换机或 Python 版本不符），将重建..."
  mv "$ROOT/backend/.venv" "$ROOT/backend/.venv.invalid.$(date +%s)"
fi

if [[ ! -x "$ROOT/backend/.venv/bin/python" ]]; then
  echo "创建 Python 虚拟环境..."
  "$PY" -m venv "$ROOT/backend/.venv"
fi

REQ_HASH="$("$PY" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$ROOT/backend/requirements.txt")"
BACKEND_HEALTHY=1
if ! "$ROOT/backend/.venv/bin/python" "$ROOT/scripts/check_dependencies.py" "$ROOT/backend/requirements.txt"; then
  BACKEND_HEALTHY=0
fi
if [[ ! -f "$ROOT/backend/.venv/.requirements-hash" ]] || \
   [[ "$(cat "$ROOT/backend/.venv/.requirements-hash")" != "$REQ_HASH" ]] || \
   [[ "$BACKEND_HEALTHY" -ne 1 ]]; then
  echo "安装后端依赖..."
  if ! "$ROOT/backend/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
    "$ROOT/backend/.venv/bin/python" -m ensurepip
  fi
  PIP_REPAIR=()
  # 已有包的元数据可能尚在但模块/本地轮子损坏，普通 install 会误判已满足。
  if [[ "$BACKEND_HEALTHY" -ne 1 ]]; then PIP_REPAIR=(--force-reinstall); fi
  "$ROOT/backend/.venv/bin/python" -m pip install "${PIP_REPAIR[@]}" -r "$ROOT/backend/requirements.txt"
  "$ROOT/backend/.venv/bin/python" "$ROOT/scripts/check_dependencies.py" "$ROOT/backend/requirements.txt"
  printf '%s\n' "$REQ_HASH" > "$ROOT/backend/.venv/.requirements-hash"
else
  echo "后端依赖已是最新，跳过 pip install"
fi

sva_ensure_env_files "$ROOT"

if [[ -d "$ROOT/frontend/node_modules" ]] && ! sva_frontend_ok "$ROOT"; then
  echo "前端依赖异常，将重新安装..."
  mv "$ROOT/frontend/node_modules" "$ROOT/frontend/node_modules.invalid.$(date +%s)"
fi

LOCK="$ROOT/frontend/package-lock.json"
PKG_HASH="$("$PY" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$LOCK")"
if [[ ! -d "$ROOT/frontend/node_modules" ]] || \
   [[ ! -f "$ROOT/frontend/node_modules/.package-hash" ]] || \
   [[ "$(cat "$ROOT/frontend/node_modules/.package-hash")" != "$PKG_HASH" ]] || \
   ! sva_frontend_ok "$ROOT"; then
  echo "安装前端依赖..."
  (cd "$ROOT/frontend" && npm ci)
  printf '%s\n' "$PKG_HASH" > "$ROOT/frontend/node_modules/.package-hash"
else
  echo "前端依赖已是最新，跳过 npm ci"
fi

echo ""
echo "完成。下一步："
echo "  1. ./start.sh"
echo "  2. 打开 http://127.0.0.1:5175/  → 配置台"
echo "  3. 只需填写大模型 API（OpenAI 兼容）；免费行情源默认可用，付费行情接口可选"
echo ""
echo "说明：docs/LOCAL_SETUP.md"
echo ""
