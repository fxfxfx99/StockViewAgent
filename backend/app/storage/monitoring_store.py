"""Account-isolated monitoring queue, immutable results and counted dispatches.

Every state transition is transactional. A dispatched request is never retried
under the same run after a crash: its response and billing outcome are unknown.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
import re
import time
import uuid

from app.config import settings
from app.schemas.monitoring import ConfigPatch, HandlingCreate, PlanCreate, PlanPatch, PlanReview, SHANGHAI
from app.storage import sqlite_bootstrap

DEFAULT_CONFIG = {"enabled": True, "daily_call_limit": 12}
LEASE_SECONDS = 120
_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitoring_state (
 user_id INTEGER NOT NULL,key TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(user_id,key));
CREATE TABLE IF NOT EXISTS monitoring_plans (
 user_id INTEGER NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,
 next_run_at REAL NOT NULL,review_at TEXT NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(user_id,id));
CREATE INDEX IF NOT EXISTS monitoring_due ON monitoring_plans(status,next_run_at);
CREATE TABLE IF NOT EXISTS monitoring_revisions (
 user_id INTEGER NOT NULL,plan_id TEXT NOT NULL,revision INTEGER NOT NULL,
 kind TEXT NOT NULL,snapshot TEXT NOT NULL,review TEXT,created_at REAL NOT NULL,
 PRIMARY KEY(user_id,plan_id,revision));
CREATE TRIGGER IF NOT EXISTS monitoring_revision_immutable_update BEFORE UPDATE ON monitoring_revisions
 BEGIN SELECT RAISE(ABORT,'monitoring revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS monitoring_revision_immutable_delete BEFORE DELETE ON monitoring_revisions
 BEGIN SELECT RAISE(ABORT,'monitoring revisions are immutable'); END;
CREATE TABLE IF NOT EXISTS monitoring_runs (
 id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,plan_id TEXT NOT NULL,plan_revision INTEGER NOT NULL,
 kind TEXT NOT NULL,trigger TEXT NOT NULL,status TEXT NOT NULL,queued_at REAL NOT NULL,
 started_at REAL,finished_at REAL,not_before REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT,lease_token TEXT,lease_until REAL,dispatched_at REAL,error_code TEXT,
 plan_snapshot TEXT NOT NULL,result TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS monitoring_one_active ON monitoring_runs(user_id,plan_id)
 WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS monitoring_run_listing ON monitoring_runs(user_id,queued_at DESC);
CREATE TRIGGER IF NOT EXISTS monitoring_result_immutable BEFORE UPDATE OF result ON monitoring_runs
 WHEN OLD.result IS NOT NULL AND NEW.result IS NOT OLD.result
 BEGIN SELECT RAISE(ABORT,'monitoring results are immutable'); END;
CREATE TABLE IF NOT EXISTS monitoring_calls (
 id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,run_id TEXT,kind TEXT NOT NULL,day TEXT NOT NULL,created_at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS monitoring_run_dispatch_once ON monitoring_calls(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS monitoring_daily_usage ON monitoring_calls(user_id,day);
CREATE TABLE IF NOT EXISTS monitoring_notifications (
 user_id INTEGER NOT NULL,id TEXT NOT NULL,plan_id TEXT NOT NULL,run_id TEXT,kind TEXT NOT NULL,
 title TEXT NOT NULL,summary TEXT NOT NULL,created_at REAL NOT NULL,read_at REAL,dedup_key TEXT NOT NULL,
 PRIMARY KEY(user_id,id),UNIQUE(user_id,dedup_key));
CREATE INDEX IF NOT EXISTS monitoring_notification_listing ON monitoring_notifications(user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS monitoring_handling (
 id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,notification_id TEXT NOT NULL,plan_id TEXT NOT NULL,
 run_id TEXT,action TEXT NOT NULL,reason TEXT NOT NULL,dossier_id TEXT,created_at REAL NOT NULL,
 dedup_key TEXT NOT NULL,source_snapshot TEXT NOT NULL,UNIQUE(user_id,notification_id,dedup_key));
CREATE INDEX IF NOT EXISTS monitoring_handling_listing ON monitoring_handling(user_id,plan_id,created_at DESC);
CREATE TRIGGER IF NOT EXISTS monitoring_handling_immutable_update BEFORE UPDATE ON monitoring_handling
 BEGIN SELECT RAISE(ABORT,'monitoring handling history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS monitoring_handling_immutable_delete BEFORE DELETE ON monitoring_handling
 BEGIN SELECT RAISE(ABORT,'monitoring handling history is immutable'); END;
"""


