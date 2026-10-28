"""Real SQLite/FTS and filesystem regression tests for the shared data centre."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from app.storage import data_collection_store as store


T0 = 1700000000
T1, T2, T3 = T0 + 100, T0 + 200, T0 + 300


def observation(**changes):
    return {"source_id": "official_test", "upstream_id": "gdp:CN:2022", "category": "macro",
            "source": "Official public data", "source_url": "https://example.org/data?format=json",
            "title": "年度 GDP growth 数据", "text": "中国经济增长年度指标", "period": "2022",
            "published_at": "2023-01-20T01:00:00Z", "observed_at": store.iso(T0),
            "region": "CN", "symbols": [], "value": 3.2, "unit": "%",
            "metadata": {"frequency": "annual", "value_status": "observed"}, **changes}


def ingest(rows=None, stamp=T1):
    return store.ingest("official_test", "macro", [observation()] if rows is None else rows, observed_at=stamp)


def current(stamp=T3, **kwargs):
    return store.records(as_of=stamp, **kwargs)["items"]


def counts():
    with store.connection() as conn:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("dc_revisions", "dc_heads", "dc_batches", "dc_chunks")}


def test_real_ingest_has_immutable_classified_file_and_fts_provenance(tmp_path):
    result = ingest()
    assert result["records_received"] == result["records_added"] == 1
    path = tmp_path / "datasets" / "macro" / "official_test" / (result["batch_id"] + ".json")
    payload = json.loads(path.read_text())
    assert payload["records"][0]["value"] == 3.2
    assert "observed_at" not in payload["records"][0]
    assert payload["observed_at"] == store.iso(T1)
    assert path.stat().st_mode & 0o777 == 0o600
    row = current(q="经济增长")[0]
    assert row["citation"] == f"dataset:{row['id']}:{row['revision_id']}"
    assert row["batch_id"] == result["batch_id"]
    assert "统计期 2022" in row["snippet"]
    assert isinstance(row["rank"], float)
    assert counts() == {"dc_revisions": 1, "dc_heads": 1, "dc_batches": 1, "dc_chunks": 1}


def test_refresh_deduplicates_despite_observation_timestamp_and_order_changes(tmp_path):
    rows = [observation(), observation(upstream_id="gdp:US:2022", region="US")]
    first = ingest(rows)
    snapshots = current()
    paths = list(tmp_path.glob("datasets/**/*.json"))
    stat = paths[0].stat().st_mtime_ns
    refreshed = deepcopy(rows[::-1])
    for row in refreshed:
        row["observed_at"] = store.iso(T2)
    second = ingest(refreshed, T2)
    assert second["batch_id"] == first["batch_id"]
    assert second["records_added"] == 0
    assert current() == snapshots
    assert paths[0].stat().st_mtime_ns == stat
    assert counts()["dc_revisions"] == 2
    with store.connection() as conn:
        assert {row[0] for row in conn.execute("SELECT last_seen_at FROM dc_heads")} == {T2}


def test_a_b_a_creates_three_revisions_reuses_batch_and_preserves_history():
    outcomes = [ingest([observation(value=value)], stamp) for value, stamp in [(3.2, T1), (4.1, T2), (3.2, T3)]]
    assert all(item["records_added"] == 1 for item in outcomes)
    assert outcomes[0]["batch_id"] == outcomes[2]["batch_id"]
    snapshots = [current(stamp)[0] for stamp in (T1, T2, T3)]
    assert [row["value"] for row in snapshots] == [3.2, 4.1, 3.2]
    assert len({row["id"] for row in snapshots}) == 1
    assert len({row["revision_id"] for row in snapshots}) == 3
    assert counts()["dc_batches"] == 2


def test_pit_selects_latest_visible_revision_before_matching_and_period_is_not_availability():
    ingest([observation(title="amberrelease", period="2000")], T1)
    ingest([observation(title="cobaltrevision", period="2000")], T2)
    assert current(T0) == []
    assert len(current(T1, q="amberrelease")) == 1
    assert current(T1, q="cobaltrevision") == []
    assert current(T2, q="amberrelease") == []
    assert current(T2, q="cobaltrevision")[0]["period"] == "2000"


def test_future_documents_cannot_change_historical_bm25_scores_or_rank():
    ingest([observation(upstream_id="a", title="GDP growth", text="economy growth"),
            observation(upstream_id="b", title="GDP development", text="growth growth fiscal monetary")], T1)
    before = current(T1, q="GDP growth")
    ingest([observation(upstream_id=f"future-{n}", title="GDP growth " * (n + 1)) for n in range(15)], T2)
    after = current(T1, q="GDP growth")
    assert after == before


def test_same_observation_time_uses_append_order_and_latest_period_breaks_ties():
    ingest([observation(value=1)], T1)
    ingest([observation(value=2)], T1)
    assert current(T1)[0]["value"] == 2
    ingest([observation(upstream_id=f"year-{year}", period=str(year), published_at=None) for year in (2017, 2020, 2023)], T2)
    assert [row["period"] for row in current(T2)[:3]] == ["2023", "2020", "2017"]


def test_backfill_is_atomic_and_does_not_leave_orphan_json(tmp_path):
    ingest(stamp=T2)
    before = counts()
    files = set(tmp_path.glob("datasets/**/*.json"))
    with pytest.raises(ValueError, match="倒填"):
        ingest([observation(upstream_id="would-be-new"), observation(value=99)], T1)
    assert counts() == before
    assert set(tmp_path.glob("datasets/**/*.json")) == files


def test_backfill_rejected_after_an_unchanged_refresh():
    ingest(stamp=T1)
    ingest(stamp=T3)
    with pytest.raises(ValueError, match="倒填"):
        ingest([observation(value=99)], T2)
    assert current()[0]["value"] == 3.2


def test_file_written_before_interruption_recovers_without_backdating_retrieval(tmp_path, monkeypatch):
    original = store._write_batch
    def interrupted(relative, payload):
        original(relative, payload)
        raise RuntimeError("process interruption after atomic link")
    monkeypatch.setattr(store, "_write_batch", interrupted)
    with pytest.raises(RuntimeError):
        ingest(stamp=T1)
    assert counts()["dc_revisions"] == counts()["dc_batches"] == 0
    path = next(tmp_path.glob("datasets/**/*.json"))
    original_bytes = path.read_bytes()
    monkeypatch.setattr(store, "_write_batch", original)
    assert ingest(stamp=T2)["records_added"] == 1
    assert path.read_bytes() == original_bytes
    assert current(T1) == []
    assert current(T2)[0]["observed_at"] == store.iso(T2)


@pytest.mark.parametrize("sql", ["UPDATE dc_revisions SET payload='{}'", "UPDATE dc_revisions SET observed_at=1", "DELETE FROM dc_revisions"])
def test_database_triggers_reject_revision_mutation(sql):
    ingest()
    before = current()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.connection() as conn:
            conn.execute(sql)
    assert current() == before


def test_repeated_batch_file_tampering_is_detected(tmp_path):
    ingest()
    path = next(tmp_path.glob("datasets/**/*.json"))
    path.write_text('{}')
    with pytest.raises(ValueError, match="冲突"):
        ingest(stamp=T2)
    assert counts()["dc_revisions"] == 1


@pytest.mark.parametrize("query", ['GDP" OR * NEAR(', "GDP'); DROP TABLE dc_revisions; --", 'GDP AND NOT (title:"'])
def test_search_escapes_sql_and_fts_syntax(query):
    ingest()
    assert len(current(q=query)) == 1
    assert counts()["dc_revisions"] == 1


def test_chunks_reduce_to_one_record_and_support_text_tail():
    ingest([observation(text=("economic_detail " * 300) + " uniquetailkeyword")])
    assert counts()["dc_chunks"] > 1
    assert len(current(q="uniquetailkeyword")) == 1
    assert len(current(q="economic_detail")) == 1
    assert current(q="*\"()") == []


def test_category_source_filters_and_pagination_return_total():
    ingest([observation(upstream_id=f"obs-{n}") for n in range(4)])
    result = store.records(source_id="official_test", category="macro", offset=1, limit=2, as_of=T3)
    assert len(result["items"]) == 2 and result["total"] == 4
    assert current(category="fx") == current(source_id="unknown") == []


def test_secrets_are_excluded_from_metadata_queue_state_and_files(tmp_path):
    private = "private-material-never-write"
    ingest([observation(api_key=private, metadata={"api_key": private, "headers": {"Authorization": private},
                        "safe": [{"value": 1, "openai_api_key": private}], "url": f"https://example.org/?token={private}"})])
    store.set_state("config", {"safe": 3, "client_secret": private})
    store.enqueue("official_test", {"scheduled": False, "password": private}, now=T1)
    row = current()[0]
    assert row["metadata"] == {"headers": {}, "safe": [{"value": 1}], "url": "[redacted_url]"}
    assert store.get_state("config") == {"safe": 3}
    assert store.list_jobs()[0]["payload"] == {"scheduled": False}
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert private.encode() not in path.read_bytes(), str(path)


def test_labeled_credential_text_is_redacted_including_bearer_and_quoted_password():
    ingest([observation(text='Authorization: Bearer hidden-bearer-token; password="hidden quoted password"; text remains')])
    text = current()[0]["text"]
    assert "hidden" not in text
    assert "text remains" in text


@pytest.mark.parametrize("url", ["http://example.org/", "https://user:password@example.org/", "https://example.org/?api_key=private", "https://example.org/#access_token=private", "https://example.org:bad/", "https://[bad/"])
def test_invalid_or_credential_source_urls_rejected_before_writes(url, tmp_path):
    with pytest.raises(ValueError):
        ingest([observation(source_url=url)])
    assert not list(tmp_path.glob("datasets/**/*.json"))


@pytest.mark.parametrize("changes", [{"value": True}, {"value": float("nan")}, {"value": float("inf")}, {"value": 10 ** 1000},
    {"symbols": "600519.SS"}, {"symbols": [1]}, {"metadata": ["wrong"]}, {"source_id": "other"},
    {"upstream_id": "x" * 501}, {"published_at": "2023-01-01"}, {"published_at": "2030-01-01T00:00:00Z"},
    {"metadata": {"nested": float("nan")}}])
def test_invalid_records_reject_entire_batch_before_writes(changes, tmp_path):
    with pytest.raises(ValueError):
        ingest([observation(upstream_id="valid"), observation(**changes)])
    assert not list(tmp_path.glob("datasets/**/*.json"))
    assert current() == []


def test_zero_and_missing_numeric_values_are_preserved():
    ingest([observation(upstream_id="zero", value=0), observation(upstream_id="missing", value=None)])
    assert {row["upstream_id"]: row["value"] for row in current()} == {"zero": 0, "missing": None}


@pytest.mark.parametrize("source,category,rows", [("../escape", "macro", []), ("official_test", "../escape", []),
                                                   ("official_test", "macro", []), ("official_test", "macro", {})])
def test_empty_or_invalid_batches_cannot_bypass_path_validation(source, category, rows, tmp_path):
    with pytest.raises(ValueError):
        store.ingest(source, category, rows, observed_at=T1)
    assert not (tmp_path / "datasets").exists()


def test_symlinked_dataset_tree_and_database_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "datasets").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        ingest()
    assert list(outside.iterdir()) == []
    (tmp_path / "datasets").unlink()
    (tmp_path / "data_center.sqlite3").unlink()
    target = outside / "db"
    target.write_text("untouched")
    (tmp_path / "data_center.sqlite3").symlink_to(target)
    with pytest.raises(ValueError, match="符号链接"):
        store.dataset_stats()
    assert target.read_text() == "untouched"


def test_enqueue_deduplicates_across_threads_and_manual_request_preserves_intent():
    def add(_):
        return store.enqueue("official_test", {"scheduled": True}, now=T0)
    with ThreadPoolExecutor(max_workers=6) as executor:
        outcomes = list(executor.map(add, range(12)))
    assert len({row["id"] for row in outcomes}) == 1
    assert sum(row.get("deduplicated", False) for row in outcomes) == 11
    manual = store.enqueue("official_test", {"scheduled": False}, now=T1)
    assert manual["deduplicated"] and manual["payload"]["scheduled"] is False
    assert store.cancel_disallowed_scheduled([], now=T1) == 0


def test_concurrent_claims_consume_daily_budget_once_per_actual_attempt():
    for n in range(6):
        store.enqueue(f"source_{n}", now=T0 + n)
    def claim(_):
        return store.claim_job(3, now=T1)
    with ThreadPoolExecutor(max_workers=6) as executor:
        outcomes = list(executor.map(claim, range(6)))
    claimed = [row for row in outcomes if row]
    assert len(claimed) == len({row["id"] for row in claimed}) == 3
    assert store.usage(now=T1) == {"day": "2023-11-15", "jobs_started": 3, "queued": 3, "running": 3}


def test_daily_allowance_resets_at_shanghai_midnight():
    before = datetime(2026, 1, 1, 15, 59, tzinfo=timezone.utc).timestamp()
    after = before + 120
    store.enqueue("source_one", now=before)
    store.enqueue("source_two", now=before)
    assert store.claim_job(1, now=before)
    assert store.claim_job(1, now=before) is None
    assert store.claim_job(1, now=after)
    assert store.usage(now=before)["day"] == "2026-01-01"
    assert store.usage(now=after)["day"] == "2026-01-02"
    assert store.usage(now=after)["jobs_started"] == 1


def test_recover_interrupted_requeues_same_job_without_losing_attempt_accounting():
    queued = store.enqueue("official_test", now=T0)
    first = store.claim_job(2, now=T1)
    assert first["id"] == queued["id"]
    store.recover_interrupted()
    assert store.list_jobs()[0]["error_code"] == "interrupted"
    second = store.claim_job(2, now=T2)
    assert second["id"] == first["id"]
    assert second["started_at"] == store.iso(T2)
    assert second["error_code"] is None
    assert store.usage(now=T2)["jobs_started"] == 2
    store.finish_job(second["id"], {"records_received": 4, "records_added": 2}, now=T3)
    assert store.list_jobs()[0]["status"] == "success"
    assert store.source_states()["official_test"]["last_success_at"] == store.iso(T3)


def test_failure_keeps_last_success_and_cooldown_is_measured_from_attempt_start():
    store.enqueue("official_test", now=T0)
    job = store.claim_job(10, now=T1)
    store.finish_job(job["id"], {"records_received": 1, "records_added": 1}, now=T1 + 1)
    blocked = store.enqueue("official_test", now=T1 + 30)
    assert blocked["status"] == "cooldown" and blocked["retry_after"] == 30
    store.enqueue("official_test", now=T2)
    job = store.claim_job(10, now=T2)
    store.finish_job(job["id"], {"status": "failed", "error_code": "http_503"}, now=T3)
    state = store.source_states()["official_test"]
    assert state["status"] == "failed" and state["failures"] == 1
    assert state["last_success_at"] == store.iso(T1 + 1)
    assert state["last_attempt_at"] == store.iso(T2)


def test_disabled_automatic_tasks_cancel_without_quota_or_source_health_changes():
    for source, payload in [("disabled", {"scheduled": True}), ("allowed", {"scheduled": True}),
                            ("manual", {"scheduled": False}), ("unspecified", {}), ("not_boolean", {"scheduled": 1})]:
        store.enqueue(source, payload, now=T0)
    assert store.cancel_disallowed_scheduled({"allowed"}, now=T1) == 1
    jobs = {row["source_id"]: row for row in store.list_jobs()}
    assert jobs["disabled"]["status"] == "skipped"
    assert jobs["disabled"]["error_code"] == "automatic_collection_disabled"
    assert jobs["disabled"]["finished_at"] == store.iso(T1)
    assert all(jobs[source]["status"] == "queued" for source in {"allowed", "manual", "unspecified", "not_boolean"})
    assert store.usage(now=T1)["jobs_started"] == 0
    assert store.source_states() == {}
    assert store.cancel_disallowed_scheduled({"allowed"}, now=T2) == 0


def test_cancellation_does_not_touch_running_jobs():
    store.enqueue("running", {"scheduled": True}, now=T0)
    store.claim_job(1, now=T1)
    assert store.cancel_disallowed_scheduled([], now=T2) == 0
    assert store.list_jobs()[0]["status"] == "running"


def test_atomic_state_updates_do_not_lose_concurrent_changes():
    store.set_state("counter", 0)
    def increment(_):
        return store.mutate_state("counter", lambda value: value + 1, 0)
    with ThreadPoolExecutor(max_workers=6) as executor:
        assert sorted(executor.map(increment, range(24))) == list(range(1, 25))
    assert store.get_state("counter") == 24


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), 0, -1, 1e100])
def test_invalid_timestamps_rejected_consistently(bad):
    for operation in (lambda: ingest(stamp=bad), lambda: store.enqueue("official_test", now=bad),
                      lambda: store.claim_job(1, now=bad), lambda: store.records(as_of=bad)):
        with pytest.raises(ValueError):
            operation()


@pytest.mark.parametrize("bad", [True, -1, float("nan"), 1.5, "10"])
def test_invalid_budget_cannot_bypass_limit(bad):
    store.enqueue("official_test", now=T0)
    with pytest.raises(ValueError):
        store.claim_job(bad, now=T1)
    assert store.usage(now=T1)["jobs_started"] == 0


@pytest.mark.parametrize("result", [{"status": "running"}, {"records_received": -1}, {"records_received": True},
                                    {"records_received": 0, "records_added": 1}, {"records_received": "5"}])
def test_invalid_results_cannot_mutate_running_task(result):
    store.enqueue("official_test", now=T0)
    job = store.claim_job(1, now=T1)
    with pytest.raises(ValueError):
        store.finish_job(job["id"], result, now=T2)
    assert store.list_jobs()[0]["status"] == "running"
    assert store.source_states() == {}


def test_finish_requires_running_job_and_sanitizes_error_message():
    queued = store.enqueue("official_test", now=T0)
    with pytest.raises(ValueError, match="运行状态"):
        store.finish_job(queued["id"], {}, now=T1)
    job = store.claim_job(1, now=T1)
    store.finish_job(job["id"], {"status": "failed", "error_code": "Authorization: private-key"}, now=T2)
    assert store.list_jobs()[0]["error_code"] == "source_unavailable"
    assert store.source_states()["official_test"]["error_code"] == "source_unavailable"
    with pytest.raises(ValueError, match="运行状态"):
        store.finish_job(job["id"], {}, now=T3)


@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 201}, {"limit": True}, {"offset": -1}, {"offset": 1.5}, {"q": "x" * 10001}])
def test_invalid_query_bounds_rejected(arguments):
    with pytest.raises(ValueError):
        store.records(**arguments)


def test_same_batch_concurrent_ingestion_is_deduplicated():
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: ingest(stamp=T1), range(8)))
    assert sum(row["records_added"] for row in results) == 1
    assert len({row["batch_id"] for row in results}) == 1
    assert counts()["dc_revisions"] == counts()["dc_batches"] == 1


def test_latest_period_precedes_revision_recency_fts_score_and_result_limit():
    base = 1770000000
    ingest([observation(upstream_id="growth-2017", period="2017", text="growth " * 30),
            observation(upstream_id="growth-2025", period="2025", text="growth current annual measure")], base)
    ingest([observation(upstream_id="growth-2017", period="2017", text="growth " * 40, value=7.7)], base + 100)
    default = store.records(category="macro", q="growth", as_of=base + 100)
    assert default["total"] == 2 and {row["period"] for row in default["items"]} == {"2017", "2025"}
    latest = store.records(category="macro", q="growth", as_of=base + 100, latest_period_only=True, limit=1)
    assert latest["total"] == 1
    assert latest["items"][0]["period"] == "2025"
    assert latest["items"][0]["observed_at"] == store.iso(base)


def test_latest_period_excludes_future_observations_before_series_choice_and_bm25():
    base = 1770000000
    ingest([observation(upstream_id="growth-2017", period="2017"),
            observation(upstream_id="growth-2025", period="2025")], base)
    before = store.records(q="growth", as_of=base + 50, latest_period_only=True)
    ingest([observation(upstream_id="growth-2026", period="2026", text="growth " * 100)], base + 100)
    assert store.records(q="growth", as_of=base + 50, latest_period_only=True) == before
    assert before["items"][0]["period"] == "2025"
    assert store.records(q="growth", as_of=base + 100, latest_period_only=True)["items"][0]["period"] == "2026"


def test_latest_period_is_selected_before_keyword_matching():
    ingest([observation(upstream_id="old", period="2017", text="obsoleteexclusivekeyword"),
            observation(upstream_id="new", period="2022", text="current dataset")])
    assert store.records(q="obsoleteexclusivekeyword", as_of=T3)["total"] == 1
    assert store.records(q="obsoleteexclusivekeyword", as_of=T3, latest_period_only=True)["total"] == 0


def test_latest_period_groups_before_pagination_even_with_hundreds_of_old_fx_dates():
    from datetime import timedelta
    beginning = datetime(2022, 1, 1)
    rows = [observation(upstream_id=f"eurusd-{n}", title="EUR/USD", period=(beginning + timedelta(days=n)).date().isoformat(),
                        text="dollar " * 25 if n < 250 else "current dollar reference") for n in range(251)]
    ingest(rows)
    result = store.records(q="dollar", as_of=T3, latest_period_only=True, limit=1)
    assert result["total"] == 1
    assert result["items"][0]["upstream_id"] == "eurusd-250"
    assert store.records(as_of=T3)["total"] == 251


def test_latest_period_keeps_distinct_series_and_publisher_filters():
    ingest([observation(upstream_id="cn-old", period="2017"), observation(upstream_id="cn-new", period="2022"),
            observation(upstream_id="us-new", period="2022", region="US"),
            observation(upstream_id="other-series", title="Inflation", period="2021")])
    store.ingest("second", "fx", [observation(source_id="second", upstream_id="second", period="2023")], observed_at=T1)
    assert store.records(as_of=T3, latest_period_only=True)["total"] == 4
    assert store.records(as_of=T3, source_id="official_test", latest_period_only=True)["total"] == 3
    assert store.records(as_of=T3, category="fx", latest_period_only=True)["items"][0]["source_id"] == "second"


def test_latest_period_policy_only_collapses_identical_titles_by_publication_time():
    rows = [observation(upstream_id="statement-old", title="FOMC statement", period=None, published_at=store.iso(T0)),
            observation(upstream_id="statement-new", title="FOMC statement", period=None, published_at=store.iso(T1)),
            observation(upstream_id="minutes", title="FOMC minutes", period=None, published_at=store.iso(T0))]
    store.ingest("official_test", "policy", rows, observed_at=T2)
    result = store.records(category="policy", as_of=T3, latest_period_only=True)
    assert {row["upstream_id"] for row in result["items"]} == {"statement-new", "minutes"}
    assert store.records(category="policy", as_of=T3)["total"] == 3


def test_record_payload_and_fts_selection_use_same_sqlite_read_snapshot(monkeypatch):
    ingest([observation(title="oldkeyword")])
    original = store.tokens
    injected = False
    def tokens_after_concurrent_commit(query):
        nonlocal injected
        if not injected:
            injected = True
            ingest([observation(title="newkeyword")], T1)
        return original(query)
    monkeypatch.setattr(store, "tokens", tokens_after_concurrent_commit)
    # Commit a newer revision between the visible-row SELECT and corpus SELECT.
    result = store.records(q="oldkeyword", as_of=T1)
    assert result["total"] == 1
    assert result["items"][0]["title"] == "oldkeyword"
    assert current(T1)[0]["title"] == "newkeyword"
