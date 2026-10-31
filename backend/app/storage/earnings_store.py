"""Public filing versions and account-owned, explicitly requested analysis jobs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import time
import uuid
from zoneinfo import ZoneInfo

from app.config import settings
from app.storage import sqlite_bootstrap

LEASE_SECONDS = 120
_SH = ZoneInfo("Asia/Shanghai")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS earnings_reports (
 id TEXT PRIMARY KEY,symbol TEXT NOT NULL,period TEXT NOT NULL,published_at REAL NOT NULL,
 metadata TEXT NOT NULL,document_sha TEXT,first_seen REAL NOT NULL,last_seen REAL NOT NULL);
CREATE INDEX IF NOT EXISTS earnings_symbol ON earnings_reports(symbol,published_at DESC);
CREATE TABLE IF NOT EXISTS earnings_documents (sha TEXT PRIMARY KEY,metadata TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS earnings_sources (symbol TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS earnings_jobs (
 id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,symbol TEXT NOT NULL,report_id TEXT,
 kind TEXT NOT NULL,focus TEXT NOT NULL,status TEXT NOT NULL,phase TEXT NOT NULL,
 created_at REAL NOT NULL,started_at REAL,finished_at REAL,owner TEXT,lease_until REAL,
 attempts INTEGER NOT NULL DEFAULT 0,dispatched_at REAL,error TEXT,result TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS earnings_one_active ON earnings_jobs(user_id,symbol,kind,COALESCE(report_id,''))
 WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS earnings_jobs_user ON earnings_jobs(user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS earnings_calls (job_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,day TEXT NOT NULL,created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS earnings_usage ON earnings_calls(user_id,day);
CREATE TABLE IF NOT EXISTS earnings_analyses (
 id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,report_id TEXT NOT NULL,document_sha TEXT NOT NULL,
 cache_key TEXT NOT NULL,focus TEXT NOT NULL,model TEXT NOT NULL,created_at REAL NOT NULL,result TEXT NOT NULL,
 UNIQUE(user_id,cache_key));
CREATE TABLE IF NOT EXISTS earnings_analysis_links (
 user_id INTEGER NOT NULL,report_id TEXT NOT NULL,analysis_id TEXT NOT NULL,
 PRIMARY KEY(user_id,report_id,analysis_id));
CREATE TRIGGER IF NOT EXISTS earnings_analysis_immutable_update BEFORE UPDATE ON earnings_analyses
 BEGIN SELECT RAISE(ABORT,'filing analyses are immutable'); END;
CREATE TRIGGER IF NOT EXISTS earnings_analysis_immutable_delete BEFORE DELETE ON earnings_analyses
 BEGIN SELECT RAISE(ABORT,'filing analyses are immutable'); END;
"""


class EarningsConflict(ValueError):
    pass


class DailyLimit(ValueError):
    pass


class LeaseLost(RuntimeError):
    pass


def _uid(value):
    if type(value) is not int or value < 1:
        raise ValueError("账户无效")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@contextmanager
def connection():
    with sqlite_bootstrap.connection(settings.data_dir / "earnings.sqlite3", _SCHEMA) as db:
        yield db


def save_discovery(symbol, result):
    stamp = time.time()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        for raw in result.get("items", [])[:100]:
            if raw.get("symbol") != symbol:
                continue
            # A revised title, disclosure date or attachment creates a new version.
            identifier = hashlib.sha256(_json([raw["identity_key"], raw["title"], raw.get("pdf_url"),
                                              raw.get("announcement_date"), raw.get("report_period")]).encode()).hexdigest()[:32]
            metadata = {**raw, "id": identifier}
            db.execute("""INSERT INTO earnings_reports VALUES(?,?,?,?,?,NULL,?,?)
              ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""",
                       (identifier, symbol, raw.get("report_period") or "", float(raw.get("published_at") or 0),
                        _json(metadata), stamp, stamp))
        state = {"sources": result.get("sources", []), "partial": bool(result.get("partial")), "updated_at": stamp,
                 "received": len(result.get("items", []))}
        db.execute("INSERT INTO earnings_sources VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET payload=excluded.payload",
                   (symbol, _json(state)))
    return state


def source_status(symbol):
    with connection() as db:
        row = db.execute("SELECT payload FROM earnings_sources WHERE symbol=?", (symbol,)).fetchone()
        return json.loads(row[0]) if row else {"sources": [], "partial": False, "updated_at": None, "received": 0}


def _report(row):
    return {**json.loads(row["metadata"]), "document_sha": row["document_sha"],
            "first_seen_at": row["first_seen"], "last_seen_at": row["last_seen"]} if row else None


def get_report(identifier):
    with connection() as db:
        return _report(db.execute("SELECT * FROM earnings_reports WHERE id=?", (identifier,)).fetchone())