class RevisionConflict(ValueError):
    pass


class LeaseLost(RuntimeError):
    pass


class BudgetExceeded(ValueError):
    pass


class PlanUnavailable(ValueError):
    pass


def _uid(value):
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise ValueError("无效账户")
    return value


def _now(value=None):
    stamp = time.time() if value is None else value
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp <= 0:
        raise ValueError("无效时间")
    return float(stamp)


def _json(value):
    """No credentials belong in plan, heartbeat, model-state or report snapshots."""
    def inspect(item, depth=0):
        if depth > 16:
            raise ValueError("数据层级过深")
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in {"apikey", "apikeybackup", "fallbackapikey", "accesstoken", "token", "authorization",
                                  "cookie", "password", "secret", "clientsecret", "credentials", "privatekey"}:
                    raise ValueError("监控记录不能保存密钥或凭据")
                inspect(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                inspect(child, depth + 1)
    inspect(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > 256 * 1024:
        raise ValueError("监控记录不能超过256KiB")
    return encoded


@contextmanager
def connection():
    with sqlite_bootstrap.connection(settings.data_dir / "monitoring.sqlite3", _SCHEMA, version=2) as conn:
        yield conn


def init_db():
    with connection():
        pass


def _state(conn, uid, key, default):
    row = conn.execute("SELECT payload FROM monitoring_state WHERE user_id=? AND key=?", (uid, key)).fetchone()
    return json.loads(row[0]) if row else default


def _set_state(conn, uid, key, value):
    conn.execute("INSERT INTO monitoring_state VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET payload=excluded.payload",
                 (uid, key, _json(value)))


def get_config(user_id):
    with connection() as conn:
        return {**DEFAULT_CONFIG, **_state(conn, _uid(user_id), "config", {})}


def update_config(user_id, patch):
    uid = _uid(user_id)
    value = ConfigPatch.model_validate(patch).model_dump(exclude_unset=True)
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        config = {**DEFAULT_CONFIG, **_state(conn, uid, "config", {}), **value}
        _set_state(conn, uid, "config", config)
        if not config["enabled"]:
            conn.execute("UPDATE monitoring_runs SET status='cancelled',finished_at=?,error_code='account_paused' WHERE user_id=? AND status='queued'",
                         (time.time(), uid))
        return config


def get_model_state(user_id):
    with connection() as conn:
        return _state(conn, _uid(user_id), "model", {})


def set_model_state(user_id, value):
    if not isinstance(value, dict):
        raise ValueError("模型状态须为对象")
    with connection() as conn:
        _set_state(conn, _uid(user_id), "model", value)
    return value


def get_worker_state():
    with connection() as conn:
        return _state(conn, 0, "worker", {})


def set_worker_state(value):
    if not isinstance(value, dict):
        raise ValueError("工作器状态须为对象")
    with connection() as conn:
        _set_state(conn, 0, "worker", value)
    return value


def _decode_plan(payload):
    value = json.loads(payload)
    # Earlier plans did not have list membership. Keep persisted revisions intact.
    value.setdefault("target_symbols", [])
    value.setdefault("channels", ["news", "price"])
    value.setdefault("research_link", None)
    return value


def _plan(conn, uid, plan_id):
    row = conn.execute("SELECT payload FROM monitoring_plans WHERE user_id=? AND id=?", (uid, plan_id)).fetchone()
    return _decode_plan(row[0]) if row else None


def _save_plan(conn, uid, plan):
    conn.execute("""INSERT INTO monitoring_plans VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,id) DO UPDATE SET
        revision=excluded.revision,status=excluded.status,next_run_at=excluded.next_run_at,
        review_at=excluded.review_at,payload=excluded.payload""",
        (uid, plan["id"], plan["revision"], plan["status"], plan["next_run_at"], plan["review_at"], _json(plan)))


def _revision(conn, uid, plan, kind, review=None):
    conn.execute("INSERT INTO monitoring_revisions VALUES(?,?,?,?,?,?,?)",
                 (uid, plan["id"], plan["revision"], kind, _json(plan), _json(review) if review is not None else None, plan["updated_at"]))


def create_plan(user_id, payload):
    uid, stamp = _uid(user_id), time.time()
    value = PlanCreate.model_validate(payload).model_dump(mode="json")
    plan = {**value, "id": uuid.uuid4().hex, "revision": 1, "created_at": stamp, "updated_at": stamp,
            "next_run_at": stamp, "last_run_at": None, "last_success_at": None, "last_error": None,
            "failures": 0, "last_review_run_date": None}
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if value.get("research_link"):
            for old in conn.execute("SELECT payload FROM monitoring_plans WHERE user_id=? AND status!='closed'", (uid,)):
                existing = _decode_plan(old[0])
                if all(existing.get(key) == value.get(key) for key in PlanCreate.model_fields):
                    return {**existing, "deduplicated": True}
        _save_plan(conn, uid, plan)
        _revision(conn, uid, plan, "created")
    return plan


def get_plan(user_id, plan_id):
    with connection() as conn:
        return _plan(conn, _uid(user_id), plan_id)


def list_plans(user_id, status=None, limit=200):
    if status not in {None, "active", "paused", "closed"}:
        raise ValueError("无效计划状态")
    with connection() as conn:
        rows = conn.execute("SELECT payload FROM monitoring_plans WHERE user_id=? AND (? IS NULL OR status=?) ORDER BY next_run_at,id LIMIT ?",
                            (_uid(user_id), status, status, max(1, min(1000, int(limit)))))
        return [_decode_plan(row[0]) for row in rows]


def _change_plan(conn, uid, plan, patch, kind, review=None):
    content = {key: plan[key] for key in PlanCreate.model_fields if key in plan}
    content.update(patch)
    content = PlanCreate.model_validate(content).model_dump(mode="json")
    stamp = time.time()
    value = {**plan, **content, "revision": plan["revision"] + 1, "updated_at": stamp,
             "next_run_at": stamp, "failures": 0, "last_error": None, "last_review_run_date": None}
    def target_key(item):
        return (item.get("target_type"), item.get("target_code"), tuple(sorted(item.get("target_symbols") or [])),
                tuple(sorted(item.get("keywords") or [])) if item.get("target_type") == "topic" else ())
    target_changed = target_key(plan) != target_key(value)
    if target_changed:
        value["research_link"] = None
    if target_changed or set(plan.get("channels") or ["news", "price"]) != set(value["channels"]):
        # Changing the companies establishes a new baseline, not a market alert.
        value.pop("last_assessment_signature", None)
        value.pop("last_evidence_signature", None)
        value.pop("last_evidence_keys", None)
    _save_plan(conn, uid, value)
    _revision(conn, uid, value, kind, review)
    conn.execute("UPDATE monitoring_runs SET status='cancelled',finished_at=?,error_code='plan_changed' WHERE user_id=? AND plan_id=? AND status='queued'",
                 (stamp, uid, plan["id"]))
    return value


def update_plan(user_id, plan_id, patch):
    uid = _uid(user_id)
    value = PlanPatch.model_validate(patch).model_dump(exclude_unset=True)
    revision = value.pop("revision")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        plan = _plan(conn, uid, plan_id)
        if plan is None:
            return None
        if plan["revision"] != revision:
            raise RevisionConflict("计划已有新版本，请重新载入")
        return _change_plan(conn, uid, plan, value, "updated")


def review_plan(user_id, plan_id, payload):
    uid = _uid(user_id)
    review = PlanReview.model_validate(payload).model_dump(mode="json", exclude_none=True)
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        plan = _plan(conn, uid, plan_id)
        if plan is None:
            return None
        if plan["revision"] != review["revision"]:
            raise RevisionConflict("计划已有新版本，请重新载入")
        if plan["status"] == "closed":
            raise PlanUnavailable("请先重新开启计划再复盘")
        patch = dict(review.get("changes") or {})
        patch["status"] = "closed" if review["action"] == "close" else plan["status"]
        if review.get("next_review_at"):
            patch["review_at"] = review["next_review_at"]
        return _change_plan(conn, uid, plan, patch, "reviewed", review)


def list_reviews(user_id, plan_id):
    with connection() as conn:
        return [{"id": f"{plan_id}:{row['revision']}", "plan_id": plan_id, "revision": row["revision"],
                 "review": json.loads(row["review"]), "plan_snapshot": json.loads(row["snapshot"]), "created_at": row["created_at"]}
                for row in conn.execute("SELECT * FROM monitoring_revisions WHERE user_id=? AND plan_id=? AND kind='reviewed' ORDER BY revision DESC",
                                        (_uid(user_id), plan_id))]


def _run(row):
    if row is None:
        return None
    value = dict(row)
    for key in ("plan_snapshot", "result"):
        value[key] = json.loads(value[key]) if value.get(key) is not None else None
    value["dispatched"] = value.get("dispatched_at") is not None
    return value


def _config(conn, uid):
    return {**DEFAULT_CONFIG, **_state(conn, uid, "config", {})}


def _enqueue(conn, uid, plan, kind, trigger, stamp):
    if not _config(conn, uid)["enabled"] or plan["status"] != "active":
        raise PlanUnavailable("账户或计划已暂停/结束")
    active = conn.execute("SELECT * FROM monitoring_runs WHERE user_id=? AND plan_id=? AND status IN ('queued','running')",
                          (uid, plan["id"])).fetchone()
    if active:
        if trigger == "manual" and active["kind"] != kind:
            raise PlanUnavailable("已有观察/复盘任务正在处理，请完成后再提交另一任务")
        return {**_run(active), "deduplicated": True}
    run_id = uuid.uuid4().hex
    conn.execute("""INSERT INTO monitoring_runs
        (id,user_id,plan_id,plan_revision,kind,trigger,status,queued_at,not_before,plan_snapshot)
        VALUES(?,?,?,?,?,?,'queued',?,?,?)""", (run_id, uid, plan["id"], plan["revision"], kind, trigger, stamp, stamp, _json(plan)))
    return _run(conn.execute("SELECT * FROM monitoring_runs WHERE id=?", (run_id,)).fetchone())


def enqueue_run(user_id, plan_id, kind="observe", trigger="manual", now=None):
    if kind not in {"observe", "review"} or trigger not in {"manual", "scheduled"}:
        raise ValueError("无效监控任务类型")
    uid, stamp = _uid(user_id), _now(now)
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        plan = _plan(conn, uid, plan_id)
        if plan is None:
            return None
        return _enqueue(conn, uid, plan, kind, trigger, stamp)


def _notify(conn, uid, plan, kind, title, summary, key, stamp, run_id=None):
    result = conn.execute("INSERT OR IGNORE INTO monitoring_notifications VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (uid, uuid.uuid4().hex, plan["id"], run_id, kind, title[:200], summary[:1200], stamp, None, key))
    return result.rowcount


def schedule_due(now=None):
    stamp = _now(now)
    today = datetime.fromtimestamp(stamp, SHANGHAI).date().isoformat()
    queued = notifications = 0
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for row in conn.execute("SELECT user_id,payload FROM monitoring_plans WHERE status='active' ORDER BY next_run_at,id").fetchall():
            uid, plan = row["user_id"], _decode_plan(row["payload"])
            if not _config(conn, uid)["enabled"]:
                continue
            due_review = plan["review_at"] <= today
            if due_review:
                notifications += _notify(conn, uid, plan, "review_due", "计划已到复盘日期", plan["title"],
                                         f"review:{plan['id']}:{plan['revision']}:{plan['review_at']}", stamp)
            if plan["next_run_at"] > stamp:
                continue
            kind = "review" if due_review and plan.get("last_review_run_date") != plan["review_at"] else "observe"
            run = _enqueue(conn, uid, plan, kind, "scheduled", stamp)
            queued += not run.get("deduplicated", False)
    return {"queued": queued, "review_notifications": notifications}


def _day(stamp):
    return datetime.fromtimestamp(stamp, SHANGHAI).date().isoformat()


def _used(conn, uid, stamp):
    return conn.execute("SELECT COUNT(*) FROM monitoring_calls WHERE user_id=? AND day=?", (uid, _day(stamp))).fetchone()[0]


def usage(user_id, now=None):
    uid, stamp = _uid(user_id), _now(now)
    with connection() as conn:
        limit = _config(conn, uid)["daily_call_limit"]
        used = _used(conn, uid, stamp)
        return {"day": _day(stamp), "timezone": "Asia/Shanghai", "used": used,
                "limit": limit, "remaining": max(0, limit - used)}


def _failure(conn, row, code, stamp):
    conn.execute("UPDATE monitoring_runs SET status='failed',finished_at=?,error_code=?,lease_until=NULL WHERE id=?",
                 (stamp, code, row["id"]))
    plan = _plan(conn, row["user_id"], row["plan_id"])
    if plan is None or plan["revision"] != row["plan_revision"] or plan["status"] != "active" or not _config(conn, row["user_id"])["enabled"]:
        return
    previous_failures = plan.get("failures", 0)
    failures = previous_failures + 1
    delay = max(plan["interval_minutes"] * 60, min(21600, 300 * 2 ** min(failures - 1, 8)))
    plan.update(failures=failures, last_error=code, last_run_at=stamp, next_run_at=stamp + delay)
    _save_plan(conn, row["user_id"], plan)
    if previous_failures == 0:
        _notify(conn, row["user_id"], plan, "error", "监控检查暂未完成", f"{plan['title']}：{code}",
                f"failure:{row['id']}", stamp, row["id"])


def _recover(conn, stamp, *, all_running=False):
    rows = conn.execute("SELECT * FROM monitoring_runs WHERE status='running'" + ("" if all_running else " AND lease_until<=?"),
                        () if all_running else (stamp,)).fetchall()
    recovered = unknown = 0
    for row in rows:
        if row["dispatched_at"] is not None:
            _failure(conn, row, "unknown_result", stamp)
            unknown += 1
            continue
        plan = _plan(conn, row["user_id"], row["plan_id"])
        if not plan or plan["status"] != "active" or plan["revision"] != row["plan_revision"] or not _config(conn, row["user_id"])["enabled"]:
            conn.execute("UPDATE monitoring_runs SET status='cancelled',finished_at=?,error_code='plan_changed',lease_until=NULL WHERE id=?",
                         (stamp, row["id"]))
        elif row["attempts"] >= 3:
            _failure(conn, row, "interrupted", stamp)
        else:
            conn.execute("UPDATE monitoring_runs SET status='queued',error_code='interrupted',lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE id=?", (row["id"],))
            recovered += 1
    return {"requeued": recovered, "unknown": unknown}


def recover_interrupted():
    """The caller must own the process-wide OS worker lock before recovery."""
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _recover(conn, time.time(), all_running=True)


def claim_run(owner, now=None):
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 200:
        raise ValueError("无效工作器标识")
    stamp = _now(now)
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _recover(conn, stamp)
        rows = conn.execute("SELECT * FROM monitoring_runs WHERE status='queued' AND not_before<=? ORDER BY attempts,queued_at,id", (stamp,)).fetchall()
        for row in rows:
            uid = row["user_id"]
            plan = _plan(conn, uid, row["plan_id"])
            config = _config(conn, uid)
            if not plan or plan["status"] != "active" or plan["revision"] != row["plan_revision"] or not config["enabled"]:
                conn.execute("UPDATE monitoring_runs SET status='cancelled',finished_at=?,error_code='plan_changed' WHERE id=?", (stamp, row["id"]))
                continue
            if _used(conn, uid, stamp) >= config["daily_call_limit"]:
                continue
            if conn.execute("SELECT 1 FROM monitoring_runs WHERE user_id=? AND status='running'", (uid,)).fetchone():
                continue
            token = uuid.uuid4().hex
            conn.execute("UPDATE monitoring_runs SET status='running',attempts=attempts+1,started_at=?,lease_owner=?,lease_token=?,lease_until=?,error_code=NULL WHERE id=?",
                         (stamp, owner, token, stamp + LEASE_SECONDS, row["id"]))
            return _run(conn.execute("SELECT * FROM monitoring_runs WHERE id=?", (row["id"],)).fetchone())
    return None


def _leased(conn, uid, run_id, token, stamp):
    row = conn.execute("SELECT * FROM monitoring_runs WHERE user_id=? AND id=? AND status='running' AND lease_token=? AND lease_until>?",
                       (uid, run_id, token, stamp)).fetchone()
    if row is None:
        raise LeaseLost("监控任务租约已失效")
    return row


def renew_run(user_id, run_id, token):
    uid, stamp = _uid(user_id), time.time()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _leased(conn, uid, run_id, token, stamp)
        conn.execute("UPDATE monitoring_runs SET lease_until=? WHERE user_id=? AND id=?", (stamp + LEASE_SECONDS, uid, run_id))
        return _run(conn.execute("SELECT * FROM monitoring_runs WHERE user_id=? AND id=?", (uid, run_id)).fetchone())


def reserve_call(user_id, *, run_id=None, kind="verify", now=None):
    uid, stamp = _uid(user_id), _now(now)
    if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", kind):
        raise ValueError("无效请求类型")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        config = _config(conn, uid)
        # Explicit connection verification can repair credentials while all
        # monitoring work remains paused. It still consumes the same daily cap.
        if not config["enabled"] and not (run_id is None and kind == "verify"):
            raise PlanUnavailable("账户监控已暂停")
        row = None
        if run_id is not None:
            row = conn.execute("SELECT * FROM monitoring_runs WHERE user_id=? AND id=? AND status='running' AND lease_until>?",
                               (uid, run_id, stamp)).fetchone()
            if row is None or row["dispatched_at"] is not None:
                raise PlanUnavailable("监控任务不可派发或已派发")
            plan = _plan(conn, uid, row["plan_id"])
            if not plan or plan["status"] != "active" or plan["revision"] != row["plan_revision"]:
                raise PlanUnavailable("计划已暂停、结束或修改")
        if _used(conn, uid, stamp) >= config["daily_call_limit"]:
            raise BudgetExceeded("已达到今日模型调用上限")
        attempt_id = uuid.uuid4().hex
        conn.execute("INSERT INTO monitoring_calls VALUES(?,?,?,?,?,?)", (attempt_id, uid, run_id, kind, _day(stamp), stamp))
        if row is not None:
            conn.execute("UPDATE monitoring_runs SET dispatched_at=? WHERE user_id=? AND id=?", (stamp, uid, run_id))
        return attempt_id


def _assessment_signature(result):
    report = result.get("report")
    if not isinstance(report, dict):
        return None
    # Narrative wording may change while the conclusion and condition states do not.
    conditions = report.get("conditions") or []
    normalized = []
    for condition in conditions:
        if isinstance(condition, dict):
            label = condition.get("id") or condition.get("condition") or condition.get("text") or condition.get("name")
            state = condition.get("status") or condition.get("state") or condition.get("assessment")
            normalized.append((str(label or ""), str(state or "")))
        elif isinstance(condition, str):
            normalized.append((condition, ""))
    payload = {"assessment": report.get("assessment"), "conditions": sorted(normalized)}
    members = report.get("members") or []
    if members:
        payload["members"] = sorted((str(member.get("symbol") or ""), str(member.get("assessment") or ""))
                                    for member in members if isinstance(member, dict))
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _evidence_signature(result):
    rows = result.get("evidence") or []
    # Cache-read time and ageing alone are not a new published item.
    payload = [{key: value for key, value in row.items() if key not in {"observed_at", "stale"}}
               for row in rows if isinstance(row, dict)]
    keys = sorted(hashlib.sha256(_json(row).encode()).hexdigest() for row in payload)
    return hashlib.sha256(_json(keys).encode()).hexdigest(), keys


def complete_run(user_id, run_id, token, result):
    uid, stamp = _uid(user_id), time.time()
    if not isinstance(result, dict):
        raise ValueError("监控结果须为对象")
    encoded = _json(result)
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _leased(conn, uid, run_id, token, stamp)
        conn.execute("UPDATE monitoring_runs SET status='succeeded',finished_at=?,result=?,error_code=NULL,lease_until=NULL WHERE id=?",
                     (stamp, encoded, run_id))
        plan = _plan(conn, uid, row["plan_id"])
        if plan and plan["revision"] == row["plan_revision"] and plan["status"] == "active" and _config(conn, uid)["enabled"]:
            signature = None if result.get("skipped_unchanged") else _assessment_signature(result)
            evidence_signature, evidence_keys = _evidence_signature(result)
            old_signature = plan.get("last_assessment_signature")
            if signature and old_signature is not None and signature != old_signature:
                report = result["report"]
                _notify(conn, uid, plan, "change", "观察结论或条件状态有变化", str(report.get("summary") or plan["title"]),
                        f"change:{run_id}", stamp, run_id)
            elif (not result.get("skipped_unchanged") and plan.get("last_evidence_signature")
                  and plan["last_evidence_signature"] != evidence_signature
                  and set(evidence_keys) - set(plan.get("last_evidence_keys") or [])):
                _notify(conn, uid, plan, "evidence_change", "监控资料出现新增或修订",
                        "已归档资料发生变化，请核对本次证据；资料变化本身不代表投资判断已被证实。",
                        f"evidence:{run_id}", stamp, run_id)
            if signature:
                plan["last_assessment_signature"] = signature
            plan["last_evidence_signature"] = evidence_signature
            plan["last_evidence_keys"] = evidence_keys
            plan.update(last_run_at=stamp, last_success_at=stamp, last_error=None, failures=0,
                        next_run_at=stamp + plan["interval_minutes"] * 60)
            if (row["kind"] == "review" and result.get("model_called") is True
                    and _day(stamp) >= plan["review_at"]):
                plan["last_review_run_date"] = plan["review_at"]
            _save_plan(conn, uid, plan)
        return _run(conn.execute("SELECT * FROM monitoring_runs WHERE user_id=? AND id=?", (uid, run_id)).fetchone())


def fail_run(user_id, run_id, token, error_code):
    uid, stamp = _uid(user_id), time.time()
    code = error_code if isinstance(error_code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) else "request_failed"
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _leased(conn, uid, run_id, token, stamp)
        _failure(conn, row, code, stamp)
        return _run(conn.execute("SELECT * FROM monitoring_runs WHERE user_id=? AND id=?", (uid, run_id)).fetchone())


def list_runs(user_id, plan_id=None, limit=50):
    with connection() as conn:
        return [_run(row) for row in conn.execute("SELECT * FROM monitoring_runs WHERE user_id=? AND (? IS NULL OR plan_id=?) ORDER BY queued_at DESC,id DESC LIMIT ?",
                                                (_uid(user_id), plan_id, plan_id, max(1, min(200, int(limit)))))]


def get_run(user_id, run_id):
    with connection() as conn:
        return _run(conn.execute("SELECT * FROM monitoring_runs WHERE user_id=? AND id=?", (_uid(user_id), run_id)).fetchone())


def _notification(row, handling=None):
    if row is None:
        return None
    value = dict(row)
    value["is_read"] = value.get("read_at") is not None
    value["handling_status"] = handling["action"] if handling else "pending"
    value["latest_handling"] = handling
    return value


def _handling(row, *, snapshot=False):
    value = dict(row)
    if snapshot:
        value["source_snapshot"] = json.loads(value["source_snapshot"])
    else:
        value.pop("source_snapshot", None)
    value.pop("user_id", None)
    value.pop("dedup_key", None)
    return value


def _last_handling(conn, uid, notification_id):
    row = conn.execute("SELECT * FROM monitoring_handling WHERE user_id=? AND notification_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                       (uid, notification_id)).fetchone()
    return _handling(row) if row else None


def list_notifications(user_id, unread_only=False):
    if type(unread_only) is not bool:
        raise ValueError("无效已读筛选")
    with connection() as conn:
        uid = _uid(user_id)
        return [_notification(row, _last_handling(conn, uid, row["id"])) for row in conn.execute("SELECT * FROM monitoring_notifications WHERE user_id=? AND (?=0 OR read_at IS NULL) ORDER BY created_at DESC,id DESC LIMIT 200",
                                                         (uid, int(unread_only)))]


def mark_notification(user_id, notification_id, read=True):
    if type(read) is not bool:
        raise ValueError("已读状态须为布尔值")
    uid = _uid(user_id)
    with connection() as conn:
        conn.execute("UPDATE monitoring_notifications SET read_at=? WHERE user_id=? AND id=?", (time.time() if read else None, uid, notification_id))
        return _notification(conn.execute("SELECT * FROM monitoring_notifications WHERE user_id=? AND id=?", (uid, notification_id)).fetchone(),
                             _last_handling(conn, uid, notification_id))


def list_handling(user_id, plan_id):
    with connection() as conn:
        return [_handling(row, snapshot=True) for row in conn.execute(
            "SELECT * FROM monitoring_handling WHERE user_id=? AND plan_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100",
            (_uid(user_id), plan_id))]


def record_handling(user_id, notification_id, payload):
    uid, value = _uid(user_id), HandlingCreate.model_validate(payload).model_dump(mode="json")
    key = hashlib.sha256(_json(value).encode()).hexdigest()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        notification = conn.execute("SELECT * FROM monitoring_notifications WHERE user_id=? AND id=?", (uid, notification_id)).fetchone()
        if notification is None:
            return None
        run_id = notification["run_id"]
        if value.get("run_id") and value["run_id"] != run_id:
            raise ValueError("处理记录须对应提醒原有的任务")
        run = _run(conn.execute("SELECT * FROM monitoring_runs WHERE user_id=? AND id=?", (uid, run_id)).fetchone()) if run_id else None
        if value["action"] == "research" and (not run or run["status"] != "succeeded"):
            raise ValueError("仅成功任务的变化证据可以转入研究")
        if value["action"] == "research":
            from app.storage import investment_research_store
            materials = investment_research_store.list_materials(uid, value["dossier_id"])
            if not any(material.get("monitor_run_id") == run_id for material in materials):
                raise PlanUnavailable("请先把此提醒的监控证据导入所选研究，再记录转入研究")
        snapshot = {"notification": _notification(notification), "plan_snapshot": run["plan_snapshot"] if run else None,
                    "evidence": ((run or {}).get("result") or {}).get("evidence", []),
                    "report": ((run or {}).get("result") or {}).get("report")}
        conn.execute("INSERT OR IGNORE INTO monitoring_handling VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (uuid.uuid4().hex, uid, notification_id, notification["plan_id"], run_id, value["action"],
                      value["reason"], value.get("dossier_id"), time.time(), key, _json(snapshot)))
        return _notification(notification, _last_handling(conn, uid, notification_id))
