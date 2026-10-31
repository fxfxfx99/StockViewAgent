"""Auditable regional investment clocks and a durable weekly worker.

The deterministic cycle is independent of the model. Public observations are
shared; preferences, jobs and generated interpretation are account-scoped.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
import logging
import math
import time
import uuid
from zoneinfo import ZoneInfo

from app.config import settings
from app.security.user_context import current_user_id
from app.services import llm_client
from app.storage import merrill_store as store
from app.storage import users_store

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
REGIONS = {"US": ("美国宏观锚点", "INDPRO", "CPIAUCSL"),
           "CN": ("中国宏观锚点", "CN_PMI", "CN_CPI_YOY")}
PHASES = [
    {"id": "recovery", "label": "复苏", "growth": "上行", "inflation": "下行", "preferred_asset": "股票", "description": "增长改善、通胀降温，企业盈利与风险偏好往往改善。"},
    {"id": "overheat", "label": "过热", "growth": "上行", "inflation": "上行", "preferred_asset": "商品", "description": "增长和通胀同时走强，实物需求与成本压力上升。"},
    {"id": "stagflation", "label": "滞胀", "growth": "下行", "inflation": "上行", "preferred_asset": "现金", "description": "增长放缓但通胀抬升，风险资产和久期资产可能同时承压。"},
    {"id": "reflation", "label": "衰退", "growth": "下行", "inflation": "下行", "preferred_asset": "债券", "description": "增长与通胀同时回落，降息预期可能支持高质量债券。"},
]
METHOD = "先取月度同比；最新三个完整月份同比均值减去此前三个月同比均值（百分点）。横轴通胀动量，右为上行；纵轴增长动量，上为上行。±0.05个百分点以内视为过渡区，不强行定位。"
CN_METHOD = "增长轴固定使用中国制造业 PMI 原始月度水平：最近三个月均值减此前三个月均值（PMI点）；通胀轴使用 CPI 月度同比的相同三个月均值差（百分点）。横轴右为通胀动量上行、纵轴上为增长动量上行；各轴±0.05单位以内视为过渡区。PMI 方向变化不等于是否高于50荣枯线。"
LIMITATIONS = [
    "这是美林时钟思想的透明规则实现，并非美林或 RLAM 官方时钟；增长指标是宏观代理，不等同 GDP。",
    "月度数据有发布滞后和事后修订；图中月份是统计期，不是发布日期，抓取时间也不是发布日期。未取得逐条历史发布日，不支持实时历史回测。",
    "阶段不是经济衰退的官方认定，也不预测下一阶段；理论中的资产偏好是历史概括，不保证收益。",
]
THEORY = {"title": "美林时钟如何读", "intro": "以增长与通胀的变化方向划分四个宏观环境，并观察股票、商品、现金与债券的相对表现。周期可以跳跃或逆转。", "phases": PHASES,
          "methodology": METHOD, "limitations": LIMITATIONS,
          "sources": [{"title": "Royal London Asset Management · Investment Clock", "url": "https://www.rlam.com/uk/intermediaries/our-capabilities/multi-asset/investment-clock/"},
                      {"title": "FRED · 美国工业生产 INDPRO", "url": "https://fred.stlouisfed.org/series/INDPRO"},
                      {"title": "FRED · 美国 CPIAUCSL", "url": "https://fred.stlouisfed.org/series/CPIAUCSL"},
                      {"title": "国家统计局 · PMI 指标与调查方法说明", "url": "https://www.stats.gov.cn/zwfwck/sjfb/202609/t20260930_1965449.html"},
                      {"title": "国家统计局 · 月度数据", "url": "https://data.stats.gov.cn/easyquery.htm?cn=A01"}]}
_JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS merrill_jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, user_id INTEGER NOT NULL, region TEXT NOT NULL,
 dedupe TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, available_at REAL NOT NULL,
 error TEXT, UNIQUE(kind,user_id,region,dedupe));
CREATE INDEX IF NOT EXISTS merrill_job_poll ON merrill_jobs(status,available_at);
CREATE TABLE IF NOT EXISTS merrill_preferences (user_id INTEGER PRIMARY KEY, auto_analysis INTEGER NOT NULL DEFAULT 0);
"""


