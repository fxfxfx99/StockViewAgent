#!/usr/bin/env bash
# GitHub 克隆后的推荐入口：同步依赖与配置，然后启动前后端。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
# setup.sh 按依赖文件哈希跳过安装，拉取新代码后也能同步新增依赖。
bash "$ROOT/scripts/setup.sh"
exec bash "$ROOT/scripts/dev.sh"
