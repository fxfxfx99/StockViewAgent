from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from app.config import settings
from app.schemas.investment_research import DossierCreate, MaterialCreate
from app.storage import investment_research_store as store


def create(uid=1, **changes):
    return store.create_dossier(uid, {"title": "公司研究", "target_type": "stock", "target_code": "600519.sh",
                                      "objective": "收入与现金流能否持续增长", "thesis": "待验证的个人判断", **changes})


def run(uid=1, dossier=None, owner="worker"):
    dossier = dossier or create(uid)
    job = store.enqueue(uid, dossier["id"])
    claimed = store.claim(owner)
    assert claimed["id"] == job["id"]
    return claimed


def result(job, **extra):
    return {"dossier_snapshot": job["snapshot"]["dossier"], "input_hash": "stable-input", "model": "test-model",
            "model_called": False, "report": {"summary": "仍须核对收入来源", "assessment": "insufficient_data",
            "findings": [], "judgment_changes": [], "open_questions": [], "next_observations": [],
            "risks": [], "limitations": []}, "evidence": [], "coverage": [], **extra}


def test_schema_targets_normalize_and_reject_cross_type_fields():
    assert create()["target_code"] == "600519.SS"
    values = DossierCreate(title="组合研究", objective="观察成员", target_type="stock_list",
                            target_symbols=["600519.sh", "600519.SS", "000001.SZ"])
    assert values.target_symbols == ["600519.SS", "000001.SZ"]
    for changes in ({"target_type": "stock_list", "target_symbols": []}, {"target_type": "topic", "keywords": []},
                    {"target_type": "stock", "target_code": "600519.SS", "target_symbols": ["000001.SZ"]}):
        with pytest.raises(ValueError):
            DossierCreate(title="对象", objective="目标", **changes)
    with pytest.raises(ValueError):
        MaterialCreate(kind="monitoring", title="伪造来源", monitor_run_id="id", text="伪造事实")
    with pytest.raises(ValueError):
        MaterialCreate(title="链接", text="正文", source_url="https://user:secret@example.com")


def test_accounts_revisions_and_queued_jobs_are_isolated():
    dossier = create()
    job = store.enqueue(1, dossier["id"])
    assert store.get_dossier(2, dossier["id"]) is None
    assert store.get_job(2, job["id"]) is None
    with pytest.raises(store.NotFound):
        store.update_dossier(2, dossier["id"], {"revision": 1, "title": "他人"})
    updated = store.update_dossier(1, dossier["id"], {"revision": 1, "thesis": "新的人工判断"})
    assert updated["revision"] == 2
    assert store.get_job(1, job["id"])["status"] == "cancelled"
    with pytest.raises(store.ResearchConflict):
        store.update_dossier(1, dossier["id"], {"revision": 1, "title": "过期修改"})
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM dossier_revisions").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE dossier_revisions SET payload='{}' WHERE dossier_id=?", (dossier["id"],))


def test_question_and_material_changes_freeze_job_inputs():
    dossier = create()
    question = store.create_question(1, dossier["id"], {"text": "收入来源？"})
    material = store.add_material(1, dossier["id"], {"kind": "note", "title": "假说", "text": "暂未验证的观点"})
    job = run(dossier=store.get_dossier(1, dossier["id"]))
    assert job["snapshot"]["questions"][0]["text"] == "收入来源？"
    store.update_question(1, question["id"], {"revision": 1, "answer": "还需财报", "status": "open"})
    store.delete_material(1, material["id"])
    assert store.get_material(1, material["id"]) is None
    assert store.get_material(1, material["id"], include_deleted=True)["text"] == "暂未验证的观点"
    assert store.get_job(1, job["id"])["snapshot"]["questions"][0]["answer"] == ""
    with pytest.raises(store.ResearchConflict):
        store.reserve_call(1, job["id"], "worker")
    assert store.usage(1)["used"] == 0


