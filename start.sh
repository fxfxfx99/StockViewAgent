#!/usr/bin/env bash
# 统一入口：准备依赖、启动服务并打开客户端；默认随全部客户端页面关闭。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec bash "$ROOT/scripts/dev.sh" "$@"
