"""Account-isolated research dossiers, frozen inputs and single-dispatch jobs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import re
import time
import uuid
from zoneinfo import ZoneInfo

from app.config import settings
from app.schemas.investment_research import DossierCreate, DossierPatch, QuestionCreate, QuestionPatch
from app.storage import sqlite_bootstrap

LEASE_SECONDS = 120
_SCHEMA = """
CREATE TABLE IF NOT EXISTS dossiers(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,payload TEXT NOT NULL,
 revision INTEGER NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS dossier_user ON dossiers(user_id,updated_at DESC);
CREATE TABLE IF NOT EXISTS dossier_revisions(dossier_id TEXT NOT NULL,user_id INTEGER NOT NULL,
 revision INTEGER NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(dossier_id,revision));
CREATE TABLE IF NOT EXISTS questions(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,dossier_id TEXT NOT NULL,
 payload TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS materials(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,dossier_id TEXT NOT NULL,
 payload TEXT NOT NULL,monitor_run_id TEXT,deleted INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS monitoring_material ON materials(user_id,dossier_id,monitor_run_id)
 WHERE monitor_run_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS research_jobs(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,dossier_id TEXT NOT NULL,
 kind TEXT NOT NULL,material_id TEXT,focus TEXT NOT NULL,snapshot TEXT NOT NULL,status TEXT NOT NULL,phase TEXT NOT NULL,
 created_at REAL NOT NULL,started_at REAL,finished_at REAL,owner TEXT,lease_until REAL,attempts INTEGER NOT NULL DEFAULT 0,
 dispatched_at REAL,error_code TEXT,result TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS research_active ON research_jobs(user_id,dossier_id,kind,COALESCE(material_id,''))
 WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS research_jobs_user ON research_jobs(user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS research_calls(job_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,day TEXT NOT NULL,created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS research_calls_user ON research_calls(user_id,day);
CREATE TABLE IF NOT EXISTS research_reports(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,dossier_id TEXT NOT NULL,
 job_id TEXT NOT NULL,input_hash TEXT NOT NULL,created_at REAL NOT NULL,result TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS research_report_cache ON research_reports(user_id,dossier_id,input_hash);
CREATE TABLE IF NOT EXISTS research_report_reads(user_id INTEGER NOT NULL,report_id TEXT NOT NULL,read_at REAL,
 PRIMARY KEY(user_id,report_id));
CREATE TABLE IF NOT EXISTS research_adoptions(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,dossier_id TEXT NOT NULL,
 report_id TEXT NOT NULL,previous_report_id TEXT,revision INTEGER NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS research_state(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS immutable_research_report_update BEFORE UPDATE ON research_reports
 BEGIN SELECT RAISE(ABORT,'research reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_research_report_delete BEFORE DELETE ON research_reports
 BEGIN SELECT RAISE(ABORT,'research reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_research_adoption_update BEFORE UPDATE ON research_adoptions
 BEGIN SELECT RAISE(ABORT,'research adoptions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_research_adoption_delete BEFORE DELETE ON research_adoptions
 BEGIN SELECT RAISE(ABORT,'research adoptions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_dossier_revision_update BEFORE UPDATE ON dossier_revisions
 BEGIN SELECT RAISE(ABORT,'research dossier revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_dossier_revision_delete BEFORE DELETE ON dossier_revisions
 BEGIN SELECT RAISE(ABORT,'research dossier revisions are immutable'); END;
"""


class ResearchConflict(ValueError):
    pass


class NotFound(ValueError):
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


def _id():
    return uuid.uuid4().hex


@contextmanager
def connection():
    with sqlite_bootstrap.connection(settings.data_dir / "investment_research.sqlite3", _SCHEMA, version=2) as db:
        yield db


def _payload(row):
    return json.loads(row["payload"]) if row else None


def _dossier(db, uid, identifier):
    value = _payload(db.execute("SELECT payload FROM dossiers WHERE user_id=? AND id=?", (_uid(uid), identifier)).fetchone())
    if value is None:
        raise NotFound("研究档案不存在")
    return value


def target_signature(value):
    return (value.get("target_type"), value.get("target_code", ""), tuple(sorted(value.get("target_symbols") or [])),
            tuple(sorted(value.get("keywords") or [])) if value.get("target_type") == "topic" else ())


def _save_dossier(db, uid, value):
    db.execute("UPDATE dossiers SET payload=?,revision=?,status=?,updated_at=? WHERE user_id=? AND id=?",
               (_json(value), value["revision"], value["status"], value["updated_at"], uid, value["id"]))
    db.execute("INSERT INTO dossier_revisions VALUES(?,?,?,?,?)", (value["id"], uid, value["revision"], _json(value), value["updated_at"]))


def _touch(db, uid, dossier):
    dossier = {**dossier, "revision": dossier["revision"] + 1, "updated_at": time.time()}
    _save_dossier(db, uid, dossier)
    db.execute("UPDATE research_jobs SET status='cancelled',phase='cancelled',finished_at=?,error_code='dossier_changed' "
               "WHERE user_id=? AND dossier_id=? AND kind='generate' AND status='queued'", (time.time(), uid, dossier["id"]))
    return dossier


def _active(dossier):
    if dossier["status"] != "active":
        raise ResearchConflict("档案已归档，请先恢复再添加或生成研究")


def create_dossier(uid, payload, *, monitoring_material=None):
    uid = _uid(uid)
    value = DossierCreate.model_validate(payload).model_dump(exclude={"monitor_run_id"})
    stamp = time.time()
    value.update(id=_id(), revision=1, adopted_report_id=None, created_at=stamp, updated_at=stamp)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT COUNT(*) FROM dossiers WHERE user_id=?", (uid,)).fetchone()[0] >= 200:
            raise ResearchConflict("每个账户最多保存200个研究档案")
        db.execute("INSERT INTO dossiers VALUES(?,?,?,?,?,?,?)", (value["id"], uid, _json(value), 1, value["status"], stamp, stamp))
        db.execute("INSERT INTO dossier_revisions VALUES(?,?,?,?,?)", (value["id"], uid, 1, _json(value), stamp))
        if monitoring_material is not None:
            _add_material(db, uid, value, monitoring_material)
            value = _dossier(db, uid, value["id"])
    return value


def get_dossier(uid, identifier):
    with connection() as db:
        return _payload(db.execute("SELECT payload FROM dossiers WHERE user_id=? AND id=?", (_uid(uid), identifier)).fetchone())


def list_dossiers(uid, *, symbol=None):
    with connection() as db:
        rows = [_payload(row) for row in db.execute("SELECT payload FROM dossiers WHERE user_id=? ORDER BY updated_at DESC,id", (_uid(uid),))]
        if symbol:
            rows = [row for row in rows if row.get("target_code") == symbol or symbol in row.get("target_symbols", [])]
        for value in rows:
            latest = db.execute("SELECT id,result FROM research_reports WHERE user_id=? AND dossier_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (uid, value["id"])).fetchone()
            value["latest_report_id"] = latest["id"] if latest else None
            value["latest_report_summary"] = json.loads(latest["result"]).get("report", {}).get("summary", "") if latest else ""
            adopted = db.execute("SELECT result FROM research_reports WHERE user_id=? AND id=?", (uid, value.get("adopted_report_id"))).fetchone()
            value["adopted_report_summary"] = json.loads(adopted[0]).get("report", {}).get("summary", "") if adopted else ""
            value["unread_count"] = db.execute("SELECT COUNT(*) FROM research_reports r LEFT JOIN research_report_reads x ON r.id=x.report_id AND r.user_id=x.user_id WHERE r.user_id=? AND r.dossier_id=? AND x.read_at IS NULL", (uid, value["id"])).fetchone()[0]
            value["active_jobs"] = db.execute("SELECT COUNT(*) FROM research_jobs WHERE user_id=? AND dossier_id=? AND status IN ('queued','running')", (uid, value["id"])).fetchone()[0]
        return rows


def update_dossier(uid, identifier, patch):
    patch = DossierPatch.model_validate(patch).model_dump(exclude_unset=True)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        old = _dossier(db, uid, identifier)
        if old["revision"] != patch.pop("revision"):
            raise ResearchConflict("档案已更新，请刷新后重试")
        fields = {key: old[key] for key in DossierCreate.model_fields if key != "monitor_run_id"}
        updated = DossierCreate.model_validate({**fields, **patch}).model_dump(exclude={"monitor_run_id"})
        value = {**old, **updated}
        if target_signature(value) != target_signature(old):
            value["adopted_report_id"] = None
        value = _touch(db, uid, value)
        if value["status"] == "archived":
            for job in db.execute("SELECT * FROM research_jobs WHERE user_id=? AND dossier_id=? AND kind='prepare' AND status='queued'", (uid, identifier)).fetchall():
                _material_failed(db, job, "dossier_archived", touch=False)
            db.execute("UPDATE research_jobs SET status='cancelled',phase='cancelled',finished_at=?,error_code='dossier_archived' WHERE user_id=? AND dossier_id=? AND status='queued'", (time.time(), uid, identifier))
        return value


def _question(db, uid, identifier):
    value = _payload(db.execute("SELECT payload FROM questions WHERE user_id=? AND id=? AND deleted=0", (_uid(uid), identifier)).fetchone())
    if value is None:
        raise NotFound("研究问题不存在")
    return value


def list_questions(uid, dossier_id):
    with connection() as db:
        _dossier(db, uid, dossier_id)
        return [_payload(row) for row in db.execute("SELECT payload FROM questions WHERE user_id=? AND dossier_id=? AND deleted=0 ORDER BY rowid", (uid, dossier_id))]


def create_question(uid, dossier_id, payload):
    value = QuestionCreate.model_validate(payload).model_dump()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        dossier = _dossier(db, uid, dossier_id)
        _active(dossier)
        if db.execute("SELECT COUNT(*) FROM questions WHERE user_id=? AND dossier_id=? AND deleted=0", (uid, dossier_id)).fetchone()[0] >= 30:
            raise ResearchConflict("每个档案最多保存30个问题")
        value.update(id=_id(), dossier_id=dossier_id, revision=1, created_at=time.time(), updated_at=time.time())
        db.execute("INSERT INTO questions(id,user_id,dossier_id,payload) VALUES(?,?,?,?)", (value["id"], uid, dossier_id, _json(value)))
        _touch(db, uid, dossier)
        return value


def update_question(uid, identifier, patch):
    patch = QuestionPatch.model_validate(patch).model_dump(exclude_unset=True)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        old = _question(db, uid, identifier)
        dossier = _dossier(db, uid, old["dossier_id"])
        _active(dossier)
        if old["revision"] != patch.pop("revision"):
            raise ResearchConflict("问题已更新，请刷新后重试")
        fields = {key: old[key] for key in QuestionCreate.model_fields}
        value = {**old, **QuestionCreate.model_validate({**fields, **patch}).model_dump(), "revision": old["revision"]+1, "updated_at": time.time()}
        db.execute("UPDATE questions SET payload=? WHERE user_id=? AND id=?", (_json(value), uid, identifier))
        _touch(db, uid, dossier)
        return value


def delete_question(uid, identifier, revision):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        value = _question(db, uid, identifier)
        dossier = _dossier(db, uid, value["dossier_id"])
        _active(dossier)
        if value["revision"] != revision:
            raise ResearchConflict("问题已更新，请刷新后重试")
        db.execute("UPDATE questions SET deleted=1 WHERE user_id=? AND id=?", (uid, identifier))
        _touch(db, uid, dossier)
    return {"deleted": True}


def _add_material(db, uid, dossier, payload):
    _active(dossier)
    if payload.get("monitor_run_id"):
        old = db.execute("SELECT payload,deleted FROM materials WHERE user_id=? AND dossier_id=? AND monitor_run_id=?", (uid, dossier["id"], payload["monitor_run_id"])).fetchone()
        if old:
            if old["deleted"]:
                existing = db.execute("SELECT payload FROM materials WHERE user_id=? AND dossier_id=? AND deleted=0", (uid, dossier["id"])).fetchall()
                if len(existing) >= 30 or sum(len(row[0]) for row in existing) + len(old["payload"]) > 4000000:
                    raise ResearchConflict("档案材料已达上限，请移除不需要的材料后再恢复")
                db.execute("UPDATE materials SET deleted=0 WHERE user_id=? AND dossier_id=? AND monitor_run_id=?", (uid, dossier["id"], payload["monitor_run_id"]))
                _touch(db, uid, dossier)
            return {**_payload(old), "deduplicated": True}
    rows = db.execute("SELECT payload FROM materials WHERE user_id=? AND dossier_id=? AND deleted=0", (uid, dossier["id"])).fetchall()
    if len(rows) >= 30:
        raise ResearchConflict("每个档案最多保存30份材料")
    if sum(len(row[0]) for row in rows) + len(_json(payload)) > 4000000:
        raise ResearchConflict("档案材料文字已达400万字符上限，请移除不需要的材料")
    stamp = time.time()
    value = {**payload, "id": _id(), "dossier_id": dossier["id"], "created_at": stamp, "updated_at": stamp}
    value.setdefault("status", "ready")
    value.setdefault("text", "")
    value.setdefault("pages", [])
    value.setdefault("sha256", hashlib.sha256(_json(payload).encode()).hexdigest())
    db.execute("INSERT INTO materials(id,user_id,dossier_id,payload,monitor_run_id) VALUES(?,?,?,?,?)", (value["id"], uid, dossier["id"], _json(value), value.get("monitor_run_id")))
    _touch(db, uid, dossier)
    return value


def add_material(uid, dossier_id, payload):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        return _add_material(db, uid, _dossier(db, uid, dossier_id), payload)


def get_material(uid, identifier, *, include_deleted=False):
    with connection() as db:
        return _payload(db.execute("SELECT payload FROM materials WHERE user_id=? AND id=? AND (deleted=0 OR ?)", (_uid(uid), identifier, include_deleted)).fetchone())


def list_materials(uid, dossier_id, *, full=False):
    with connection() as db:
        _dossier(db, uid, dossier_id)
        values = [_payload(row) for row in db.execute("SELECT payload FROM materials WHERE user_id=? AND dossier_id=? AND deleted=0 ORDER BY rowid", (uid, dossier_id))]
    if full:
        return values
    return [{key: value for key, value in row.items() if key not in {"text", "pages", "monitoring_snapshot"}} for row in values]


def delete_material(uid, identifier):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        value = _payload(db.execute("SELECT payload FROM materials WHERE user_id=? AND id=? AND deleted=0", (_uid(uid), identifier)).fetchone())
        if value is None:
            raise NotFound("材料不存在")
        dossier = _dossier(db, uid, value["dossier_id"])
        _active(dossier)
        db.execute("UPDATE materials SET deleted=1 WHERE user_id=? AND id=?", (uid, identifier))
        db.execute("UPDATE research_jobs SET status='cancelled',phase='cancelled',finished_at=? WHERE user_id=? AND material_id=? AND status='queued'", (time.time(), uid, identifier))
        _touch(db, uid, dossier)
    return {"deleted": True}


def _job(row):
    if row is None:
        return None
    value = dict(row)
    for key in ("snapshot", "result"):
        value[key] = json.loads(value[key]) if value[key] else None
    return value


def get_job(uid, identifier):
    with connection() as db:
        return _job(db.execute("SELECT * FROM research_jobs WHERE user_id=? AND id=?", (_uid(uid), identifier)).fetchone())


def list_jobs(uid, dossier_id):
    with connection() as db:
        _dossier(db, uid, dossier_id)
        return [_job(row) for row in db.execute("SELECT * FROM research_jobs WHERE user_id=? AND dossier_id=? ORDER BY created_at DESC,rowid DESC LIMIT 30", (uid, dossier_id))]


def enqueue(uid, dossier_id, *, kind="generate", material_id=None, focus=""):
    if kind not in {"generate", "prepare"} or not isinstance(focus, str) or len(focus) > 2000:
        raise ValueError("研究任务参数无效")
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        dossier = _dossier(db, uid, dossier_id)
        _active(dossier)
        active = db.execute("SELECT * FROM research_jobs WHERE user_id=? AND dossier_id=? AND kind=? AND COALESCE(material_id,'')=? AND status IN ('queued','running')", (uid, dossier_id, kind, material_id or "")).fetchone()
        if active:
            if active["focus"] != focus.strip() or json.loads(active["snapshot"])["dossier"]["revision"] != dossier["revision"]:
                raise ResearchConflict("该档案已有任务正在处理，请完成后重新提交")
            return {**_job(active), "deduplicated": True}
        if db.execute("SELECT COUNT(*) FROM research_jobs WHERE user_id=? AND status IN ('queued','running')", (uid,)).fetchone()[0] >= 10:
            raise ResearchConflict("当前账户待处理研究任务较多，请稍后重试")
        if db.execute("SELECT COUNT(*) FROM research_jobs WHERE status IN ('queued','running')").fetchone()[0] >= 1000:
            raise ResearchConflict("研究任务队列已满，请稍后重试")
        materials = [_payload(row) for row in db.execute("SELECT payload FROM materials WHERE user_id=? AND dossier_id=? AND deleted=0 ORDER BY rowid", (uid, dossier_id))]
        if kind == "prepare":
            if not any(row["id"] == material_id and row["kind"] == "pdf" for row in materials):
                raise NotFound("待解析PDF材料不存在")
            material = next(row for row in materials if row["id"] == material_id)
            if material.get("status") != "pending":
                material = {**material, "status": "pending", "updated_at": time.time()}
                material.pop("error_code", None)
                db.execute("UPDATE materials SET payload=? WHERE user_id=? AND id=?", (_json(material), uid, material_id))
                dossier = _touch(db, uid, dossier)
        elif any(row.get("status") == "pending" for row in materials):
            raise ResearchConflict("材料仍在解析，请完成后再生成研究")
        questions = [_payload(row) for row in db.execute("SELECT payload FROM questions WHERE user_id=? AND dossier_id=? AND deleted=0 ORDER BY rowid", (uid, dossier_id))]
        snapshot = {"dossier": dossier, "questions": questions, "material_ids": [row["id"] for row in materials]}
        identifier, stamp = _id(), time.time()
        db.execute("INSERT INTO research_jobs(id,user_id,dossier_id,kind,material_id,focus,snapshot,status,phase,created_at) VALUES(?,?,?,?,?,?,?,'queued','queued',?)", (identifier, uid, dossier_id, kind, material_id, focus.strip(), _json(snapshot), stamp))
        return _job(db.execute("SELECT * FROM research_jobs WHERE id=?", (identifier,)).fetchone())


def claim(owner):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM research_jobs WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
        if row is None:
            return None
        stamp = time.time()
        db.execute("UPDATE research_jobs SET status='running',phase='starting',started_at=?,owner=?,lease_until=?,attempts=attempts+1 WHERE id=?", (stamp, owner, stamp+LEASE_SECONDS, row["id"]))
        return _job(db.execute("SELECT * FROM research_jobs WHERE id=?", (row["id"],)).fetchone())


def _leased(db, uid, identifier, owner):
    row = db.execute("SELECT * FROM research_jobs WHERE user_id=? AND id=? AND owner=? AND status='running' AND lease_until>?", (_uid(uid), identifier, owner, time.time())).fetchone()
    if row is None:
        raise LeaseLost("研究任务处理权已失效")
    return row


def heartbeat(uid, identifier, owner, *, phase=None):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        _leased(db, uid, identifier, owner)
        db.execute("UPDATE research_jobs SET lease_until=?,phase=COALESCE(?,phase) WHERE id=?", (time.time()+LEASE_SECONDS, phase, identifier))


def _day():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def usage(uid):
    day = _day()
    with connection() as db:
        used = db.execute("SELECT COUNT(*) FROM research_calls WHERE user_id=? AND day=?", (_uid(uid), day)).fetchone()[0]
    limit = settings.investment_research_daily_call_limit
    return {"used": used, "limit": limit, "remaining": max(0, limit-used), "day": day, "timezone": "Asia/Shanghai"}


def reserve_call(uid, identifier, owner):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _leased(db, uid, identifier, owner)
        dossier = _dossier(db, uid, row["dossier_id"])
        _active(dossier)
        if not settings.enable_investment_research or dossier["revision"] != json.loads(row["snapshot"])["dossier"]["revision"]:
            raise ResearchConflict("档案或后台配置已改变，本次未调用模型")
        if row["kind"] != "generate" or row["dispatched_at"] is not None:
            raise ResearchConflict("本研究请求已发送，不能自动重复调用")
        day, stamp = _day(), time.time()
        used = db.execute("SELECT COUNT(*) FROM research_calls WHERE user_id=? AND day=?", (uid, day)).fetchone()[0]
        if used >= settings.investment_research_daily_call_limit:
            raise DailyLimit("今日研究模型请求额度已用完，请明日再试")
        db.execute("INSERT INTO research_calls VALUES(?,?,?,?)", (identifier, uid, day, stamp))
        db.execute("UPDATE research_jobs SET dispatched_at=? WHERE id=?", (stamp, identifier))


def _report(row):
    if row is None:
        return None
    value = dict(row)
    value["result"] = json.loads(value["result"])
    value["is_read"] = value.pop("read_at", None) is not None
    value.pop("user_id", None)
    return value


def _get_report(db, uid, identifier):
    return _report(db.execute("SELECT r.*,x.read_at FROM research_reports r LEFT JOIN research_report_reads x ON r.id=x.report_id AND r.user_id=x.user_id WHERE r.user_id=? AND r.id=?", (_uid(uid), identifier)).fetchone())


def get_report(uid, identifier):
    with connection() as db:
        return _get_report(db, uid, identifier)


def list_reports(uid, dossier_id):
    with connection() as db:
        _dossier(db, uid, dossier_id)
        return [_report(row) for row in db.execute("SELECT r.*,x.read_at FROM research_reports r LEFT JOIN research_report_reads x ON r.id=x.report_id AND r.user_id=x.user_id WHERE r.user_id=? AND r.dossier_id=? ORDER BY r.created_at DESC,r.rowid DESC LIMIT 50", (uid, dossier_id))]


def cached_report(uid, dossier_id, input_hash):
    with connection() as db:
        row = db.execute("SELECT id FROM research_reports WHERE user_id=? AND dossier_id=? AND input_hash=? ORDER BY created_at DESC LIMIT 1", (_uid(uid), dossier_id, input_hash)).fetchone()
        return _get_report(db, uid, row[0]) if row else None


def finish(uid, identifier, owner, result):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        job = _leased(db, uid, identifier, owner)
        stamp = time.time()
        if job["kind"] == "prepare":
            material = _payload(db.execute("SELECT payload FROM materials WHERE user_id=? AND id=? AND deleted=0", (uid, job["material_id"])).fetchone())
            if material is None:
                raise ResearchConflict("材料已移除，解析结果不再写回")
            extraction = result["extraction"]
            if extraction.get("sha256") != material["sha256"]:
                raise ResearchConflict("材料校验失败")
            others = db.execute("SELECT payload FROM materials WHERE user_id=? AND dossier_id=? AND deleted=0 AND id<>?", (uid, job["dossier_id"], material["id"])).fetchall()
            value = {**material, **extraction, "updated_at": stamp}
            if sum(len(row[0]) for row in others)+len(_json(value)) > 4000000:
                value.update(status="too_large", pages=[], text="", warnings=["档案材料总文字超过400万字符上限"])
            db.execute("UPDATE materials SET payload=? WHERE user_id=? AND id=?", (_json(value), uid, material["id"]))
            _touch(db, uid, _dossier(db, uid, job["dossier_id"]))
            final = {"material_id": material["id"], "status": value["status"]}
        else:
            if result.get("reused") and result.get("cached_report_id"):
                cached = _get_report(db, uid, result["cached_report_id"])
                if not cached or cached["dossier_id"] != job["dossier_id"] or cached["result"].get("input_hash") != result.get("input_hash"):
                    raise ResearchConflict("研究缓存归属不一致")
                report_id = cached["id"]
            else:
                if result.get("model_called") and job["dispatched_at"] is None:
                    raise ResearchConflict("研究请求未计入模型额度")
                report_id = _id()
                saved = {**result, "dossier_snapshot": json.loads(job["snapshot"])["dossier"]}
                db.execute("INSERT INTO research_reports VALUES(?,?,?,?,?,?,?)", (report_id, uid, job["dossier_id"], identifier, saved["input_hash"], stamp, _json(saved)))
            final = {"report_id": report_id, "reused": bool(result.get("reused"))}
        db.execute("UPDATE research_jobs SET status='succeeded',phase='succeeded',finished_at=?,result=?,owner=NULL,lease_until=NULL WHERE id=?", (stamp, _json(final), identifier))
        return final


def _material_failed(db, job, code, *, touch=True):
    if job["kind"] != "prepare":
        return
    row = db.execute("SELECT payload FROM materials WHERE user_id=? AND id=? AND deleted=0", (job["user_id"], job["material_id"])).fetchone()
    if row is not None:
        value = {**_payload(row), "status": "error", "error_code": code, "updated_at": time.time()}
        db.execute("UPDATE materials SET payload=? WHERE user_id=? AND id=?", (_json(value), job["user_id"], job["material_id"]))
        if touch:
            _touch(db, job["user_id"], _dossier(db, job["user_id"], job["dossier_id"]))


def fail(uid, identifier, owner, error_code):
    code = error_code if isinstance(error_code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) else "research_failed"
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        job = _leased(db, uid, identifier, owner)
        _material_failed(db, job, code)
        db.execute("UPDATE research_jobs SET status='failed',phase='failed',finished_at=?,error_code=?,owner=NULL,lease_until=NULL WHERE id=?", (time.time(), code, identifier))


def cancel_job(uid, identifier):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM research_jobs WHERE user_id=? AND id=?", (_uid(uid), identifier)).fetchone()
        if row is None:
            raise NotFound("研究任务不存在")
        if row["status"] in {"queued", "running"}:
            if row["dispatched_at"] is not None:
                raise ResearchConflict("模型请求已发送，无法撤回；结果仍会保留为历史报告")
            _material_failed(db, row, "cancelled")
            db.execute("UPDATE research_jobs SET status='cancelled',phase='cancelled',finished_at=?,owner=NULL,lease_until=NULL WHERE id=?", (time.time(), identifier))
        return _job(db.execute("SELECT * FROM research_jobs WHERE id=?", (identifier,)).fetchone())


def recover_interrupted():
    """Call only while holding the process lock; paid requests are never requeued."""
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        for row in db.execute("SELECT * FROM research_jobs WHERE status='running'").fetchall():
            dossier = _dossier(db, row["user_id"], row["dossier_id"])
            changed = dossier["status"] != "active" or (row["kind"] == "generate" and dossier["revision"] != json.loads(row["snapshot"])["dossier"]["revision"])
            if row["dispatched_at"] is not None or row["attempts"] >= 3 or changed:
                code = "interrupted_after_dispatch" if row["dispatched_at"] is not None else "dossier_changed" if changed else "interrupted"
                _material_failed(db, row, code)
                db.execute("UPDATE research_jobs SET status='failed',phase='failed',finished_at=?,error_code=?,owner=NULL,lease_until=NULL WHERE id=?", (time.time(), code, row["id"]))
            else:
                db.execute("UPDATE research_jobs SET status='queued',phase='queued',owner=NULL,lease_until=NULL WHERE id=?", (row["id"],))


def mark_report(uid, identifier, is_read=True):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if _get_report(db, uid, identifier) is None:
            raise NotFound("研究报告不存在")
        db.execute("INSERT INTO research_report_reads VALUES(?,?,?) ON CONFLICT(user_id,report_id) DO UPDATE SET read_at=excluded.read_at", (uid, identifier, time.time() if is_read else None))
        return _get_report(db, uid, identifier)


def adopt_report(uid, identifier, revision):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        report = _get_report(db, uid, identifier)
        if report is None:
            raise NotFound("研究报告不存在")
        dossier = _dossier(db, uid, report["dossier_id"])
        _active(dossier)
        if dossier["revision"] != revision:
            raise ResearchConflict("档案已更新，请刷新后采用报告")
        if target_signature(dossier) != target_signature(report["result"]["dossier_snapshot"]):
            raise ResearchConflict("研究对象已经改变，不能将旧对象报告设为当前研究")
        previous = dossier.get("adopted_report_id")
        if previous == identifier:
            return dossier
        dossier["adopted_report_id"] = identifier
        dossier = _touch(db, uid, dossier)
        db.execute("INSERT INTO research_adoptions VALUES(?,?,?,?,?,?,?)", (_id(), uid, dossier["id"], identifier, previous, dossier["revision"], time.time()))
        db.execute("INSERT INTO research_report_reads VALUES(?,?,?) ON CONFLICT(user_id,report_id) DO UPDATE SET read_at=excluded.read_at", (uid, identifier, time.time()))
        return dossier


def list_adoptions(uid, dossier_id):
    with connection() as db:
        _dossier(db, uid, dossier_id)
        return [{key: value for key, value in dict(row).items() if key != "user_id"} for row in db.execute("SELECT * FROM research_adoptions WHERE user_id=? AND dossier_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100", (uid, dossier_id))]


def get_worker_state():
    with connection() as db:
        row = db.execute("SELECT payload FROM research_state WHERE key='worker'").fetchone()
        return json.loads(row[0]) if row else {}


def set_worker_state(value):
    with connection() as db:
        db.execute("INSERT INTO research_state VALUES('worker',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (_json(value),))