class Conflict(ValueError):
    pass


class RateLimit(ValueError):
    pass


def _iso(stamp=None):
    return datetime.fromtimestamp(time.time() if stamp is None else stamp, timezone.utc).isoformat()


def _region(region):
    if region not in REGIONS:
        raise ValueError("地区必须为 US 或 CN")
    return region


def _month(day: date, delta=0):
    number = day.year * 12 + day.month - 1 + delta
    return date(number // 12, number % 12 + 1, 1)


def _axis(rows, sid, label, *, direct_yoy, anchor, today, direct_level=False):
    values = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row["date"])[:10]).replace(day=1)
            value = float(row["value"])
            if math.isfinite(value) and day < _month(today) and (direct_yoy or value > 0):
                values[day] = value
        except (KeyError, ValueError, TypeError):
            continue
    observations = []
    for offset in range(-5, 1):
        day = _month(anchor, offset)
        value = values.get(day)
        base = values.get(_month(day, -12))
        if value is None or (not direct_yoy and not direct_level and not base):
            return None
        metric = value if direct_yoy or direct_level else (value / base - 1) * 100
        observations.append({"date": day.isoformat(), "value": value, "metric": round(metric, 6),
                             "yoy": None if direct_level else round(metric, 6),
                             "year_ago_value": None if direct_yoy or direct_level else base})
    recent = sum(o["metric"] for o in observations[3:]) / 3
    previous = sum(o["metric"] for o in observations[:3]) / 3
    momentum = recent - previous
    return {"series_id": sid, "label": label, "latest_date": anchor.isoformat(),
            "latest_value": observations[-1]["value"], "yoy": observations[-1]["yoy"],
            "latest_metric": observations[-1]["metric"], "value_label": "PMI" if direct_level else "同比",
            "transform": "level" if direct_level else "reported_yoy" if direct_yoy else "yoy",
            "recent_mean": round(recent, 6), "previous_mean": round(previous, 6),
            "momentum": round(momentum, 6), "unit": "PMI点" if direct_level else "同比百分比 / 动量百分点",
            "momentum_unit": "PMI点" if direct_level else "百分点",
            "direction": "up" if momentum > .05 else "down" if momentum < -.05 else "flat",
            "observations": observations, "source_url": f"https://fred.stlouisfed.org/series/{sid}" if not direct_yoy and not direct_level else "https://data.stats.gov.cn/easyquery.htm?cn=A01"}


