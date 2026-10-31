"""Cycle identity, account boundaries, retry policy and worker cancellation."""
import asyncio
from datetime import date, datetime, timezone
import fcntl
import json
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.deps.auth import get_current_user
from app.routers import merrill_clock as router
from app.security.user_context import current_user_id
from app.services import merrill_clock as service
from app.storage import merrill_store as store

NOW = datetime(2026, 10, 31, 4, tzinfo=timezone.utc)


def macro(growth=1, inflation=-1, *, region="US"):
    rows = {}
    for sid, direction in zip(service.REGIONS[region][1:], (growth, inflation)):
        values = []
        for index in range(18):
            day = service._month(date(2026, 9, 1), index - 17)
            rate = 3 + (index - 12) * direction if index >= 12 else 0
            value = (50 + rate if sid == "CN_PMI" else rate) if region == "CN" else 100 + rate
            values.append({"date": day.isoformat(), "value": value})
        rows[sid] = values
    return rows


@pytest.mark.parametrize("g,i,phase", [(1,-1,"recovery"),(1,1,"overheat"),(-1,1,"stagflation"),(-1,-1,"reflation")])
def test_four_quadrants_and_reproducible_axis_values(g,i,phase):
    cycle = service.compute_cycle(macro(g,i), now=NOW)
    assert cycle["status"] == "ready" and cycle["phase"] == phase
    assert cycle["growth"]["momentum"] == g * 3
    assert cycle["inflation"]["momentum"] == i * 3
    assert cycle["growth"]["observations"][-1]["year_ago_value"] == 100
    assert (cycle["position"]["x"] > 0) == (i > 0)
    assert (cycle["position"]["y"] > 0) == (g > 0)


def test_cn_uses_fixed_pmi_levels_cpi_yoy_and_never_bridges_missing_month():
    evidence = macro(region="CN")
    cycle = service.compute_cycle(evidence, "CN", now=NOW)
    assert cycle["growth"]["yoy"] is None and cycle["growth"]["latest_metric"] == 58
    assert cycle["growth"]["transform"] == "level" and cycle["growth"]["momentum_unit"] == "PMI点"
    assert cycle["inflation"]["transform"] == "reported_yoy"
    assert cycle["growth"]["momentum"] == 3
    assert cycle["phase"] == "recovery"
    evidence["CN_PMI"] = [r for r in evidence["CN_PMI"] if r["date"] != "2026-06-01"]
    assert service.compute_cycle(evidence, "CN", now=NOW)["status"] == "insufficient"


def test_stale_transition_and_future_input_do_not_claim_current_phase():
    stale = service.compute_cycle(macro(), now=datetime(2027, 2, 1, tzinfo=timezone.utc))
    assert stale["status"] == "stale" and stale["phase"] is None and stale["position"] is None
    assert service.compute_cycle(macro(0,1), now=NOW)["status"] == "transition"
    data = macro()
    for rows in data.values():
        rows.extend([{"date":"2026-10-01","value":99999},{"date":"2026-11-01","value":999999}])
    assert service.compute_cycle(data, now=NOW)["as_of"] == "2026-09-01"
    data["INDPRO"][-3]["value"] = float("nan")
    assert service.compute_cycle(data, now=NOW)["as_of"] == "2026-08-01"


def test_beijing_schedule_before_after_saturday_and_restart_dedupe(monkeypatch):
    before = datetime(2026,10,30,23,59,tzinfo=timezone.utc)
    after = datetime(2026,10,31,0,0,tzinfo=timezone.utc)
    assert service.schedule_window(before)[0].date().isoformat() == "2026-10-24"
    assert service.schedule_window(after)[0].date().isoformat() == "2026-10-31"
    service._schedule_jobs(after.timestamp())
    service._schedule_jobs(after.timestamp() + 10)
    with service._connection() as conn:
        assert conn.execute("SELECT count(*) FROM merrill_jobs").fetchone()[0] == 1
    assert service.latest_job("refresh")["status"] == "queued"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "enable_merrill_clock", True)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1,role="user")
    monkeypatch.setattr(service, "model_status", lambda uid: {"configured":False,"model":"test"})
    with TestClient(app) as c:
        yield c


def test_refresh_is_admin_only_reads_do_not_enqueue_and_preferences_private(client, monkeypatch):
    assert client.get("/api/merrill-clock").status_code == 200
    assert service.latest_job("refresh") is None
    assert client.post("/api/merrill-clock/refresh").status_code == 403
    assert client.post("/api/merrill-clock/analyze",json={"region":"US"}).status_code == 409
    assert client.put("/api/merrill-clock/preferences",json={"auto_analysis":True}).status_code == 200
    assert client.get("/api/merrill-clock").json()["automation"]["auto_analysis"]
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2,role="admin")
    result = client.get("/api/merrill-clock?region=CN").json()
    assert not result["automation"]["auto_analysis"] and result["analysis"]["text"] is None
    assert client.post("/api/merrill-clock/refresh").status_code == 202
    assert client.get("/api/merrill-clock/series?ids=not-valid&years=50").status_code == 400
    assert client.put("/api/merrill-clock/preferences",json={"auto_analysis":"true"}).status_code == 422
    monkeypatch.setattr(settings, "enable_merrill_clock", False)
    assert client.post("/api/merrill-clock/refresh").status_code == 409


