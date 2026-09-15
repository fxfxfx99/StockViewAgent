"""量化交易：模拟账户与委托记录（SQLite：backend/data/quant.db）。"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import settings

_DEFAULT_CASH = 1_000_000.0


def _db_path() -> Path:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / "quant.db"


def init_db() -> None:
    p = _db_path()
    with sqlite3.connect(p) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_account (
                user_id INTEGER PRIMARY KEY,
                cash REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_position (
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                volume INTEGER NOT NULL,
                avg_price REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, symbol)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quant_order (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                volume INTEGER NOT NULL,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                external_ref TEXT,
                detail TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_quant_order_user ON quant_order(user_id, created_at DESC)")


def _now() -> float:
    return time.time()


def get_or_create_account(user_id: int) -> dict[str, Any]:
    init_db()
    p = _db_path()
    with sqlite3.connect(p) as conn:
        row = conn.execute("SELECT cash FROM paper_account WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            cash = float(row[0])
        else:
            cash = _DEFAULT_CASH
            conn.execute(
                "INSERT INTO paper_account (user_id, cash, updated_at) VALUES (?, ?, ?)",
                (user_id, cash, _now()),
            )
            conn.commit()
        pos_rows = conn.execute(
            "SELECT symbol, volume, avg_price FROM paper_position WHERE user_id = ? ORDER BY symbol",
            (user_id,),
        ).fetchall()
    positions = [
        {"symbol": r[0], "volume": int(r[1]), "avg_price": float(r[2])} for r in (pos_rows or [])
    ]
    return {"cash": cash, "initial_cash_default": _DEFAULT_CASH, "positions": positions}


def list_orders(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    p = _db_path()
    limit = max(1, min(200, limit))
    with sqlite3.connect(p) as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, side, price, volume, status, mode, external_ref, detail, created_at
            FROM quant_order WHERE user_id = ? ORDER BY created_at DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    out = []
    for r in rows or []:
        raw_detail = r[8]
        detail_parsed = None
        if raw_detail:
            try:
                detail_parsed = json.loads(raw_detail) if raw_detail.startswith("{") else raw_detail
            except json.JSONDecodeError:
                detail_parsed = raw_detail
        out.append(
            {
                "id": r[0],
                "symbol": r[1],
                "side": r[2],
                "price": float(r[3]),
                "volume": int(r[4]),
                "status": r[5],
                "mode": r[6],
                "external_ref": r[7],
                "detail": detail_parsed,
                "created_at": float(r[9]),
            }
        )
    return out


def paper_place_order(
    user_id: int,
    *,
    symbol: str,
    side: str,
    price: float,
    volume: int,
    commission_rate: float,
) -> dict[str, Any]:
    """模拟限价立即成交；A 股按 100 股一手校验。"""
    sym = symbol.strip().upper()
    side_l = side.strip().lower()
    if side_l not in ("buy", "sell"):
        raise ValueError("side 须为 buy 或 sell")
    if volume < 100 or volume % 100 != 0:
        raise ValueError("数量须为 100 的整数倍")
    if price <= 0:
        raise ValueError("价格须为正数")

    init_db()
    p = _db_path()
    oid = str(uuid.uuid4())
    ts = _now()

    with sqlite3.connect(p) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT cash FROM paper_account WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO paper_account (user_id, cash, updated_at) VALUES (?, ?, ?)",
                (user_id, _DEFAULT_CASH, ts),
            )
            cash = _DEFAULT_CASH
        else:
            cash = float(row[0])

        pos = conn.execute(
            "SELECT volume, avg_price FROM paper_position WHERE user_id = ? AND symbol = ?",
            (user_id, sym),
        ).fetchone()
        held = int(pos[0]) if pos else 0
        avg_px = float(pos[1]) if pos else 0.0

        gross = price * volume
        comm = gross * commission_rate

        if side_l == "buy":
            need = gross + comm
            if cash < need:
                conn.rollback()
                raise ValueError(f"可用资金不足：需要 {need:.2f}，当前 {cash:.2f}")
            new_cash = cash - need
            new_vol = held + volume
            new_avg = (avg_px * held + price * volume) / new_vol if new_vol else 0.0
            conn.execute(
                "UPDATE paper_account SET cash = ?, updated_at = ? WHERE user_id = ?",
                (new_cash, ts, user_id),
            )
            conn.execute(
                """
                INSERT INTO paper_position (user_id, symbol, volume, avg_price, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, symbol) DO UPDATE SET
                    volume = excluded.volume,
                    avg_price = excluded.avg_price,
                    updated_at = excluded.updated_at
                """,
                (user_id, sym, new_vol, new_avg, ts),
            )
        else:
            if held < volume:
                conn.rollback()
                raise ValueError(f"持仓不足：当前 {held} 股，卖出 {volume}")
            proceeds = gross - comm
            new_cash = cash + proceeds
            new_vol = held - volume
            conn.execute(
                "UPDATE paper_account SET cash = ?, updated_at = ? WHERE user_id = ?",
                (new_cash, ts, user_id),
            )
            if new_vol <= 0:
                conn.execute(
                    "DELETE FROM paper_position WHERE user_id = ? AND symbol = ?",
                    (user_id, sym),
                )
            else:
                conn.execute(
                    "UPDATE paper_position SET volume = ?, updated_at = ? WHERE user_id = ? AND symbol = ?",
                    (new_vol, ts, user_id, sym),
                )

        detail = {"commission": round(comm, 4), "gross": round(gross, 4)}
        conn.execute(
            """
            INSERT INTO quant_order (id, user_id, symbol, side, price, volume, status, mode, external_ref, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'filled', 'paper', NULL, ?, ?)
            """,
            (oid, user_id, sym, side_l, price, volume, json.dumps(detail, ensure_ascii=False), ts),
        )
        conn.commit()

    return {
        "order_id": oid,
        "symbol": sym,
        "side": side_l,
        "price": price,
        "volume": volume,
        "status": "filled",
        "mode": "paper",
        "detail": detail,
    }


def insert_live_order_record(
    user_id: int,
    *,
    symbol: str,
    side: str,
    price: float,
    volume: int,
    status: str,
    external_ref: str | None,
    detail: dict[str, Any] | None,
) -> str:
    init_db()
    oid = str(uuid.uuid4())
    ts = _now()
    djson = json.dumps(detail, ensure_ascii=False) if detail else None
    p = _db_path()
    with sqlite3.connect(p) as conn:
        conn.execute(
            """
            INSERT INTO quant_order (id, user_id, symbol, side, price, volume, status, mode, external_ref, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'live', ?, ?, ?)
            """,
            (
                oid,
                user_id,
                symbol.strip().upper(),
                side.lower(),
                price,
                volume,
                status,
                external_ref,
                djson,
                ts,
            ),
        )
        conn.commit()
    return oid


def reset_paper_account(user_id: int, cash: float = _DEFAULT_CASH) -> None:
    """清空持仓并将现金恢复为默认值（仅管理端调用）。"""
    init_db()
    ts = _now()
    p = _db_path()
    with sqlite3.connect(p) as conn:
        conn.execute("DELETE FROM paper_position WHERE user_id = ?", (user_id,))
        conn.execute(
            """
            INSERT INTO paper_account (user_id, cash, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET cash = excluded.cash, updated_at = excluded.updated_at
            """,
            (user_id, float(cash), ts),
        )
        conn.commit()
