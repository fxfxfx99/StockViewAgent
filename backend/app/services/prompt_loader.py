"""Read analysis prompts; optional local overrides in data/prompt_overrides.json."""
from __future__ import annotations

import json
from pathlib import Path

from app.config import settings

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_TITLES = {
    "system_prompt": "系统指令",
    "news_relevance": "相关性判断",
    "stock_impact_analysis": "潜在影响分析",
}
_ALLOWED = set(PROMPT_TITLES)
MAX_PROMPT_LENGTH = 20000


def _overrides_path() -> Path:
    return settings.data_dir / "prompt_overrides.json"


def load_prompt(name: str) -> str:
    """Used by the analyzer on every request; overrides apply without a restart."""
    if name not in _ALLOWED:
        raise ValueError("Unknown analysis prompt")
    text = ""
    try:
        value = json.loads(_overrides_path().read_text(encoding="utf-8"))
        prompts = value.get("prompts", {}) if isinstance(value, dict) else {}
        candidate = prompts.get(name) if isinstance(prompts, dict) else None
        if isinstance(candidate, str) and candidate.strip() and len(candidate) <= MAX_PROMPT_LENGTH:
            text = candidate
    except (OSError, ValueError):
        pass
    if not text.strip():
        text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("Analysis prompt is empty")
    return text.strip()
