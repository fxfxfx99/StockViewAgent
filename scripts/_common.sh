#!/usr/bin/env bash
# 供 setup.sh / dev.sh / check-env.sh 共用，勿直接执行。
set -euo pipefail

sva_root() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$here/.." && pwd
}

sva_die() {
  echo "错误：$1" >&2
  exit 1
}

sva_python_ok() {
  local bin="$1"
  "$bin" -c 'import sys; v=sys.version_info[:2]; raise SystemExit(0 if (3,11)<=v<=(3,13) else 1)' 2>/dev/null
}

# 解析 Python 3.11–3.13。3.14 当前无法安装 pydantic-core 预编译轮子。
sva_resolve_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    command -v "$PYTHON_BIN" >/dev/null 2>&1 || sva_die "PYTHON_BIN=$PYTHON_BIN 不存在"
    sva_python_ok "$PYTHON_BIN" || sva_die "需要 Python 3.11–3.13（当前 $($PYTHON_BIN -V 2>&1)）。可安装 3.12 后重试，或设置 PYTHON_BIN。"
    command -v "$PYTHON_BIN"
    return
  fi
  local cand root
  for cand in python3.13 python3.12 python3.11; do
    if command -v "$cand" >/dev/null 2>&1 && sva_python_ok "$(command -v "$cand")"; then
      command -v "$cand"
      return
    fi
  done
  root="$(sva_root)"
  if [[ -x "$root/backend/.venv/bin/python" ]] && sva_python_ok "$root/backend/.venv/bin/python"; then
    echo "$root/backend/.venv/bin/python"
    return
  fi
  if command -v python3 >/dev/null 2>&1 && sva_python_ok "$(command -v python3)"; then
    command -v python3
    return
  fi
  local have=""
  command -v python3 >/dev/null 2>&1 && have="（当前 $(python3 -V 2>&1)）"
  sva_die "未找到 Python 3.11–3.13${have}。请安装后重试（勿使用 3.14），或设置 PYTHON_BIN=/path/to/python3.12"
}

sva_require_node() {
  command -v node >/dev/null 2>&1 || sva_die "未找到 Node.js。请安装 Node 20、22 或 24（需包含 npm）：https://nodejs.org/"
  command -v npm >/dev/null 2>&1 || sva_die "未找到 npm。请安装完整的 Node.js 发行版。"
  local major
  major="$(node -p "process.versions.node.split('.')[0]")"
  if [[ "$major" -lt 20 ]]; then
    sva_die "需要 Node.js 20+（当前 $(node -v)）"
  fi
}

sva_venv_ok() {
  local root="$1"
  [[ -x "$root/backend/.venv/bin/python" ]] || return 1
  sva_python_ok "$root/backend/.venv/bin/python" || return 1
  "$root/backend/.venv/bin/python" -c "import uvicorn, fastapi" >/dev/null 2>&1
}

sva_frontend_ok() {
  local root="$1"
  [[ -d "$root/frontend/node_modules" ]] || return 1
  (cd "$root/frontend" && node -e "require('rollup/dist/native.js')" >/dev/null 2>&1)
}

sva_ensure_env_files() {
  local root="$1"
  mkdir -p "$root/backend/data"
  if [[ ! -f "$root/backend/.env" ]]; then
    cp "$root/backend/.env.example" "$root/backend/.env"
    echo "已创建 backend/.env（密钥请在浏览器配置台填写，不要提交此文件）"
  fi
  chmod 600 "$root/backend/.env" 2>/dev/null || true
  if ! grep -q '^AUTH_JWT_SECRET=.\+' "$root/backend/.env"; then
    local secret
    secret="$("$root/backend/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null || python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf '\n# 由 scripts/setup.sh 生成，请勿提交\nAUTH_JWT_SECRET=%s\n' "$secret" >> "$root/backend/.env"
    echo "已写入随机 AUTH_JWT_SECRET"
  fi
  if [[ ! -f "$root/frontend/.env" && -f "$root/frontend/.env.example" ]]; then
    cp "$root/frontend/.env.example" "$root/frontend/.env"
    echo "已创建 frontend/.env（开发时代理目标；./scripts/dev.sh 会按端口覆盖）"
  fi
}