def test_pending_pdf_blocks_generation_and_parse_preserves_physical_pages():
    dossier = create()
    material = store.add_material(1, dossier["id"], {"kind": "pdf", "title": "财报", "status": "pending", "sha256": "a"*64})
    with pytest.raises(store.ResearchConflict):
        store.enqueue(1, dossier["id"])
    store.enqueue(1, dossier["id"], kind="prepare", material_id=material["id"])
    job = store.claim("owner")
    store.finish(1, job["id"], "owner", {"extraction": {"sha256": "a"*64, "status": "partial",
                                                          "pages": [{"page": 1, "text": "文字"}, {"page": 3, "text": "报表"}]}})
    assert [row["page"] for row in store.get_material(1, material["id"])["pages"]] == [1, 3]
    assert store.enqueue(1, dossier["id"])["status"] == "queued"
    assert store.usage(1)["used"] == 0


def test_material_capacity_and_monitor_import_idempotence():
    dossier = create()
    payload = {"kind": "monitoring", "title": "监控", "monitor_run_id": "same-run", "text": "", "monitoring_snapshot": {"report": {}}}
    first = store.add_material(1, dossier["id"], payload)
    revision = store.get_dossier(1, dossier["id"])["revision"]
    again = store.add_material(1, dossier["id"], payload)
    assert first["id"] == again["id"] and again["deduplicated"]
    assert store.get_dossier(1, dossier["id"])["revision"] == revision
    for index in range(29):
        store.add_material(1, dossier["id"], {"kind": "note", "title": str(index), "text": "x"})
    with pytest.raises(store.ResearchConflict):
        store.add_material(1, dossier["id"], {"kind": "note", "title": "31", "text": "x"})


def test_queue_claim_is_atomic_and_request_focus_is_not_swallowed():
    dossier = create()
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(lambda _: store.enqueue(1, dossier["id"]), range(8)))
    assert len({job["id"] for job in jobs}) == 1
    with pytest.raises(store.ResearchConflict):
        store.enqueue(1, dossier["id"], focus="不同问题")
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda owner: store.claim(str(owner)), range(4)))
    assert len([row for row in claims if row]) == 1


def test_daily_budget_is_atomic_and_separate_per_account(monkeypatch):
    monkeypatch.setattr(settings, "enable_investment_research", True)
    monkeypatch.setattr(settings, "investment_research_daily_call_limit", 1)
    jobs = [run(dossier=create(title=f"公司{i}"), owner=f"owner{i}") for i in range(2)]
    def reserve(index):
        try:
            store.reserve_call(1, jobs[index]["id"], f"owner{index}")
            return "reserved"
        except store.DailyLimit:
            return "limited"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, [0, 1])) == ["limited", "reserved"]
    assert store.usage(1)["used"] == 1
    assert store.usage(2)["used"] == 0


def test_dispatch_cannot_repeat_and_interruption_never_requeues_paid(monkeypatch):
    monkeypatch.setattr(settings, "enable_investment_research", True)
    job = run()
    store.reserve_call(1, job["id"], "worker")
    with pytest.raises(store.ResearchConflict):
        store.reserve_call(1, job["id"], "worker")
    store.recover_interrupted()
    saved = store.get_job(1, job["id"])
    assert saved["status"] == "failed" and saved["error_code"] == "interrupted_after_dispatch"
    assert store.claim("new") is None
    assert store.usage(1)["used"] == 1


def test_undispatched_recovery_and_cancellation_are_safe():
    job = run()
    store.recover_interrupted()
    reclaimed = store.claim("new")
    assert reclaimed["id"] == job["id"] and reclaimed["attempts"] == 2
    with pytest.raises(store.LeaseLost):
        store.heartbeat(1, job["id"], "worker")
    store.cancel_job(1, job["id"])
    with pytest.raises(store.LeaseLost):
        store.reserve_call(1, job["id"], "new")