def compute_cycle(macro, region="US", *, now=None, metadata=None):
    """Never interpolate missing months or let a model choose the quadrant."""
    _region(region)
    instant = now or datetime.now(timezone.utc)
    today = instant.astimezone(SHANGHAI).date()
    label, growth_id, inflation_id = REGIONS[region]
    result = {"status": "insufficient", "phase": None, "phase_label": "数据不足",
              "as_of": None, "calculated_at": instant.isoformat(), "region_code": region,
              "region": label, "growth": None, "inflation": None, "position": None,
              "methodology": METHOD if region == "US" else CN_METHOD, "limitations": list(LIMITATIONS)}
    if region == "US":
        result["limitations"].append("美国使用季调 INDPRO 与季调 CPIAUCSL 的同比；CPI 口径与常见非季调官方同比可能略有差异。")
    else:
        result["limitations"].append("中国增长轴固定采用制造业 PMI 水平动量；CPI 使用已公布同比。PMI 高于50表示扩张、低于50表示收缩，但本时钟判定的是其改善/走弱方向，不是50水平。工业增加值同比仅作参考，不参与定位。")
    sets = []
    for sid in (growth_id, inflation_id):
        months = set()
        for row in macro.get(sid, []):
            try:
                day = date.fromisoformat(str(row["date"])[:10]).replace(day=1)
                value = float(row["value"])
                if day < _month(today) and math.isfinite(value) and (region == "CN" or value > 0):
                    months.add(day)
            except (KeyError, ValueError, TypeError):
                pass
        sets.append(months)
    common = sets[0] & sets[1]
    if not common:
        result["limitations"].append("尚无两个指标共同可用的统计月份。")
        return result
    anchor = max(common)
    result["as_of"] = anchor.isoformat()
    result["growth"] = _axis(macro.get(growth_id, []), growth_id, "制造业 PMI" if region == "CN" else "工业产出同比", direct_yoy=False, direct_level=region == "CN", anchor=anchor, today=today)
    result["inflation"] = _axis(macro.get(inflation_id, []), inflation_id, "CPI 同比", direct_yoy=region == "CN", anchor=anchor, today=today)
    for axis in ("growth", "inflation"):
        if result[axis]:
            provenance = (metadata or {}).get(result[axis]["series_id"], {})
            if provenance.get("source_url"):
                result[axis]["source_url"] = provenance["source_url"]
            if provenance.get("source"):
                result[axis]["source"] = provenance["source"]
            result[axis]["observation_unit"] = provenance.get("unit", "PMI点" if region == "CN" and axis == "growth" else "%" if region == "CN" else "指数点")
    if region == "CN":
        reference = [row for row in macro.get("CN_INDUSTRY_YOY", []) if row.get("date", "") <= anchor.isoformat()]
        if reference:
            latest = max(reference, key=lambda row: row["date"])
            result["reference_indicators"] = [{"series_id": "CN_INDUSTRY_YOY", "label": "工业增加值当月同比（仅供参考）",
                                               "date": latest["date"], "value": latest["value"], "unit": "%"}]
    if not result["growth"] or not result["inflation"]:
        result["limitations"].append("需要连续六个月完整观测；美国同比还需对应去年基期。缺月不会插值或跨月拼接。")
        return result
    period_end = _month(anchor, 1) - timedelta(days=1)
    result["observation_age_days"] = max(0, (today - period_end).days)
    if result["observation_age_days"] > 75:
        result.update(status="stale", phase_label="宏观数据已过期")
        result["limitations"].append("共同统计期距今超过 75 天，暂停当前阶段定位。")
        return result
    for sid in (growth_id, inflation_id):
        health = (metadata or {}).get(sid, {})
        fetched = health.get("last_synced_at")
        if fetched and instant.timestamp() - fetched > 14 * 86400:
            result.update(status="stale", phase_label="宏观来源待更新")
            result["limitations"].append("宏观来源超过 14 天未成功同步，暂停当前阶段定位。")
            return result
        if health.get("last_error"):
            result["limitations"].append(f"{sid} 最近抓取失败；当前计算使用上次保存的观测。")
    growth = result["growth"]["direction"]
    inflation = result["inflation"]["direction"]
    if "flat" in {growth, inflation}:
        result.update(status="transition", phase_label="阶段过渡区")
        return result
    phase = {("up", "down"): "recovery", ("up", "up"): "overheat", ("down", "up"): "stagflation", ("down", "down"): "reflation"}[(growth, inflation)]
    result.update(status="ready", phase=phase, phase_label=next(p["label"] for p in PHASES if p["id"] == phase),
                  position={"x": round(math.tanh(result["inflation"]["momentum"] / 2), 6), "y": round(math.tanh(result["growth"]["momentum"] / 2), 6)})
    result["position_note"] = "节点坐标为动量经 tanh(动量/2) 压缩；只表示方向与相对强弱，不是概率或预测。"
    return result


