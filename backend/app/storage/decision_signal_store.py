"""可审计决策信号与后验结果（按用户隔离）。"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from app.config import settings


def _conn() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.data_dir / "decision_signals.db")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS decision_signal (
              id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, symbol TEXT NOT NULL,
              action TEXT NOT NULL, horizon TEXT NOT NULL, confidence REAL NOT NULL,
              score REAL NOT NULL, reason TEXT NOT NULL, risks_json TEXT NOT NULL,
              watch_json TEXT NOT NULL, evidence_json TEXT NOT NULL, source_type TEXT NOT NULL,
              source_ref TEXT, source_meta_json TEXT NOT NULL, status TEXT NOT NULL,
              created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signal_user_time ON decision_signal(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_signal_user_symbol ON decision_signal(user_id, symbol, created_at DESC);
            CREATE TABLE IF NOT EXISTS decision_signal_outcome (
              signal_id TEXT NOT NULL, horizon_days INTEGER NOT NULL, engine_version TEXT NOT NULL,
              eval_status TEXT NOT NULL, base_price REAL, end_price REAL, return_pct REAL,
              direction_correct INTEGER, unable_reason TEXT, evaluated_at REAL NOT NULL,
              PRIMARY KEY(signal_id, horizon_days, engine_version)
            );
            """
        )


def create(user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    init_db()
    now = time.time()
    sid = str(uuid.uuid4())
    row = {
        "id": sid, "user_id": int(user_id), "symbol": str(payload["symbol"]).upper(),
        "action": payload.get("action") or "watch", "horizon": payload.get("horizon") or "swing",
        "confidence": float(payload.get("confidence") or 0), "score": float(payload.get("score") or 50),
        "reason": str(payload.get("reason") or "")[:4000], "risks": payload.get("risks") or [],
        "watch_conditions": payload.get("watch_conditions") or [], "evidence": payload.get("evidence") or [],
        "source_type": payload.get("source_type") or "manual", "source_ref": payload.get("source_ref"),
        "source_meta": payload.get("source_meta") or {}, "status": "active", "created_at": now, "updated_at": now,
    }
    with _conn() as conn:
        conn.execute(
            "INSERT INTO decision_signal VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, row["user_id"], row["symbol"], row["action"], row["horizon"], row["confidence"], row["score"],
             row["reason"], json.dumps(row["risks"], ensure_ascii=False), json.dumps(row["watch_conditions"], ensure_ascii=False),
             json.dumps(row["evidence"], ensure_ascii=False), row["source_type"], row["source_ref"],
             json.dumps(row["source_meta"], ensure_ascii=False), row["status"], now, now),
        )
    return row


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for source, target in (("risks_json", "risks"), ("watch_json", "watch_conditions"), ("evidence_json", "evidence"), ("source_meta_json", "source_meta")):
        try: out[target] = json.loads(out.pop(source) or "[]")
        except json.JSONDecodeError: out[target] = [] if target != "source_meta" else {}
    return out


def list_signals(user_id: int, *, symbol: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    clauses, args = ["user_id=?"], [int(user_id)]
    if symbol: clauses.append("symbol=?"); args.append(symbol.upper())
    if status: clauses.append("status=?"); args.append(status)
    args.append(max(1, min(limit, 200)))
    with _conn() as conn:
        rows = conn.execute(f"SELECT * FROM decision_signal WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?", args).fetchall()
        outcomes = conn.execute("SELECT * FROM decision_signal_outcome WHERE signal_id IN (%s) ORDER BY horizon_days" % (",".join("?" * len(rows))), [r["id"] for r in rows]).fetchall() if rows else []
    by_id: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes: by_id.setdefault(outcome["signal_id"], []).append(dict(outcome))
    result = []
    for row in rows:
        item = _decode(row); item["outcomes"] = by_id.get(item["id"], []); result.append(item)
    return result


def update_status(user_id: int, signal_id: str, status: str) -> bool:
    if status not in {"active", "closed", "invalidated", "archived"}: raise ValueError("无效信号状态")
    with _conn() as conn:
        cur = conn.execute("UPDATE decision_signal SET status=?, updated_at=? WHERE id=? AND user_id=?", (status, time.time(), signal_id, int(user_id)))
    return cur.rowcount > 0


def upsert_outcome(signal_id: str, payload: dict[str, Any]) -> None:
    with _conn() as conn:
        conn.execute("""INSERT OR REPLACE INTO decision_signal_outcome
          (signal_id,horizon_days,engine_version,eval_status,base_price,end_price,return_pct,direction_correct,unable_reason,evaluated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?)""", (signal_id, payload["horizon_days"], "decision-signal-v1", payload["eval_status"],
          payload.get("base_price"), payload.get("end_price"), payload.get("return_pct"), payload.get("direction_correct"),
          payload.get("unable_reason"), time.time()))