def test_user_and_region_interpretations_are_separate(client):
    store.save_analysis(1,{"text":"private US","status":"ready"},region="US")
    store.save_analysis(1,{"text":"private CN","status":"ready"},region="CN")
    assert client.get("/api/merrill-clock").json()["analysis"]["text"] == "private US"
    assert client.get("/api/merrill-clock?region=CN").json()["analysis"]["text"] == "private CN"
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2,role="user")
    assert client.get("/api/merrill-clock?region=CN").json()["analysis"]["text"] is None


def test_manual_budget_dedupe_and_bounded_delayed_retries():
    one = service._enqueue("analysis",1,"US","manual:one",manual=True)
    assert service._enqueue("analysis",1,"US","manual:repeat",manual=True)["id"] == one["id"]
    with service._connection() as conn:
        conn.execute("UPDATE merrill_jobs SET status='success'")
    service._enqueue("analysis",1,"CN","manual:two",manual=True)
    with service._connection() as conn:
        conn.execute("UPDATE merrill_jobs SET status='success'")
    with pytest.raises(service.RateLimit):
        service._enqueue("analysis",1,"US","manual:three",manual=True)
    service._finish({"id":one["id"],"attempts":1},"timeout")
    assert service.latest_job("analysis",1,"US")["status"] == "queued"
    with service._connection() as conn:
        row = conn.execute("SELECT * FROM merrill_jobs WHERE id=?", (one["id"],)).fetchone()
        assert row["available_at"] - row["updated_at"] == 300
    service._finish({"id":one["id"],"attempts":3},"timeout")
    assert service.latest_job("analysis",1,"US")["status"] == "failed"


def test_llm_cannot_override_stage_and_receives_only_current_account(monkeypatch):
    monkeypatch.setattr(service, "compute_cycle", lambda *a,**k: {"status":"ready","phase":"recovery","as_of":"2026-09-01","region_code":"US"})
    monkeypatch.setattr(service, "asset_evidence", lambda: [])
    monkeypatch.setattr(service, "model_status", lambda uid: {"configured":True,"model":"test"})
    monkeypatch.setattr(service.users_store,"get_by_id",lambda uid:SimpleNamespace(id=uid))
    def wrong(*a,**kw):
        assert current_user_id.get() == 7
        return json.dumps({"phase":"overheat","text":"错"*100}),None
    monkeypatch.setattr(service.llm_client,"chat_completion_sync",wrong)
    with pytest.raises(RuntimeError,match="响应格式"):
        asyncio.run(service._analyze({"user_id":7,"region":"US","dedupe":"manual:x"}))
    assert store.get_analysis(7) is None
    def good(*a,**kw):
        assert current_user_id.get() == 7
        return json.dumps({"phase":"recovery","text":"数据解释"*60}),None
    monkeypatch.setattr(service.llm_client,"chat_completion_sync",good)
    asyncio.run(service._analyze({"user_id":7,"region":"US","dedupe":"manual:x"}))
    assert store.get_analysis(7)["phase"] == "recovery"
    assert store.get_analysis(8) is None


def test_external_thread_is_drained_before_cancel_returns():
    entered, release = threading.Event(), threading.Event()
    finished = []
    def blocking():
        entered.set()
        release.wait(timeout=5)
        finished.append(True)
    async def exercise():
        task = asyncio.create_task(service._drain_call(blocking))
        while not entered.is_set():
            await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.03)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == [True]
    asyncio.run(exercise())


def test_cross_process_lock_prevents_another_worker(monkeypatch):
    monkeypatch.setattr(settings,"enable_merrill_clock",True)
    with (settings.data_dir / "merrill_worker.lock").open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert asyncio.run(service.run_once()) is False
        fcntl.flock(lock,fcntl.LOCK_UN)


def test_old_fetch_is_not_current_even_when_statistical_period_is_recent():
    cycle = service.compute_cycle(macro(), now=NOW, metadata={"INDPRO":{"last_synced_at":NOW.timestamp() - 15*86400}})
    assert cycle["status"] == "stale" and cycle["phase"] is None


