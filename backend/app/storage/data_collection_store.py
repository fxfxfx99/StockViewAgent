"""Shared collection queue and immutable, locally observed public datasets."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from urllib.parse import parse_qsl, urlsplit

from app.config import settings
from app.storage import sqlite_bootstrap
from app.storage.event_calendar_store import SHANGHAI, tokens


_SECRET_KEYS = {"apikey", "accesstoken", "refreshtoken", "token", "password", "passwd", "authorization",
                "cookie", "setcookie", "clientsecret", "secret", "credentials", "privatekey", "secretkey"}
_MAX_BATCH_BYTES = 16 * 1024 * 1024
_ENCODER = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _encoded_chunks(value):
    for chunk in _ENCODER.iterencode(value):
        yield chunk.encode("utf-8")


def _file_digest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.digest()


def _secret_key(key):
    normalised = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalised in _SECRET_KEYS or any(normalised.endswith(suffix) for suffix in ("apikey", "accesstoken", "password", "clientsecret", "privatekey"))


def _identifier(value, *, category=False):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0," + ("40" if category else "60") + "}", value):
        raise ValueError("无效数据分类" if category else "无效来源标识")
    return value


def _stamp(value=None):
    if isinstance(value, bool):
        raise ValueError("无效时点")
    try:
        result = time.time() if value is None else float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError
        datetime.fromtimestamp(result, timezone.utc)
        return result
    except (TypeError, ValueError, OverflowError, OSError):
        raise ValueError("无效时点") from None


def _public_url(value, *, allow_http=False):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("来源链接无效")
    try:
        parts = urlsplit(value)
        schemes = {"https", "http"} if allow_http else {"https"}
        ports = (None, 443, 80) if allow_http else (None, 443)
        if parts.scheme not in schemes or not parts.hostname or parts.username or parts.password or parts.port not in ports:
            raise ValueError
        if any(_secret_key(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True) + parse_qsl(parts.fragment, keep_blank_values=True)):
            raise ValueError
    except ValueError:
        raise ValueError("来源链接包含凭据或格式无效") from None
    return value


def _clean_public_json(value, depth=0):
    """Keep public metadata, never persist common credential fields or URLs."""
    if depth > 12:
        raise ValueError("元数据层级过深")
    if isinstance(value, dict):
        if len(value) > 2000 or any(not isinstance(key, str) for key in value):
            raise ValueError("元数据字段无效")
        return {key: _clean_public_json(item, depth + 1) for key, item in value.items() if not _secret_key(key)}
    if isinstance(value, list):
        if len(value) > 10000:
            raise ValueError("元数据列表过大")
        return [_clean_public_json(item, depth + 1) for item in value]
    if isinstance(value, str):
        if len(value) > 12000:
            raise ValueError("元数据文本过长")
        if value.startswith(("https://", "http://")):
            try:
                _public_url(value, allow_http=True)
            except ValueError:
                return "[redacted_url]"
        return re.sub(r"(?i)\b(api[_-]?key|access[_-]?token|authorization|password|client[_-]?secret)\s*[:=]\s*(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|\S+)",
                      lambda match: match.group(1) + "=[redacted]", value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("元数据必须为有限 JSON 值")


def _public_payload(value):
    cleaned = _clean_public_json(value)
    if len(canonical(cleaned)) > 50000:
        raise ValueError("持久化数据过大")
    return canonical(cleaned)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def iso(stamp=None):
    return datetime.fromtimestamp(time.time() if stamp is None else stamp, timezone.utc).isoformat()


_SCHEMA_SQL = """
          CREATE TABLE IF NOT EXISTS dc_settings(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS dc_jobs(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,status TEXT NOT NULL,
            queued_at REAL NOT NULL,started_at REAL,finished_at REAL,records_received INTEGER DEFAULT 0,
            records_added INTEGER DEFAULT 0,error_code TEXT,payload TEXT NOT NULL);
          CREATE UNIQUE INDEX IF NOT EXISTS dc_active_source ON dc_jobs(source_id) WHERE status IN ('queued','running');
          CREATE TABLE IF NOT EXISTS dc_attempts(id INTEGER PRIMARY KEY,job_id TEXT,day TEXT,started_at REAL);
          CREATE INDEX IF NOT EXISTS dc_attempt_day ON dc_attempts(day);
          CREATE TABLE IF NOT EXISTS dc_rounds(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,window_key TEXT NOT NULL,
            scheduled_for REAL NOT NULL,status TEXT NOT NULL,symbols TEXT NOT NULL,completed TEXT NOT NULL,
            created_at REAL NOT NULL,finished_at REAL,UNIQUE(source_id,window_key));
          CREATE UNIQUE INDEX IF NOT EXISTS dc_one_round ON dc_rounds(source_id) WHERE status='active';
          CREATE TABLE IF NOT EXISTS dc_sources(source_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS dc_batches(id TEXT PRIMARY KEY,source_id TEXT,content_hash TEXT,path TEXT,
            observed_at REAL,records INTEGER,size_bytes INTEGER);
          CREATE TABLE IF NOT EXISTS dc_revisions(seq INTEGER PRIMARY KEY,revision_id TEXT UNIQUE NOT NULL,
            record_id TEXT NOT NULL,source_id TEXT NOT NULL,batch_id TEXT NOT NULL,observed_at REAL NOT NULL,
            content_hash TEXT NOT NULL,payload TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS dc_observation ON dc_revisions(record_id,observed_at,seq);
          CREATE TABLE IF NOT EXISTS dc_heads(record_id TEXT PRIMARY KEY,revision_id TEXT,content_hash TEXT,last_seen_at REAL);
          CREATE VIRTUAL TABLE IF NOT EXISTS dc_chunks USING fts5(revision_id UNINDEXED,text,terms);
          CREATE TRIGGER IF NOT EXISTS dc_no_revision_update BEFORE UPDATE ON dc_revisions
            BEGIN SELECT RAISE(ABORT,'dataset revisions are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS dc_no_revision_delete BEFORE DELETE ON dc_revisions
            BEGIN SELECT RAISE(ABORT,'dataset revisions are immutable'); END;
        """


@contextmanager
def connection():
    path = settings.data_dir / "data_center.sqlite3"
    with sqlite_bootstrap.connection(path, _SCHEMA_SQL, version=2) as conn:
        yield conn


def get_state(key, default=None):
    with connection() as conn:
        row = conn.execute("SELECT payload FROM dc_settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_state(key, value):
    if _secret_key(key):
        raise ValueError("数据中心状态不保存凭据")
    payload = _public_payload(value)
    with connection() as conn:
        conn.execute("INSERT INTO dc_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, payload))


def mutate_state(key, function, default=None):
    if _secret_key(key):
        raise ValueError("数据中心状态不保存凭据")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM dc_settings WHERE key=?", (key,)).fetchone()
        value = _clean_public_json(function(json.loads(row[0]) if row else default))
        conn.execute("INSERT INTO dc_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, _public_payload(value)))
    return value


def source_states():
    with connection() as conn:
        return {row[0]: json.loads(row[1]) for row in conn.execute("SELECT * FROM dc_sources")}


def block_source(source_id, error_code):
    source_id = _identifier(source_id)
    if error_code != "symbol_pool_too_large":
        raise ValueError("无效暂停原因")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM dc_sources WHERE source_id=?", (source_id,)).fetchone()
        state = json.loads(row[0]) if row else {}
        state.update(status="blocked", error_code=error_code)
        conn.execute("INSERT INTO dc_sources VALUES(?,?) ON CONFLICT(source_id) DO UPDATE SET payload=excluded.payload",
                     (source_id, canonical(state)))


def retry_delay(failures, error_code=None):
    delay = min(21600, 60 * 2 ** min(max(0, int(failures)), 9))
    if error_code in {"http_401", "http_403"}:
        delay = max(1800, delay)
    elif error_code == "http_429":
        delay = max(300, delay)
    return delay


def _retry_at(conn, source_id):
    row = conn.execute("SELECT payload FROM dc_sources WHERE source_id=?", (source_id,)).fetchone()
    value = json.loads(row[0]) if row else {}
    if value.get("status") not in {"failed", "partial"} and not (value.get("status") == "skipped" and value.get("retry_at")):
        return 0
    if value.get("retry_at"):
        return datetime.fromisoformat(value["retry_at"]).timestamp()
    last = value.get("last_finished_at") or value.get("last_attempt_at")
    return datetime.fromisoformat(last).timestamp() + retry_delay(value.get("failures", 0), value.get("error_code")) if last else 0


def round_states():
    with connection() as conn:
        rows = conn.execute("SELECT * FROM dc_rounds r WHERE r.rowid=(SELECT MAX(x.rowid) FROM dc_rounds x WHERE x.source_id=r.source_id)")
        return {row["source_id"]: {"round_id": row["id"], "round_status": row["status"],
                "last_scheduled_for": iso(row["scheduled_for"]), "total_symbols": len(json.loads(row["symbols"])),
                "completed_symbols": len(json.loads(row["completed"])),
                "pending_symbols": len(json.loads(row["symbols"]))-len(json.loads(row["completed"]))} for row in rows}


def active_round_symbols(source_id):
    with connection() as conn:
        row = conn.execute("SELECT symbols FROM dc_rounds WHERE source_id=? AND status='active'", (source_id,)).fetchone()
        return json.loads(row[0]) if row else []


def active_rounds():
    with connection() as conn:
        return [{"source_id": row["source_id"], "window_key": row["window_key"], "scheduled_for": row["scheduled_for"],
                 "symbols": json.loads(row["symbols"]), "scheduled": not bool(row["manual"])}
                for row in conn.execute("SELECT r.*,s.payload AS manual FROM dc_rounds r LEFT JOIN dc_settings s ON s.key='round_manual:'||r.id WHERE r.status='active'")]


class CollectionConflict(ValueError):
    pass


def _job(row):
    data = dict(row)
    data["payload"] = json.loads(data["payload"])
    for key in ("queued_at", "started_at", "finished_at"):
        data[key] = iso(data[key]) if data[key] is not None else None
    return data


def enqueue(source_id, payload=None, *, now=None, minimum_interval=60):
    source_id, stamp = _identifier(source_id), _stamp(now)
    if isinstance(minimum_interval, bool) or not isinstance(minimum_interval, (int, float)) or not math.isfinite(minimum_interval) or minimum_interval < 0:
        raise ValueError("无效任务间隔")
    if payload is not None and not isinstance(payload, dict):
        raise ValueError("任务参数必须为对象")
    clean_payload = _public_payload(payload or {})
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute("SELECT * FROM dc_jobs WHERE source_id=? AND status IN ('queued','running')", (source_id,)).fetchone()
        if active:
            previous = json.loads(active["payload"])
            incoming = payload or {}
            if incoming.get("explicit_news") and (not previous.get("explicit_news")
                    or previous.get("news_symbols") != incoming.get("news_symbols")
                    or previous.get("news_lookback_days") != incoming.get("news_lookback_days")):
                raise CollectionConflict("新闻来源已有不同范围的任务，请完成后重试")
            if active["status"] == "queued" and payload is not None and payload.get("scheduled") is False:
                prior = json.loads(active["payload"])
                if prior.get("scheduled") is True:
                    prior["scheduled"] = False
                    conn.execute("UPDATE dc_jobs SET payload=? WHERE id=?", (_public_payload(prior), active["id"]))
                    active = conn.execute("SELECT * FROM dc_jobs WHERE id=?", (active["id"],)).fetchone()
            return {**_job(active), "deduplicated": True}
        retry_at = _retry_at(conn, source_id)
        if retry_at > stamp:
            return {"source_id": source_id, "status": "cooldown", "retry_after": max(1, math.ceil(retry_at-stamp)), "retry_at": iso(retry_at)}
        window = (payload or {}).get("window_key")
        if window and conn.execute("SELECT 1 FROM dc_jobs WHERE source_id=? AND json_extract(payload,'$.window_key')=? AND status='success' LIMIT 1", (source_id, window)).fetchone():
            return {"source_id": source_id, "status": "completed", "deduplicated": True}
        last = conn.execute("SELECT MAX(started_at) FROM dc_jobs WHERE source_id=?", (source_id,)).fetchone()[0]
        if last is not None and stamp - last < minimum_interval:
            return {"source_id": source_id, "status": "cooldown", "retry_after": max(1, math.ceil(minimum_interval - stamp + last))}
        identifier = uuid.uuid4().hex
        conn.execute("INSERT INTO dc_jobs(id,source_id,status,queued_at,payload) VALUES(?,?,'queued',?,?)",
                     (identifier, source_id, stamp, clean_payload))
        return _job(conn.execute("SELECT * FROM dc_jobs WHERE id=?", (identifier,)).fetchone())


def enqueue_round(source_id, symbols, *, window_key, scheduled_for, scheduled, batch_size, now=None):
    """Freeze a whole pool once, then submit one bounded batch at a time."""
    source_id, stamp = _identifier(source_id), _stamp(now)
    if (not isinstance(symbols, list) or not symbols or len(symbols) > 3000
            or any(not isinstance(s, str) or not re.fullmatch(r"\d{6}\.(SS|SZ|BJ)", s) for s in symbols)):
        raise ValueError("股票池须含1–3000个规范代码")
    if type(batch_size) is not int or not 1 <= batch_size <= 30 or type(scheduled) is not bool:
        raise ValueError("无效股票批次参数")
    if not isinstance(window_key, str) or not 1 <= len(window_key) <= 120:
        raise ValueError("无效采集窗口")
    scheduled_for = _stamp(scheduled_for)
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute("SELECT * FROM dc_jobs WHERE source_id=? AND status IN ('queued','running')", (source_id,)).fetchone()
        if active:
            if not scheduled and active["status"] == "queued":
                payload = json.loads(active["payload"])
                payload["scheduled"] = False
                if payload.get("round_id"):
                    conn.execute("INSERT OR REPLACE INTO dc_settings VALUES(?, 'true')", ("round_manual:"+payload["round_id"],))
                conn.execute("UPDATE dc_jobs SET payload=? WHERE id=?", (_public_payload(payload), active["id"]))
                active = conn.execute("SELECT * FROM dc_jobs WHERE id=?", (active["id"],)).fetchone()
            return {**_job(active), "deduplicated": True}
        retry_at = _retry_at(conn, source_id)
        if retry_at > stamp:
            return {"source_id": source_id, "status": "cooldown", "retry_after": math.ceil(retry_at-stamp), "retry_at": iso(retry_at)}
        round_row = conn.execute("SELECT * FROM dc_rounds WHERE source_id=? AND status='active'", (source_id,)).fetchone()
        if round_row is None:
            prior = conn.execute("SELECT * FROM dc_rounds WHERE source_id=? AND window_key=?", (source_id, window_key)).fetchone()
            if prior:
                return {"source_id": source_id, "status": "completed", "deduplicated": True}
            identifier = uuid.uuid4().hex
            conn.execute("INSERT INTO dc_rounds VALUES(?,?,?,?,'active',?,'[]',?,NULL)",
                         (identifier, source_id, window_key, scheduled_for, canonical(list(dict.fromkeys(symbols))), stamp))
            round_row = conn.execute("SELECT * FROM dc_rounds WHERE id=?", (identifier,)).fetchone()
        if not scheduled:
            conn.execute("INSERT OR REPLACE INTO dc_settings VALUES(?, 'true')", ("round_manual:"+round_row["id"],))
        completed = set(json.loads(round_row["completed"]))
        batch = [symbol for symbol in json.loads(round_row["symbols"]) if symbol not in completed][:batch_size]
        payload = {"scheduled": scheduled, "round_id": round_row["id"], "window_key": round_row["window_key"],
                   "scheduled_for": iso(round_row["scheduled_for"]), "symbols": batch}
        identifier = uuid.uuid4().hex
        conn.execute("INSERT INTO dc_jobs(id,source_id,status,queued_at,payload) VALUES(?,?,'queued',?,?)", (identifier, source_id, stamp, _public_payload(payload)))
        return _job(conn.execute("SELECT * FROM dc_jobs WHERE id=?", (identifier,)).fetchone())


def pending_job_symbols(identifier):
    with connection() as conn:
        row = conn.execute("SELECT payload FROM dc_jobs WHERE id=?", (identifier,)).fetchone()
        payload = json.loads(row[0]) if row else {}
        round_row = conn.execute("SELECT completed FROM dc_rounds WHERE id=?", (payload.get("round_id"),)).fetchone()
        completed = set(json.loads(round_row[0])) if round_row else set()
        return [symbol for symbol in payload.get("symbols", []) if symbol not in completed]


def checkpoint_symbol(identifier, symbol):
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT payload FROM dc_jobs WHERE id=? AND status='running'", (identifier,)).fetchone()
        if job is None:
            raise ValueError("采集任务不在运行状态")
        payload = json.loads(job[0])
        if symbol not in payload.get("symbols", []):
            raise ValueError("股票不属于此冻结批次")
        row = conn.execute("SELECT * FROM dc_rounds WHERE id=? AND status='active'", (payload.get("round_id"),)).fetchone()
        if row is None:
            raise ValueError("采集轮次不可更新")
        completed = set(json.loads(row["completed"]))
        completed.add(symbol)
        conn.execute("UPDATE dc_rounds SET completed=? WHERE id=?", (canonical(sorted(completed)), row["id"]))


def recover_interrupted():
    # Caller owns the cross-process worker lease: no live worker can be reset.
    with connection() as conn:
        conn.execute("UPDATE dc_jobs SET status='queued',error_code='interrupted' WHERE status='running'")


def cancel_disallowed_scheduled(allowed_source_ids, *, now=None):
    """Cancel only queued automatic work after its configuration is disabled.

    The worker holds its exclusive lease while calling this; cancellation never
    consumes a collection attempt or changes the publisher's health history.
    """
    stamp = _stamp(now)
    allowed = {_identifier(source_id) for source_id in allowed_source_ids}
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT id,source_id FROM dc_jobs WHERE status='queued' AND json_type(payload,'$.scheduled')='true'").fetchall()
        identifiers = [row["id"] for row in rows if row["source_id"] not in allowed]
        conn.executemany("UPDATE dc_jobs SET status='skipped',finished_at=?,error_code='automatic_collection_disabled' WHERE id=?",
                         [(stamp, identifier) for identifier in identifiers])
    return len(identifiers)


def claim_job(daily_limit, *, now=None):
    stamp = _stamp(now)
    if type(daily_limit) is not int or not 0 <= daily_limit <= 10000:
        raise ValueError("无效每日任务额度")
    day = datetime.fromtimestamp(stamp, SHANGHAI).date().isoformat()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT COUNT(*) FROM dc_attempts WHERE day=?", (day,)).fetchone()[0] >= daily_limit:
            return None
        row = conn.execute("""SELECT j.* FROM dc_jobs j WHERE j.status='queued'
            ORDER BY j.started_at IS NOT NULL,j.started_at,j.queued_at,j.id LIMIT 1""").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE dc_jobs SET status='running',started_at=?,finished_at=NULL,error_code=NULL WHERE id=?", (stamp, row["id"]))
        conn.execute("INSERT INTO dc_attempts(job_id,day,started_at) VALUES(?,?,?)", (row["id"], day, stamp))
        return _job(conn.execute("SELECT * FROM dc_jobs WHERE id=?", (row["id"],)).fetchone())


def finish_job(identifier, result, *, now=None):
    stamp = _stamp(now)
    if not isinstance(result, dict):
        raise ValueError("无效采集结果")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT * FROM dc_jobs WHERE id=?", (identifier,)).fetchone()
        if not job or job["status"] != "running":
            raise ValueError("采集任务不在运行状态")
        status = result.get("status", "success")
        if status not in {"success", "partial", "failed", "skipped"}:
            raise ValueError("无效采集结果")
        received, added = result.get("records_received", 0), result.get("records_added", 0)
        if any(type(value) is not int or not 0 <= value <= 1000000000 for value in (received, added)) or added > received:
            raise ValueError("无效采集计数")
        if stamp < job["started_at"]:
            raise ValueError("完成时间不能早于开始时间")
        code = result.get("error_code")
        if code is not None and (not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)):
            code = "source_unavailable"
        conn.execute("UPDATE dc_jobs SET status=?,finished_at=?,records_received=?,records_added=?,error_code=? WHERE id=?",
                     (status, stamp, received, added, code, identifier))
        row = conn.execute("SELECT payload FROM dc_sources WHERE source_id=?", (job["source_id"],)).fetchone()
        state = json.loads(row[0]) if row else {}
        state.update(status=status, last_attempt_at=iso(job["started_at"]), last_finished_at=iso(stamp),
                     duration_seconds=round(stamp - job["started_at"], 3), records_received=received,
                     records_added=added, error_code=code, last_job_id=identifier)
        if status == "success":
            state.update(last_success_at=iso(stamp), failures=0)
        elif status in {"failed", "partial"}:
            state["failures"] = state.get("failures", 0) + 1
            if status == "partial" and received:
                state["last_success_at"] = iso(stamp)
        state["retry_at"] = iso(stamp + retry_delay(state.get("failures", 0), code)) if status in {"failed", "partial"} else None
        if status == "skipped" and code in {"source_busy", "memory_pressure"}:
            state["retry_at"] = iso(stamp + 60)
        payload = json.loads(job["payload"])
        if payload.get("scheduled_for"):
            state["last_scheduled_for"] = payload["scheduled_for"]
        if payload.get("round_id"):
            round_row = conn.execute("SELECT * FROM dc_rounds WHERE id=?", (payload["round_id"],)).fetchone()
            if round_row and set(json.loads(round_row["symbols"])) <= set(json.loads(round_row["completed"])):
                conn.execute("UPDATE dc_rounds SET status='complete',finished_at=? WHERE id=?", (stamp, round_row["id"]))
                state["last_round_finished_at"] = iso(stamp)
        elif status == "success":
            state["last_round_finished_at"] = iso(stamp)
        # A skipped job (empty pool/provider cooldown) is not a failed request.
        conn.execute("INSERT INTO dc_sources VALUES(?,?) ON CONFLICT(source_id) DO UPDATE SET payload=excluded.payload",
                     (job["source_id"], canonical(state)))


def list_jobs(limit=50):
    if type(limit) is not int:
        raise ValueError("无效任务分页")
    with connection() as conn:
        return [_job(row) for row in conn.execute("SELECT * FROM dc_jobs ORDER BY queued_at DESC,id DESC LIMIT ?", (max(1, min(limit, 200)),))]


def usage(*, now=None):
    stamp = _stamp(now)
    day = datetime.fromtimestamp(stamp, SHANGHAI).date().isoformat()
    with connection() as conn:
        used = conn.execute("SELECT COUNT(*) FROM dc_attempts WHERE day=?", (day,)).fetchone()[0]
        counts = dict(conn.execute("SELECT status,COUNT(*) FROM dc_jobs WHERE status IN ('queued','running') GROUP BY status"))
    return {"day": day, "jobs_started": used, "queued": counts.get("queued", 0), "running": counts.get("running", 0)}


def _clean_record(row, source_id, category, stamp):
    if not isinstance(row, dict) or row.get("source_id") != source_id:
        raise ValueError("来源标识不符")
    upstream, title = row.get("upstream_id"), row.get("title")
    if not isinstance(upstream, str) or not 1 <= len(upstream) <= 500 or not isinstance(title, str) or not title.strip():
        raise ValueError("数据缺少有效标识或来源")
    url = _public_url(row.get("source_url"))
    number = row.get("value")
    try:
        valid_number = number is None or (not isinstance(number, bool) and isinstance(number, (int, float)) and math.isfinite(number))
    except OverflowError:
        valid_number = False
    if not valid_number:
        raise ValueError("数值必须为有限数字或缺值")
    published = row.get("published_at")
    if published is not None:
        try:
            moment = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if moment.tzinfo is None or moment.utcoffset() is None or moment.timestamp() > stamp:
                raise ValueError
            published = moment.astimezone(timezone.utc).isoformat()
        except (AttributeError, ValueError, TypeError, OverflowError):
            raise ValueError("公布时点无效或晚于采集时点") from None
    symbols = row.get("symbols") or []
    if not isinstance(symbols, list) or len(symbols) > 30 or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9.^:/_-]{1,32}", item) for item in symbols):
        raise ValueError("无效标的列表")
    metadata = row.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("元数据必须为对象")
    value = {"published_at": published, "value": number}
    for key, maximum in (("source", 300), ("period", 32), ("region", 32), ("unit", 100)):
        field = row.get(key)
        if field is not None and (not isinstance(field, str) or len(field) > maximum):
            raise ValueError("文本字段无效")
        value[key] = _clean_public_json(field)
    value.update(source_id=source_id, upstream_id=upstream, category=category, title=_clean_public_json(title[:500]), source_url=url,
                 text=_clean_public_json(str(row.get("text") or "")[:8000]), symbols=symbols, metadata=_clean_public_json(metadata or {}))
    # Observation timestamps belong to local provenance, never to semantic hashes.
    if len(canonical(value)) > 50000:
        raise ValueError("单条数据过大")
    return value


def _batch_path(relative):
    root = settings.data_dir.resolve()
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("无效数据批次路径")
    path = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("数据批次路径不能为符号链接")
    return path


def _write_batch(relative, payload):
    path = _batch_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".batch-", dir=path.parent)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in _encoded_chunks(payload):
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            handle.write(b"\n")
            digest.update(b"\n")
            size += 1
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            if _file_digest(path) != (size, digest.digest()):
                raise ValueError("不可变数据批次冲突")
        else:
            os.link(tmp, path)  # No replacement of an existing immutable batch.
    finally:
        Path(tmp).unlink(missing_ok=True)
    return size


def ingest(source_id, category, rows, *, observed_at=None):
    source_id, category, stamp = _identifier(source_id), _identifier(category, category=True), _stamp(observed_at)
    if not isinstance(rows, list) or not rows or len(rows) > 20000:
        raise ValueError("无效采集批次")
    records, identifiers, encoded_size = [], set(), 2
    for row in rows:
        clean = _clean_record(row, source_id, category, stamp)
        encoded_size += len(canonical(clean).encode("utf-8")) + bool(records)
        if encoded_size > _MAX_BATCH_BYTES:
            raise ValueError("单批规范数据不能超过16 MiB，请分批归档")
        if clean["upstream_id"] in identifiers:
            raise ValueError("采集批次存在重复数据标识")
        identifiers.add(clean["upstream_id"])
        records.append(clean)
    records.sort(key=lambda row: row["upstream_id"])
    hasher = hashlib.sha256()
    for chunk in _encoded_chunks(records):
        hasher.update(chunk)
    digest = hasher.hexdigest()
    batch_id = hashlib.sha256(f"{source_id}:{digest}".encode()).hexdigest()
    relative = f"datasets/{category}/{source_id}/{batch_id}.json"
    added = 0
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Validate every existing head before creating a file, so a rejected
        # backfill cannot leave an orphan batch behind on disk.
        heads = {}
        for row in records:
            rid = hashlib.sha256(f"{source_id}:{row['upstream_id']}".encode()).hexdigest()
            head = conn.execute("SELECT * FROM dc_heads WHERE record_id=?", (rid,)).fetchone()
            if head and head["last_seen_at"] > stamp:
                raise ValueError("不能倒填采集时点")
            heads[rid] = head
        existing = conn.execute("SELECT * FROM dc_batches WHERE id=?", (batch_id,)).fetchone()
        first_seen = existing["observed_at"] if existing else stamp
        batch = {"version": 1, "source_id": source_id, "category": category, "content_hash": digest, "records": records}
        path = _batch_path(relative)
        if not existing and path.exists():
            # A crash can occur after the atomic file link and before SQLite
            # commits. Reuse that identical file instead of being blocked by a
            # different retry timestamp. New revisions still use this attempt's
            # observed_at, so recovery cannot backdate retrievable evidence.
            try:
                # Canonical key order places this scalar before records. Read
                # only its small header; _write_batch verifies the whole file.
                with path.open("rb") as handle:
                    header = handle.read(4096).decode("utf-8", errors="ignore").split(',"records":', 1)[0]
                match = re.search(r'"observed_at":("[^"\\]*")', header)
                if match is None:
                    raise ValueError
                first_seen = _stamp(datetime.fromisoformat(json.loads(match[1])).timestamp())
                if first_seen > stamp:
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                raise ValueError("不可变数据批次冲突") from None
        size = _write_batch(relative, {**batch, "observed_at": iso(first_seen)})
        conn.execute("INSERT OR IGNORE INTO dc_batches VALUES(?,?,?,?,?,?,?)", (batch_id, source_id, digest, relative, first_seen, len(records), size))
        for row in records:
            rid = hashlib.sha256(f"{source_id}:{row['upstream_id']}".encode()).hexdigest()
            payload = canonical(row)
            rhash = hashlib.sha256(payload.encode()).hexdigest()
            head = heads[rid]
            revision = head["revision_id"] if head else ""
            if head is None or head["content_hash"] != rhash:
                revision = hashlib.sha256(f"{rid}:{rhash}:{stamp}:{revision}".encode()).hexdigest()
                conn.execute("INSERT INTO dc_revisions(revision_id,record_id,source_id,batch_id,observed_at,content_hash,payload) VALUES(?,?,?,?,?,?,?)",
                             (revision, rid, source_id, batch_id, stamp, rhash, payload))
                text = f"{row['title']}\n{row['text']}\n统计期 {row.get('period')}；值 {row.get('value')} {row.get('unit') or ''}\n来源 {row['source_url']}"
                for start in range(0, len(text), 1200):
                    chunk = text[max(0, start - 120):start + 1200]
                    conn.execute("INSERT INTO dc_chunks VALUES(?,?,?)", (revision, chunk, " ".join(tokens(row['title'] + " " + category + " " + chunk))))
                added += 1
            conn.execute("INSERT INTO dc_heads VALUES(?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET revision_id=excluded.revision_id,content_hash=excluded.content_hash,last_seen_at=excluded.last_seen_at", (rid, revision, rhash, stamp))
    from app.storage import data_asset_manager
    data_asset_manager.register_existing_asset(settings.data_dir / relative, kind="public_dataset_batch", key=batch_id, source=source_id, item_count=len(records))
    return {"records_received": len(records), "records_added": added, "batch_id": batch_id, "size_bytes": size}


_VISIBLE = """WITH visible AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY record_id ORDER BY observed_at DESC,seq DESC) AS rn
 FROM dc_revisions WHERE observed_at<=?)"""


def records(*, source_id=None, category=None, q="", as_of=None, offset=0, limit=50, latest_period_only=False):
    stamp = min(_stamp(as_of), time.time())
    if not isinstance(q, str) or len(q) > 10000 or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("无效数据查询分页或关键词")
    if type(latest_period_only) is not bool:
        raise ValueError("统计期筛选必须为布尔值")
    clauses, args = ["rn=1"], [stamp]
    if source_id:
        _identifier(source_id)
        clauses.append("source_id=?")
        args.append(source_id)
    if category:
        _identifier(category, category=True)
        clauses.append("json_extract(payload,'$.category')=?")
        args.append(category)
    with connection() as conn:
        # The historical corpus may be much larger than a result page. Keep
        # ranking/sorting on SQLite's bounded, file-backed working tables.
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("PRAGMA cache_size=-2048")
        conn.execute("PRAGMA temp.cache_size=-2048")
        conn.execute("BEGIN")
        selection = "SELECT * FROM visible WHERE " + " AND ".join(clauses)
        if latest_period_only:
            selection = """SELECT * FROM (SELECT *, ROW_NUMBER() OVER (
                PARTITION BY source_id,json_extract(payload,'$.title'),json_extract(payload,'$.region')
                ORDER BY COALESCE(json_extract(payload,'$.period'),'') DESC,
                         COALESCE(json_extract(payload,'$.published_at'),'') DESC,observed_at DESC,seq DESC
                ) AS period_rank FROM (""" + selection + ")) WHERE period_rank=1"
        conn.execute("CREATE TEMP TABLE dc_selected_revisions(revision_id TEXT PRIMARY KEY)")
        conn.execute(_VISIBLE + " INSERT INTO dc_selected_revisions SELECT revision_id FROM (" + selection + ")", args)
        # All following reads share the snapshot established above, including a
        # concurrent ingestion between payload selection and FTS construction.
        words = tokens(q)[:40]
        join_hits = ""
        extras = ""
        ranking = ""
        if words:
            conn.execute("CREATE VIRTUAL TABLE temp.dc_visible_fts USING fts5(revision_id UNINDEXED,text,terms)")
            if latest_period_only:
                conn.execute("INSERT INTO dc_visible_fts SELECT c.revision_id,c.text,c.terms FROM dc_chunks c JOIN dc_selected_revisions s ON s.revision_id=c.revision_id")
            else:
                # Match the historical BM25 corpus, including other publishers;
                # filters restrict results, never silently change past scores.
                conn.execute(_VISIBLE + " INSERT INTO dc_visible_fts SELECT c.revision_id,c.text,c.terms FROM dc_chunks c JOIN visible v ON v.revision_id=c.revision_id AND v.rn=1", (stamp,))
            match = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
            conn.execute("CREATE TEMP TABLE dc_visible_hits(revision_id TEXT PRIMARY KEY,snippet TEXT,rank REAL)")
            conn.execute("""INSERT OR IGNORE INTO dc_visible_hits
                SELECT revision_id,text,bm25(dc_visible_fts) AS rank FROM dc_visible_fts
                WHERE dc_visible_fts MATCH ? ORDER BY rank,revision_id,rowid""", (match,))
            join_hits = " JOIN dc_visible_hits h ON h.revision_id=r.revision_id"
            extras = ",h.snippet,h.rank"
            ranking = "h.rank ASC,"
        selected = " FROM dc_revisions r JOIN dc_selected_revisions s ON s.revision_id=r.revision_id" + join_hits
        if q and not words:
            selected += " WHERE 0"
        total = conn.execute("SELECT COUNT(*)" + selected).fetchone()[0]
        rows = conn.execute("SELECT r.*" + extras + selected + " ORDER BY " + ranking +
                            "r.observed_at DESC,COALESCE(json_extract(r.payload,'$.period'),'') DESC,r.record_id ASC LIMIT ? OFFSET ?",
                            (limit, offset)).fetchall()
        items = [{**json.loads(row["payload"]), "id": row["record_id"], "revision_id": row["revision_id"],
                  "batch_id": row["batch_id"], "observed_at": iso(row["observed_at"]),
                  "citation": f"dataset:{row['record_id']}:{row['revision_id']}",
                  **({"snippet": row["snippet"], "rank": row["rank"]} if words else {})} for row in rows]
    return {"items": items, "total": total, "offset": offset, "limit": limit, "as_of": iso(stamp)}


def dataset_stats():
    with connection() as conn:
        return {"records": conn.execute("SELECT COUNT(*) FROM dc_heads").fetchone()[0],
                "revisions": conn.execute("SELECT COUNT(*) FROM dc_revisions").fetchone()[0],
                "batches": conn.execute("SELECT COUNT(*) FROM dc_batches").fetchone()[0],
                "sources": [dict(row) for row in conn.execute("SELECT source_id,COUNT(*) AS batches,SUM(records) AS batch_records,SUM(size_bytes) AS size_bytes FROM dc_batches GROUP BY source_id")]}
