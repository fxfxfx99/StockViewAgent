"""本机 API Token 覆盖项（与 .env 并存：此处非空则优先）。文件见 backend/data/integrations.json（勿提交）。

优先级示例（详见 backend/docs/INTEGRATIONS_AND_CACHE.md）：
- 雪球 Cookie：`integrations.json` 的 `xueqiu_cookies` 非空 → 仅用该项；否则用环境变量 `XUEQIU_COOKIES`。
- Tushare 平台兜底：`integrations.json` 的 `tushare_token` 非空优先于 `TUSHARE_TOKEN`；登录用户个人凭据优先级更高。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import settings
from app.storage import data_asset_manager

_FILE_NAME = "integrations.json"


def _path() -> Path:
    return settings.data_dir / _FILE_NAME


def load_integrations() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, Any]) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    data_asset_manager.atomic_write_json(
        _path(),
        data,
        kind="integrations_config",
        key="integrations",
        source="admin",
        item_count=len(data),
    )


def merge_integrations(patch: dict[str, Any]) -> dict[str, Any]:
    """空字符串表示删除该键（回退到环境变量等默认来源）。"""
    cur = load_integrations()
    for key, val in patch.items():
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            cur.pop(key, None)
        elif isinstance(val, str):
            cur[key] = val.strip()
        else:
            cur[key] = val
    _save(cur)
    return cur