def list_reports(symbol):
    with connection() as db:
        return [_report(row) for row in db.execute("SELECT * FROM earnings_reports WHERE symbol=? ORDER BY published_at DESC,id LIMIT 120", (symbol,))]


def save_document(report_id, sha, metadata):
    if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
        raise ValueError("财报文件校验失败")
    public = {key: value for key, value in metadata.items() if key != "pages"}
    public["sha256"] = sha
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO earnings_documents VALUES(?,?,?) ON CONFLICT(sha) DO UPDATE SET metadata=excluded.metadata", (sha, _json(public), time.time()))
        changed = db.execute("UPDATE earnings_reports SET document_sha=? WHERE id=?", (sha, report_id))
        if not changed.rowcount:
            raise ValueError("财报不存在")
    return public


def get_document(sha):
    if not sha:
        return None
    with connection() as db:
        row = db.execute("SELECT metadata FROM earnings_documents WHERE sha=?", (sha,)).fetchone()
        return json.loads(row[0]) if row else None


def _job(row):
    if row is None:
        return None
    value = dict(row)
    value["result"] = json.loads(value["result"]) if value.get("result") else None
    return value


def get_job(uid, identifier):
    with connection() as db:
        return _job(db.execute("SELECT * FROM earnings_jobs WHERE user_id=? AND id=?", (_uid(uid), identifier)).fetchone())


def list_jobs(uid, *, symbol=None, report_id=None):
    with connection() as db:
        return [_job(row) for row in db.execute("""SELECT * FROM earnings_jobs WHERE user_id=?
          AND (? IS NULL OR symbol=?) AND (? IS NULL OR report_id=?) ORDER BY created_at DESC,rowid DESC LIMIT 20""",
                                               (_uid(uid), symbol, symbol, report_id, report_id))]


def enqueue(uid, symbol, kind, *, report_id=None, focus=""):
    uid = _uid(uid)
    if kind not in {"refresh", "prepare", "analyze"} or not isinstance(focus, str) or len(focus) > 2000:
        raise ValueError("财报任务参数无效")
    stamp = time.time()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if kind != "refresh":
            row = db.execute("SELECT symbol FROM earnings_reports WHERE id=?", (report_id,)).fetchone()
            if row is None or row[0] != symbol:
                raise ValueError("财报不存在或与股票不匹配")
        active = db.execute("""SELECT * FROM earnings_jobs WHERE user_id=? AND symbol=? AND kind=?
          AND COALESCE(report_id,'')=? AND status IN ('queued','running')""", (uid, symbol, kind, report_id or "")).fetchone()
        if active:
            if active["focus"] != focus.strip():
                raise EarningsConflict("当前财报正在解读，请完成后再提交不同的关注问题")
            return {**_job(active), "deduplicated": True}
        if kind == "refresh":
            recent = db.execute("SELECT created_at FROM earnings_jobs WHERE symbol=? AND kind='refresh' ORDER BY created_at DESC LIMIT 1", (symbol,)).fetchone()
            if recent and stamp - recent[0] < 60:
                raise EarningsConflict("该股票刚刚查询过财报，请一分钟后重试")
        if db.execute("SELECT COUNT(*) FROM earnings_jobs WHERE user_id=? AND status IN ('queued','running')", (uid,)).fetchone()[0] >= 10:
            raise EarningsConflict("当前账户待处理财报任务较多，请完成后再提交")
        if db.execute("SELECT COUNT(*) FROM earnings_jobs WHERE status IN ('queued','running')").fetchone()[0] >= 1000:
            raise EarningsConflict("财报队列已满，请稍后重试")
        identifier = uuid.uuid4().hex
        db.execute("""INSERT INTO earnings_jobs(id,user_id,symbol,report_id,kind,focus,status,phase,created_at)
          VALUES(?,?,?,?,?,?,'queued','queued',?)""", (identifier, uid, symbol, report_id, kind, focus.strip(), stamp))
        return _job(db.execute("SELECT * FROM earnings_jobs WHERE id=?", (identifier,)).fetchone())


def recover_interrupted():
    """Only call under the OS worker lock. Never repeat a dispatched model call."""
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        for row in db.execute("SELECT * FROM earnings_jobs WHERE status='running'").fetchall():
            if row["dispatched_at"] is not None or row["attempts"] >= 3:
                message = "服务中断，模型请求结果未知，未重复调用；请检查历史后手动重试" if row["dispatched_at"] else "任务多次中断，请手动重试"
                db.execute("UPDATE earnings_jobs SET status='failed',phase='failed',finished_at=?,error=?,owner=NULL,lease_until=NULL WHERE id=?",
                           (time.time(), message, row["id"]))
            else:
                db.execute("UPDATE earnings_jobs SET status='queued',phase='queued',owner=NULL,lease_until=NULL WHERE id=?", (row["id"],))


