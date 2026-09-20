#!/usr/bin/env bash
# 检查本机是否满足运行条件（不安装依赖）。
set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(cd "$(dirname "$0")" && pwd)/_common.sh"

ROOT="$(sva_root)"
cd "$ROOT"

echo "== StockViewAgent 环境检查 =="
echo "项目目录: $ROOT"

ok=0
fail=0

check() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "  [ok] $name"
    ok=$((ok + 1))
  else
    echo "  [!!] $name"
    fail=$((fail + 1))
  fi
}

PY="$(sva_resolve_python)"
echo "  Python: $PY ($("$PY" -V 2>&1))"
sva_require_node
echo "  Node:   $(node -v)  npm $(npm -v)"

check "后端虚拟环境 backend/.venv（未就绪时运行 ./scripts/setup.sh）" sva_venv_ok "$ROOT"
check "前端依赖 frontend/node_modules（未就绪时运行 ./scripts/setup.sh）" sva_frontend_ok "$ROOT"

check "backend/.env（缺少时运行 ./scripts/setup.sh）" test -f "$ROOT/backend/.env"
check "backend/.env.example" test -f "$ROOT/backend/.env.example"

echo ""
echo "检查结果：${ok} 项通过，${fail} 项未就绪"
echo "默认端口：后端 8001 · 前端 5175（可用 BACKEND_PORT / FRONTEND_PORT 覆盖）"
echo "详细步骤见 docs/LOCAL_SETUP.md"
echo ""
if [[ "$fail" -gt 0 ]]; then
  exit 1
fi
