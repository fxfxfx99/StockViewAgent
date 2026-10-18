#!/usr/bin/env bash
# 兼容原开发入口；默认稳定运行，--reload / SVA_DEV_RELOAD=true 才开启后端重载。
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_common.sh"
ROOT="$(sva_root)"
PY="$(sva_resolve_python)"
exec "$PY" "$ROOT/scripts/local_supervisor.py" --root "$ROOT" "$@"
