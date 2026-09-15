"""用户隔离的选股 Agent 定义与运行记录。"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from app.config import settings


def _conn() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.data_dir / "stock_picker_agents.db")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS stock_picker_agent (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
          description TEXT NOT NULL, enabled INTEGER NOT NULL, definition_json TEXT NOT NULL,
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_picker_agent_user ON stock_picker_agent(user_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS stock_picker_run (
          id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, user_id INTEGER NOT NULL,
          symbols_json TEXT NOT NULL, result_json TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_picker_run_user ON stock_picker_run(user_id, created_at DESC);
        """)


DEFAULT_TAIL_AGENT = {
    "name": "尾盘选股器",
    "description": "按趋势、资金、位置、尾盘承接和风险过滤筛选 A 股候选。",
    "enabled": True,
    "definition": {
        "agent_type": "tail_session",
        "system_prompt": """你是专业、审慎、规则驱动的尾盘选股器 Agent。只能依据系统数据，禁止预测必涨或编造缺失数据。严格遵循：市场环境→强势板块→资金认可个股→价格位置→14:30后承接→风险过滤。趋势决定方向，资金决定强弱，位置决定赔率，承接决定时点，风险决定是否出手。板块退潮、高位加速、尾盘脉冲拉升、放量滞涨、跌破关键支撑、流动性不足、重大风险或关键数据严重缺失时应排除。尾盘分钟数据缺失时不得判定强承接；资金缺失时资金项不得高分。输出必须客观、可验证，不构成投资建议。""",
        "dimensions": [
            {"key": "trend", "label": "趋势", "weight": 25},
            {"key": "capital", "label": "资金", "weight": 25},
            {"key": "position", "label": "位置", "weight": 20},
            {"key": "support", "label": "承接", "weight": 25},
            {"key": "risk", "label": "风险修正", "weight": 5},
        ],
        "hard_filters": ["板块明显退潮", "连续大涨后的高位加速", "尾盘脉冲拉升且无持续成交", "放量滞涨或冲高回落", "跌破关键支撑", "流动性过低", "重大风险", "关键数据严重缺失"],
        "data_requirements": ["市场环境", "板块强度", "日线K线", "资金流", "14:30后分钟K线", "风险事件"],
        "min_score": 65,
        "max_selected": 5,
        "max_grade_a": 3,
        "output_instruction": "返回 market_environment、selected_stocks、watchlist、excluded_stocks、summary、risk_disclaimer 的 JSON 对象。",
    },
}


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["enabled"] = bool(out["enabled"])
    out["definition"] = json.loads(out.pop("definition_json") or "{}")
    return out


def list_agents(user_id: int) -> list[dict[str, Any]]:
    init_db()
    with _conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM stock_picker_agent WHERE user_id=?", (user_id,)).fetchone()[0]
    if not count:
        create_agent(user_id, DEFAULT_TAIL_AGENT)
    with _conn() as conn:
        return [_decode(r) for r in conn.execute("SELECT * FROM stock_picker_agent WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()]


def create_agent(user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    init_db(); now = time.time(); aid = str(uuid.uuid4())
    with _conn() as conn:
        conn.execute("INSERT INTO stock_picker_agent VALUES (?,?,?,?,?,?,?,?)", (aid, user_id, payload["name"], payload.get("description", ""), int(payload.get("enabled", True)), json.dumps(payload.get("definition") or {}, ensure_ascii=False), now, now))
        row = conn.execute("SELECT * FROM stock_picker_agent WHERE id=?", (aid,)).fetchone()
    return _decode(row)


def get_agent(user_id: int, agent_id: str) -> dict[str, Any] | None:
    init_db()
    with _conn() as conn: row = conn.execute("SELECT * FROM stock_picker_agent WHERE id=? AND user_id=?", (agent_id, user_id)).fetchone()
    return _decode(row) if row else None


def update_agent(user_id: int, agent_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    old = get_agent(user_id, agent_id)
    if not old: return None
    merged = {**old, **payload}; now = time.time()
    with _conn() as conn:
        conn.execute("UPDATE stock_picker_agent SET name=?,description=?,enabled=?,definition_json=?,updated_at=? WHERE id=? AND user_id=?", (merged["name"], merged.get("description", ""), int(merged.get("enabled", True)), json.dumps(merged.get("definition") or {}, ensure_ascii=False), now, agent_id, user_id))
    return get_agent(user_id, agent_id)


def delete_agent(user_id: int, agent_id: str) -> bool:
    with _conn() as conn: cur = conn.execute("DELETE FROM stock_picker_agent WHERE id=? AND user_id=?", (agent_id, user_id))
    return cur.rowcount > 0


def save_run(user_id: int, agent_id: str, symbols: list[str], result: dict[str, Any]) -> dict[str, Any]:
    rid = str(uuid.uuid4()); now = time.time()
    with _conn() as conn: conn.execute("INSERT INTO stock_picker_run VALUES (?,?,?,?,?,?)", (rid, agent_id, user_id, json.dumps(symbols), json.dumps(result, ensure_ascii=False), now))
    return {"id": rid, "agent_id": agent_id, "symbols": symbols, "result": result, "created_at": now}


def list_runs(user_id: int, limit: int = 30) -> list[dict[str, Any]]:
    init_db()
    with _conn() as conn: rows = conn.execute("SELECT * FROM stock_picker_run WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, max(1, min(limit, 100)))).fetchall()
    return [{**dict(r), "symbols": json.loads(r["symbols_json"]), "result": json.loads(r["result_json"])} for r in rows]