def claim(owner):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM earnings_jobs WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
        if row is None:
            return None
        stamp = time.time()
        db.execute("UPDATE earnings_jobs SET status='running',phase='starting',started_at=?,owner=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                   (stamp, owner, stamp + LEASE_SECONDS, row["id"]))
        return _job(db.execute("SELECT * FROM earnings_jobs WHERE id=?", (row["id"],)).fetchone())


def _leased(db, uid, identifier, owner):
    row = db.execute("SELECT * FROM earnings_jobs WHERE user_id=? AND id=? AND owner=? AND status='running' AND lease_until>?",
                     (_uid(uid), identifier, owner, time.time())).fetchone()
    if row is None:
        raise LeaseLost("财报任务处理权已失效")
    return row


def heartbeat(uid, identifier, owner, *, phase=None):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        _leased(db, uid, identifier, owner)
        db.execute("UPDATE earnings_jobs SET lease_until=?,phase=COALESCE(?,phase) WHERE id=?",
                   (time.time() + LEASE_SECONDS, phase, identifier))


def _day():
    return datetime.now(_SH).date().isoformat()


def usage(uid):
    with connection() as db:
        used = db.execute("SELECT COUNT(*) FROM earnings_calls WHERE user_id=? AND day=?", (_uid(uid), _day())).fetchone()[0]
    limit = settings.earnings_daily_call_limit
    return {"used": used, "limit": limit, "remaining": max(0, limit - used), "day": _day(), "timezone": "Asia/Shanghai"}


def reserve_call(uid, identifier, owner):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _leased(db, uid, identifier, owner)
        if row["kind"] != "analyze" or row["dispatched_at"] is not None:
            raise EarningsConflict("财报模型请求已发送，不能重复发送")
        used = db.execute("SELECT COUNT(*) FROM earnings_calls WHERE user_id=? AND day=?", (uid, _day())).fetchone()[0]
        if used >= settings.earnings_daily_call_limit:
            raise DailyLimit("今日财报模型请求额度已用完，请明日再试")
        stamp = time.time()
        db.execute("INSERT INTO earnings_calls VALUES(?,?,?,?)", (identifier, uid, _day(), stamp))
        db.execute("UPDATE earnings_jobs SET dispatched_at=? WHERE id=?", (stamp, identifier))


def _analysis(row):
    if row is None:
        return None
    value = dict(row)
    value["result"] = json.loads(value["result"])
    return value


def cached_analysis(uid, report_id, cache_key):
    with connection() as db:
        return _analysis(db.execute("SELECT * FROM earnings_analyses WHERE user_id=? AND cache_key=?",
                                    (_uid(uid), cache_key)).fetchone())


def list_analyses(uid, report_id):
    with connection() as db:
        return [_analysis(row) for row in db.execute("""SELECT a.* FROM earnings_analyses a
          JOIN earnings_analysis_links l ON l.user_id=a.user_id AND l.analysis_id=a.id
          WHERE l.user_id=? AND l.report_id=? ORDER BY a.created_at DESC LIMIT 30""",
                                                     (_uid(uid), report_id))]


def finish(uid, identifier, owner, *, result=None, error=None, analysis=None):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _leased(db, uid, identifier, owner)
        stamp = time.time()
        if analysis:
            aid = uuid.uuid4().hex
            db.execute("INSERT INTO earnings_analyses VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,cache_key) DO NOTHING",
                       (aid, uid, row["report_id"], analysis["document_sha"], analysis["cache_key"], row["focus"],
                        analysis["result"]["model"], stamp, _json(analysis["result"])))
            saved = db.execute("SELECT id FROM earnings_analyses WHERE user_id=? AND cache_key=?",
                               (uid, analysis["cache_key"])).fetchone()
            result = {"analysis_id": saved[0], "reused": False}
        if result and result.get("analysis_id"):
            saved = db.execute("SELECT document_sha FROM earnings_analyses WHERE user_id=? AND id=?", (uid, result["analysis_id"])).fetchone()
            report = db.execute("SELECT document_sha FROM earnings_reports WHERE id=?", (row["report_id"],)).fetchone()
            if saved is None or report is None or report[0] != saved[0]:
                raise ValueError("财报解读与当前文件不一致")
            db.execute("INSERT OR IGNORE INTO earnings_analysis_links VALUES(?,?,?)", (uid, row["report_id"], result["analysis_id"]))
        status = "failed" if error else "succeeded"
        db.execute("UPDATE earnings_jobs SET status=?,phase=?,finished_at=?,error=?,result=?,owner=NULL,lease_until=NULL WHERE id=?",
                   (status, status, stamp, str(error)[:500] if error else None, _json(result or {}), identifier))
        return _job(db.execute("SELECT * FROM earnings_jobs WHERE id=?", (identifier,)).fetchone())