def test_enabled_accounts_schedule_once_per_region_after_backfill(monkeypatch):
    monkeypatch.setattr(service.users_store,"get_by_id",lambda uid:SimpleNamespace(id=uid))
    monkeypatch.setattr(service,"model_status",lambda uid:{"configured":True,"model":"test"})
    monkeypatch.setattr(store,"get_macro",lambda:{**macro(),**macro(region="CN")})
    _source_freshness(monkeypatch, NOW.timestamp())
    service.set_preferences(1,True)
    store.set_state("sync_status",{"status":"succeeded","finished_at":NOW.timestamp()})
    service._schedule_jobs(NOW.timestamp())
    service._schedule_jobs(NOW.timestamp() + 1)
    with service._connection() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM merrill_jobs")]
    assert len(rows) == 3
    assert [r["status"] for r in rows if r["kind"] == "refresh"] == ["success"]
    assert {r["region"] for r in rows if r["kind"] == "analysis"} == {"US","CN"}
    assert {r["user_id"] for r in rows if r["kind"] == "analysis"} == {1}
    service.set_preferences(1,False)
    assert service.latest_job("analysis",1,"US")["status"] == "cancelled"


def _source_freshness(monkeypatch, stamp):
    from app.services.merrill_sources import MACRO_CATALOG
    assets = [{**asset, "last_synced_at": stamp, "last_error": None, "row_count": 5}
              for asset in store.ASSET_CATALOG]
    metadata = {sid: {"last_synced_at": stamp, "last_error": None, "row_count": 18}
                for sid in MACRO_CATALOG}
    monkeypatch.setattr(store, "catalog", lambda: assets)
    store.set_state("macro_metadata", metadata)
    return assets, metadata


@pytest.mark.parametrize("missing", ["old_asset", "failed_asset", "empty_asset", "missing_macro", "failed_macro"])
def test_selected_success_does_not_hide_unfinished_weekly_sources(monkeypatch, missing):
    due, _ = service.schedule_window(NOW)
    assets, metadata = _source_freshness(monkeypatch, NOW.timestamp())
    if missing == "old_asset":
        assets[-1]["last_synced_at"] = due.timestamp() - 1
    elif missing == "failed_asset":
        assets[-1]["last_error"] = "source unavailable"
    elif missing == "empty_asset":
        assets[-1]["row_count"] = 0
    elif missing == "missing_macro":
        metadata.pop("CN_PMI")
    else:
        metadata["INDPRO"]["last_error"] = "timeout"
    store.set_state("macro_metadata", metadata)
    store.set_state("sync_status", {"scope": "selected", "status": "succeeded", "finished_at": NOW.timestamp(), "series_count": 1, "macro_count": 0})
    service._schedule_jobs(NOW.timestamp())
    assert service.latest_job("refresh")["status"] == "queued"


@pytest.mark.parametrize("prior_status,attempts", [("queued", 2), ("failed", 3)])
def test_targeted_runs_that_complete_all_sources_retire_previous_retry(monkeypatch, prior_status, attempts):
    _source_freshness(monkeypatch, NOW.timestamp())
    due, _ = service.schedule_window(NOW)
    job = service._enqueue("refresh", 0, "", "weekly:" + due.date().isoformat(), now=NOW.timestamp() - 10)
    with service._connection() as conn:
        conn.execute("UPDATE merrill_jobs SET status=?,attempts=?,error='old failure' WHERE id=?", (prior_status, attempts, job["id"]))
    # This last invocation only downloaded one missing source. The per-source
    # success timestamps prove the preceding runs already completed the rest.
    store.set_state("sync_status", {"scope": "selected", "status": "succeeded", "finished_at": NOW.timestamp(), "series_count": 1, "macro_count": 0})
    service._schedule_jobs(NOW.timestamp())
    result = service.latest_job("refresh")
    assert result["status"] == "success" and result["attempts"] == attempts and result["error"] is None
    assert service._recover_and_claim() is None


def test_rate_evidence_reports_basis_points_not_price_returns(monkeypatch):
    monkeypatch.setattr(store,"catalog",lambda:[{"id":"rate","name":"rate","kind":"yield","unit":"%"}])
    monkeypatch.setattr(store,"get_series",lambda *a,**k:[{"id":"rate","points":[{"date":"2026-10-23","observation_date":"2026-10-23","close":4.0},{"date":"2026-10-30","observation_date":"2026-10-30","close":4.1}]}])
    result = service.asset_evidence()[0]
    assert result["change_unit"] == "bp" and result["change_1w"] == 10


def test_restart_after_report_save_does_not_repeat_a_paid_call(monkeypatch):
    monkeypatch.setattr(service.users_store,"get_by_id",lambda uid:SimpleNamespace(id=uid))
    monkeypatch.setattr(service.llm_client,"chat_completion_sync",lambda *a,**k:pytest.fail("already-published job must not call model again"))
    store.save_analysis(1,{"job_id":"finished-before-stop","text":"saved"},region="CN")
    asyncio.run(service._analyze({"id":"finished-before-stop","user_id":1,"region":"CN","dedupe":"weekly:2026-10-31"}))
