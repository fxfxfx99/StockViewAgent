#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$ROOT/backend/.venv/bin/python" ]]; then
  echo "请先运行 ./scripts/setup.sh，建立 backend/.venv。" >&2
  exit 1
fi
exec "$ROOT/backend/.venv/bin/python" "$ROOT/scripts/public_trial.py" "${@:-status}"
