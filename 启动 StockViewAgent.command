#!/usr/bin/env bash
# macOS Finder 双击入口；与终端中的 ./start.sh 使用相同启动和退出流程。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)" || exit 1
cd "$ROOT" || exit 1

bash "$ROOT/start.sh" "$@"
status=$?
if [[ "$status" -ne 0 ]]; then
  printf '\nStockViewAgent 未能正常运行（退出码 %s）。请查看上方错误信息。\n' "$status" >&2
  if [[ -t 0 ]]; then
    printf '按回车关闭此窗口……' >&2
    read -r _ || true
  fi
fi
exit "$status"
