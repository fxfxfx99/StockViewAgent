#!/usr/bin/env bash
# 克隆后的统一入口：监督器串行准备依赖、打开浏览器并恢复意外退出的服务。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec bash "$ROOT/scripts/dev.sh" "$@"