def test_report_is_immutable_and_requires_explicit_adoption(monkeypatch):
    monkeypatch.setattr(settings, "enable_investment_research", True)
    job = run()
    store.reserve_call(1, job["id"], "worker")
    final = store.finish(1, job["id"], "worker", result(job, model_called=True))
    report_id, dossier_id = final["report_id"], job["dossier_id"]
    dossier = store.get_dossier(1, dossier_id)
    assert dossier["adopted_report_id"] is None and dossier["thesis"] == "待验证的个人判断"
    assert store.list_dossiers(1)[0]["unread_count"] == 1
    assert store.get_report(2, report_id) is None
    with store.connection() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE research_reports SET result='{}' WHERE id=?", (report_id,))
    adopted = store.adopt_report(1, report_id, dossier["revision"])
    assert adopted["adopted_report_id"] == report_id and adopted["thesis"] == "待验证的个人判断"
    assert store.get_report(1, report_id)["is_read"]
    assert len(store.list_adoptions(1, dossier_id)) == 1
    assert store.list_dossiers(1)[0]["adopted_report_summary"] == "仍须核对收入来源"
    store.mark_report(1, report_id, False)
    assert not store.get_report(1, report_id)["is_read"]


def test_inflight_old_result_keeps_snapshot_and_cannot_adopt_other_target(monkeypatch):
    monkeypatch.setattr(settings, "enable_investment_research", True)
    job = run()
    store.reserve_call(1, job["id"], "worker")
    updated = store.update_dossier(1, job["dossier_id"], {"revision": 1, "target_code": "000001.SZ"})
    report_id = store.finish(1, job["id"], "worker", result(job, model_called=True))["report_id"]
    assert store.get_report(1, report_id)["result"]["dossier_snapshot"]["target_code"] == "600519.SS"
    assert store.get_dossier(1, job["dossier_id"])["target_code"] == "000001.SZ"
    with pytest.raises(store.ResearchConflict):
        store.adopt_report(1, report_id, updated["revision"])


def test_cached_report_cannot_cross_account_or_dossier():
    job = run()
    report_id = store.finish(1, job["id"], "worker", result(job))["report_id"]
    assert store.cached_report(1, job["dossier_id"], "stable-input")["id"] == report_id
    assert store.cached_report(2, job["dossier_id"], "stable-input") is None
    another = run(dossier=create(title="其它档案"))
    with pytest.raises(store.ResearchConflict):
        store.finish(1, another["id"], "worker", result(another, reused=True, cached_report_id=report_id))


def test_archive_blocks_reserve_and_retains_history(monkeypatch):
    monkeypatch.setattr(settings, "enable_investment_research", True)
    job = run()
    store.update_dossier(1, job["dossier_id"], {"revision": 1, "status": "archived"})
    with pytest.raises(store.ResearchConflict):
        store.reserve_call(1, job["id"], "worker")
    with pytest.raises(store.ResearchConflict):
        store.add_material(1, job["dossier_id"], {"kind": "note", "title": "new", "text": "x"})
    assert store.get_dossier(1, job["dossier_id"])["status"] == "archived"
    assert store.usage(1)["used"] == 0


@pytest.mark.parametrize("action", ["failure", "cancel", "archive"])
def test_failed_pdf_becomes_unavailable_and_can_be_explicitly_reprepared(action):
    dossier = create()
    material = store.add_material(1, dossier["id"], {"kind": "pdf", "title": "PDF", "status": "pending", "sha256": "a"*64})
    job = store.enqueue(1, dossier["id"], kind="prepare", material_id=material["id"])
    if action == "failure":
        store.claim("worker")
        store.fail(1, job["id"], "worker", "Bearer should-never-be-saved")
        assert store.get_job(1, job["id"])["error_code"] == "research_failed"
    elif action == "cancel":
        store.cancel_job(1, job["id"])
    else:
        value = store.get_dossier(1, dossier["id"])
        archived = store.update_dossier(1, dossier["id"], {"revision": value["revision"], "status": "archived"})
        store.update_dossier(1, dossier["id"], {"revision": archived["revision"], "status": "active"})
    assert store.get_material(1, material["id"])["status"] == "error"
    retry = store.enqueue(1, dossier["id"], kind="prepare", material_id=material["id"])
    assert retry["id"] != job["id"]
    assert store.get_material(1, material["id"])["status"] == "pending"
    with pytest.raises(store.ResearchConflict):
        store.enqueue(1, dossier["id"])