def _fingerprint(cycle, assets=None):
    fields = {k: cycle.get(k) for k in ("region_code", "phase", "as_of", "growth", "inflation", "reference_indicators")}
    if assets is not None:
        fields["assets"] = assets
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def asset_evidence():
    """Small observed snapshot; rates are changes in bp, never price returns."""
    catalog = store.catalog()
    today = datetime.now(SHANGHAI).date()
    series_by_id = {s["id"]: s for s in store.get_series([a["id"] for a in catalog], start=(today - timedelta(days=200)).isoformat(), end=today.isoformat())}
    result = []
    for asset in catalog:
        points = series_by_id.get(asset["id"], {}).get("points", [])
        latest = points[-1] if points else None
        snapshot = {k: asset.get(k) for k in ("id", "name", "kind", "unit", "source", "source_url", "available_from", "available_to", "proxy_note")}
        snapshot.update(latest=latest, change_1w=None, change_13w=None,
                        change_unit="bp" if asset["kind"] == "yield" else "%",
                        stale=latest is None or (today - date.fromisoformat(latest["observation_date"])).days > 14)
        if latest:
            by_date = {p["date"]: p for p in points}
            for weeks in (1, 13):
                earlier = by_date.get((date.fromisoformat(latest["date"]) - timedelta(weeks=weeks)).isoformat())
                if earlier:
                    old, new = earlier["close"], latest["close"]
                    value = (new - old) * 100 if asset["kind"] == "yield" else ((new / old - 1) * 100 if old > 0 else None)
                    snapshot[f"change_{weeks}w"] = round(value, 4) if value is not None else None
        result.append(snapshot)
    return result


@contextmanager
def _connection():
    with store.connection() as conn:
        conn.executescript(_JOB_SCHEMA)
        yield conn


def preferences(uid):
    with _connection() as conn:
        row = conn.execute("SELECT auto_analysis FROM merrill_preferences WHERE user_id=?", (uid,)).fetchone()
    return {"auto_analysis": bool(row[0]) if row else False}


def set_preferences(uid, auto_analysis):
    with _connection() as conn:
        conn.execute("INSERT INTO merrill_preferences VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET auto_analysis=excluded.auto_analysis", (uid, int(auto_analysis)))
        if not auto_analysis:
            conn.execute("UPDATE merrill_jobs SET status='cancelled',updated_at=? WHERE user_id=? AND kind='analysis' AND status='queued' AND dedupe LIKE 'weekly:%'", (time.time(), uid))
    return preferences(uid)


def model_status(uid):
    token = current_user_id.set(uid)
    try:
        return {"configured": settings.any_llm_key_configured, "model": settings.kline_llm_model}
    finally:
        current_user_id.reset(token)


