#!/usr/bin/env bash
# 克隆后首次运行：创建 venv、安装依赖、生成 .env。
set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(cd "$(dirname "$0")" && pwd)/_common.sh"

ROOT="$(sva_root)"
cd "$ROOT"

echo "== StockViewAgent 环境初始化 =="
echo "项目目录: $ROOT"

PY="$(sva_resolve_python)"
echo "使用 Python: $PY ($("$PY" -V 2>&1))"
sva_require_node
echo "使用 Node:   $(node -v) / npm $(npm -v)"

mkdir -p "$ROOT/backend/data" "$ROOT/pids"

if [[ -d "$ROOT/backend/.venv" ]] && ! sva_venv_ok "$ROOT"; then
  echo "现有虚拟环境不可用（常见于换机或 Python 版本不符），将重建..."
  mv "$ROOT/backend/.venv" "$ROOT/backend/.venv.invalid.$(date +%s)"
fi

if ! sva_venv_ok "$ROOT"; then
  echo "创建 Python 虚拟环境..."
  "$PY" -m venv "$ROOT/backend/.venv"
fi

REQ_HASH="$(shasum -a 256 "$ROOT/backend/requirements.txt" | awk '{print $1}')"
if [[ ! -f "$ROOT/backend/.venv/.requirements-hash" ]] || \
   [[ "$(cat "$ROOT/backend/.venv/.requirements-hash")" != "$REQ_HASH" ]] || \
   ! "$ROOT/backend/.venv/bin/python" -c "import uvicorn, fastapi" >/dev/null 2>&1; then
  echo "安装后端依赖..."
  "$ROOT/backend/.venv/bin/python" -m pip install -q -U pip
  "$ROOT/backend/.venv/bin/python" -m pip install -r "$ROOT/backend/requirements.txt"
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
PKG_HASH="$(shasum -a 256 "$LOCK" | awk '{print $1}')"
if [[ ! -d "$ROOT/frontend/node_modules" ]] || \
   [[ ! -f "$ROOT/frontend/node_modules/.package-hash" ]] || \
   [[ "$(cat "$ROOT/frontend/node_modules/.package-hash")" != "$PKG_HASH" ]] || \
   ! sva_frontend_ok "$ROOT"; then
  echo "安装前端依赖..."
  if ! (cd "$ROOT/frontend" && npm ci); then
    echo "npm ci 失败，改用 npm install..."
    (cd "$ROOT/frontend" && npm install)
  fi
  printf '%s\n' "$PKG_HASH" > "$ROOT/frontend/node_modules/.package-hash"
else
  echo "前端依赖已是最新，跳过 npm ci"
fi

echo ""
echo "完成。下一步："
echo "  1. ./start.sh"
echo "  2. 打开 http://127.0.0.1:5175/  → 配置台"
echo "  3. 填写大模型 API（OpenAI 兼容）与行情接口（推荐 KlineShare）"
echo ""
echo "说明：docs/LOCAL_SETUP.md"
echo ""
