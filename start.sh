#!/usr/bin/env bash
# GitHub 克隆后的推荐入口：缺环境则安装，然后启动前后端。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/_common.sh
source "$ROOT/scripts/_common.sh"

need_setup=0
sva_venv_ok "$ROOT" || need_setup=1
sva_frontend_ok "$ROOT" || need_setup=1
[[ -f "$ROOT/backend/.env" ]] || need_setup=1

if [[ "$need_setup" -eq 1 ]]; then
  echo "检测到首次运行或环境不完整，正在自动配置..."
  bash "$ROOT/scripts/setup.sh"
fi

exec bash "$ROOT/scripts/dev.sh"