def schedule_window(now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(SHANGHAI)
    due = (local - timedelta(days=(local.weekday() - 5) % 7)).replace(hour=8, minute=0, second=0, microsecond=0)
    if due > local:
        due -= timedelta(days=7)
    return due, due + timedelta(days=7)


def _public_job(row):
    if not row:
        return None
    item = dict(row)
    return {key: (_iso(value) if key.endswith("_at") else value) for key, value in item.items() if key not in {"user_id", "dedupe"}}


def latest_job(kind, uid=0, region=""):
    with _connection() as conn:
        row = conn.execute("SELECT * FROM merrill_jobs WHERE kind=? AND user_id=? AND region=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (kind, uid, region)).fetchone()
    return _public_job(row)


def _enqueue(kind, uid, region, dedupe, *, now=None, manual=False):
    stamp = time.time() if now is None else now
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM merrill_jobs WHERE kind=? AND user_id=? AND region=? AND (dedupe=? OR status IN ('queued','running')) ORDER BY created_at DESC LIMIT 1", (kind, uid, region, dedupe)).fetchone()
        if existing:
            return _public_job(existing)
        if manual:
            seconds, maximum = (3600, 1) if kind == "refresh" else (86400, 2)
            count = conn.execute("SELECT count(*) FROM merrill_jobs WHERE kind=? AND user_id=? AND created_at>? AND dedupe LIKE 'manual:%'", (kind, uid, stamp - seconds)).fetchone()[0]
            if count >= maximum:
                raise RateLimit("公共采集每小时最多手动一次" if kind == "refresh" else "每账户每天最多手动解读两次（中美合计）")
        jid = str(uuid.uuid4())
        conn.execute("INSERT INTO merrill_jobs(id,kind,user_id,region,dedupe,status,created_at,updated_at,available_at) VALUES(?,?,?,?,?,'queued',?,?,?)", (jid, kind, uid, region, dedupe, stamp, stamp, stamp))
        return _public_job(conn.execute("SELECT * FROM merrill_jobs WHERE id=?", (jid,)).fetchone())


def enqueue_refresh():
    return _enqueue("refresh", 0, "", "manual:" + str(uuid.uuid4()), manual=True)


def enqueue_analysis(uid, region="US"):
    _region(region)
    if not model_status(uid)["configured"]:
        raise Conflict("请先在控制台配置当前账户的大模型接口")
    cycle = compute_cycle(store.get_macro(), region, metadata=store.get_state("macro_metadata", {}))
    if cycle["status"] != "ready":
        raise Conflict("当前地区宏观数据不足、过期或处于过渡区，暂不能生成阶段解读")
    return _enqueue("analysis", uid, region, "manual:" + str(uuid.uuid4()), manual=True)


def overview(uid, region="US"):
    _region(region)
    cycle = compute_cycle(store.get_macro(), region, metadata=store.get_state("macro_metadata", {}))
    model = model_status(uid)
    prefs = preferences(uid)
    analysis = store.get_analysis(uid, region=region) or {}
    job = latest_job("analysis", uid, region)
    default = {"status": "not_generated" if model["configured"] else "not_configured", "text": None,
               "generated_at": None, "model": None, "error": None, "as_of": None}
    analysis = {**default, **analysis}
    if analysis.get("text"):
        analysis["status"] = "ready" if cycle["status"] == "ready" and analysis.get("fingerprint") == _fingerprint(cycle, asset_evidence()) else "stale"
    if job and job["status"] in {"queued", "running", "failed"}:
        analysis.update(status=job["status"], error=job["error"])
    analysis.pop("fingerprint", None)
    analysis["job"] = job
    last_seen = store.get_state("merrill_worker_seen", 0) or 0
    _, next_run = schedule_window()
    return {"catalog": store.catalog(), "cycle": cycle, "analysis": analysis,
            "automation": {"enabled": bool(getattr(settings, "enable_merrill_clock", True)), **prefs,
                           "llm_configured": model["configured"], "schedule": "每周六 08:00（北京时间）", "next_run_at": next_run.isoformat(),
                           "worker_running": time.time() - float(last_seen) < 120, "scope_regions": ["US", "CN"],
                           "limits": "每地区每周自动解读一次；失败最多重试三轮；每账户每天最多手动解读两次"},
            "sync": {**store.get_state("sync_status", {}), "job": latest_job("refresh")},
            "macro_sources": {sid: store.get_state("macro_metadata", {}).get(sid, {}) for sid in REGIONS[region][1:]},
            "theory": {**THEORY, "methodology": METHOD if region == "US" else CN_METHOD}}


def series(ids, years):
    known = {item["id"] for item in store.catalog()}
    chosen = list(dict.fromkeys(ids))
    if not chosen or len(chosen) > 20 or set(chosen) - known:
        raise ValueError("请选择目录内 1 到 20 个资产")
    today = datetime.now(SHANGHAI).date()
    try:
        start = today.replace(year=today.year - years)
    except ValueError:
        start = today.replace(year=today.year - years, day=28)
    return {"series": store.get_series(chosen, start=start.isoformat(), end=today.isoformat())}


def _weekly_data_complete(due_stamp):
    """Reconcile individual source successes, including several targeted runs.

    The last sync record describes one invocation; a successful one-asset retry
    is not proof that the other assets or either clock's inputs were refreshed.
    """
    from app.services.merrill_sources import MACRO_CATALOG

    def fresh(item):
        return bool(item.get("last_synced_at") and item["last_synced_at"] >= due_stamp
                    and not item.get("last_error") and item.get("row_count", 0) > 0)

    assets = {item["id"]: item for item in store.catalog()}
    expected_assets = {item["id"] for item in store.ASSET_CATALOG}
    metadata = store.get_state("macro_metadata", {}) or {}
    return (expected_assets <= assets.keys() and all(fresh(assets[sid]) for sid in expected_assets)
            and all(fresh(metadata.get(sid, {})) for sid in MACRO_CATALOG))


def _schedule_jobs(now=None):
    stamp = time.time() if now is None else now
    due, _ = schedule_window(datetime.fromtimestamp(stamp, timezone.utc))
    key = "weekly:" + due.date().isoformat()
    _enqueue("refresh", 0, "", key, now=stamp)
    sync = store.get_state("sync_status", {}) or {}
    synced_this_week = bool(sync.get("finished_at") and sync["finished_at"] >= due.timestamp())
    all_sources_complete = _weekly_data_complete(due.timestamp())
    with _connection() as conn:
        # Successful CLI backfills/retries satisfy the weekly job only after all
        # 20 assets and all monthly inputs have actually succeeded this week.
        # Keep the attempts for audit, but retire pending/exhausted retries.
        if all_sources_complete:
            conn.execute("UPDATE merrill_jobs SET status='success',error=NULL,updated_at=? WHERE kind='refresh' AND dedupe=? AND status IN ('queued','failed')", (stamp, key))
        # Wait for this week's public job to settle before capturing macro evidence.
        refresh = conn.execute("SELECT status FROM merrill_jobs WHERE kind='refresh' AND dedupe=?", (key,)).fetchone()
        accounts = [row[0] for row in conn.execute("SELECT user_id FROM merrill_preferences WHERE auto_analysis=1")]
    # A partial fetch can leave an asset source unavailable while its region's
    # macro inputs are sound. Model evidence explicitly identifies those gaps.
    if not refresh or (refresh[0] not in {"success", "failed"} and not synced_this_week):
        return
    macro = store.get_macro()
    for uid in accounts:
        if not users_store.get_by_id(uid) or not model_status(uid)["configured"]:
            continue
        for region in REGIONS:
            if compute_cycle(macro, region, now=datetime.fromtimestamp(stamp, timezone.utc), metadata=store.get_state("macro_metadata", {}))["status"] == "ready":
                _enqueue("analysis", uid, region, key, now=stamp)


async def _drain_call(function, *args, **kwargs):
    """A cancelled coroutine must not release the lock over a running HTTP thread."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
        # Consume the result/exception while preserving cancellation of the worker.
        if not task.cancelled():
            task.exception()
        raise


async def _analyze(job):
    uid, region = job["user_id"], job["region"]
    if not users_store.get_by_id(uid):
        raise Conflict("账户已不存在")
    persisted = store.get_analysis(uid, region=region)
    if job.get("id") and persisted and persisted.get("job_id") == job["id"]:
        # A process may stop after publication but before finalizing its queue
        # row. Replaying that row must not charge for an already saved report.
        return
    if job["dedupe"].startswith("weekly:") and not preferences(uid)["auto_analysis"]:
        raise Conflict("本账户已关闭自动解读")
    cycle = compute_cycle(store.get_macro(), region, metadata=store.get_state("macro_metadata", {}))
    if cycle["status"] != "ready":
        raise Conflict("当前宏观数据不能定位阶段")
    if not model_status(uid)["configured"]:
        raise Conflict("当前账户尚未配置大模型")
    token = current_user_id.set(uid)
    try:
        assets = asset_evidence()
        evidence = {"cycle": cycle, "assets": assets}
        messages = [{"role": "system", "content": "你是宏观数据解释助手。仅解释提供的确定性规则结果，不得修改阶段，不得推测未提供的实时事件或行情。清楚区分统计期与发布日期、趋势与预测。说明该地区增长/通胀动量、理论资产表现与已观察行情是否一致及局限；收益率变化单位为bp，不能当债券回报，缺失和stale资料不能描述为当前事实；不是个性化投资建议。只返回JSON对象：phase（必须原样等于输入cycle.phase），text（中文300至600字）。"},
                    {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}]
        raw, error = await _drain_call(llm_client.chat_completion_sync, messages, temperature=.2, timeout=60)
        if error or not raw:
            raise RuntimeError(llm_client.safe_error(error or "LLM 返回空内容"))
        try:
            parsed = llm_client.parse_json_response(raw)
            if not isinstance(parsed, dict) or parsed.get("phase") != cycle["phase"] or not isinstance(parsed.get("text"), str) or len(parsed["text"].strip()) < 80:
                raise ValueError("invalid")
        except (ValueError, TypeError):
            raise RuntimeError("LLM 响应格式无效") from None
        payload = {"status": "ready", "text": parsed["text"].strip()[:5000], "generated_at": _iso(),
                   "model": settings.kline_llm_model, "as_of": cycle["as_of"], "phase": cycle["phase"],
                   "region": region, "job_id": job.get("id"), "fingerprint": _fingerprint(cycle, assets), "evidence": evidence, "error": None}
        await _drain_call(store.save_analysis, uid, payload, region=region)
    finally:
        current_user_id.reset(token)


def _recover_and_claim():
    now = time.time()
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # File-lock ownership proves there can be no live worker for these rows.
        conn.execute("UPDATE merrill_jobs SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END, available_at=?,updated_at=?,error='服务中断后恢复任务' WHERE status='running'", (now + 60, now))
        row = conn.execute("SELECT * FROM merrill_jobs WHERE status='queued' AND available_at<=? ORDER BY CASE kind WHEN 'refresh' THEN 0 ELSE 1 END,created_at LIMIT 1", (now,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE merrill_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=?", (now, row["id"]))
        return dict(conn.execute("SELECT * FROM merrill_jobs WHERE id=?", (row["id"],)).fetchone())


def _finish(job, error=None, *, retry=True):
    now = time.time()
    queued = error is not None and retry and job["attempts"] < 3
    with _connection() as conn:
        conn.execute("UPDATE merrill_jobs SET status=?,error=?,updated_at=?,available_at=? WHERE id=?", (
            "queued" if queued else "failed" if error else "success", error, now,
            now + 300 * 2 ** max(0, job["attempts"] - 1) if queued else now, job["id"]))


async def run_once():
    """One bounded task under a process-wide OS lock, including cancellation drain."""
    if not getattr(settings, "enable_merrill_clock", True):
        return False
    with (settings.data_dir / "merrill_worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        token = current_user_id.set(0)
        async def heartbeat():
            while True:
                store.set_state("merrill_worker_seen", time.time())
                await asyncio.sleep(30)
        pulse = asyncio.create_task(heartbeat())
        try:
            store.set_state("merrill_worker_seen", time.time())
            _schedule_jobs()
            job = _recover_and_claim()
            if not job:
                return False
            try:
                if job["kind"] == "refresh":
                    from app.services.merrill_sources import sync_all
                    result = await sync_all(full=False)
                    if result.get("status") in {"failed", "error", "partial"} or result.get("errors"):
                        raise RuntimeError("部分公共数据源未更新，保留既有数据并延迟重试")
                else:
                    await _analyze(job)
            except asyncio.CancelledError:
                _finish(job, "服务停止，任务已保留", retry=True)
                raise
            except Conflict as exc:
                _finish(job, str(exc), retry=False)
            except Exception as exc:
                if job["kind"] == "analysis":
                    message = llm_client.safe_error(str(exc))
                    retry = llm_client.is_retryable_error(message)
                else:
                    message, retry = "部分公共数据源未更新，保留既有数据并延迟重试", True
                logger.warning("美林时钟 %s 任务失败：%s", job["kind"], type(exc).__name__)
                _finish(job, message, retry=retry)
            else:
                _finish(job)
            return True
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            current_user_id.reset(token)
            fcntl.flock(lock, fcntl.LOCK_UN)


async def worker_loop():
    while True:
        try:
            worked = await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("美林时钟后台本轮失败")
            worked = False
        await asyncio.sleep(2 if worked else 30)
