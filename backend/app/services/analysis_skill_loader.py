"""加载 analysis_skills 目录下 YAML：扩展新闻/K线等提示词，无需改 Python。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "analysis_skills"


def _safe_read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@lru_cache(maxsize=32)
def load_skill(skill_id: str) -> dict[str, Any]:
    sid = (skill_id or "news_impact").strip() or "news_impact"
    p = _SKILLS_DIR / f"{sid}.yaml"
    if not p.exists():
        p = _SKILLS_DIR / "news_impact.yaml"
    if not p.exists():
        return {"name": sid, "instructions": ""}
    return _safe_read_yaml(p)


def list_skill_ids() -> list[str]:
    if not _SKILLS_DIR.is_dir():
        return []
    out = []
    for p in sorted(_SKILLS_DIR.glob("*.yaml")):
        out.append(p.stem)
    return out
