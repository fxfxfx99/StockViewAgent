"""Real SQLite/FTS5 coverage for historical event evidence and account settings."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from app.storage import event_calendar_store as store

T0 = "2026-03-10T10:00:00+00:00"
T1 = "2026-03-11T10:00:00+00:00"
T2 = "2026-03-12T10:00:00+00:00"
T3 = "2026-03-13T10:00:00+00:00"


def test_calendar_first_access_is_safe_for_simultaneous_readers_and_writers(tmp_path):
    workers = 12
    barrier = threading.Barrier(workers)
    assert not (tmp_path / "economic_events.sqlite3").exists()
    def save_config(user_id):
        barrier.wait(timeout=10)
        return store.update_config(user_id, {"top_k": user_id + 1})
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(save_config, range(workers)))
    assert [row["top_k"] for row in results] == list(range(1, workers + 1))
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_preferences").fetchone()[0] == workers
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def event(**changes):
    return {
        "source_id": "official_test", "upstream_id": "us-cpi-march-2026",
        "source": "Official statistics", "source_url": "https://www.bls.gov/schedule/",
        "title": "Consumer Price Index CPI", "description": "美国消费者物价数据",
        "region": "US", "currency": "USD", "category": "inflation", "importance": 3,
        "scheduled_at": T0, "timezone": "America/New_York", "all_day": False,
        "status": "released", "actual": "2.8", "forecast": "2.9", "previous": "3.0", "unit": "%",
        **changes,
    }


def revision_at(as_of=T3):
    rows = store.visible_events(as_of=as_of)
    assert len(rows) == 1
    return rows[0]


def counts():
    with store.connection() as conn:
        return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("event_revisions", "event_heads", "event_chunks")}


def test_ingest_persists_real_sqlite_fts_evidence_and_provenance(tmp_path):
    assert store.ingest_events([event()], observed_at=T1) == {"received": 1, "added": 1}
    current = revision_at()
    assert (tmp_path / "economic_events.sqlite3").is_file()
    assert current["observed_at"] == T1
    found = store.matching_chunks("CPI", as_of=T1)
    assert set(found) == {current["revision_id"]}
    assert "公布值：2.8 %" in found[current["revision_id"]]["snippet"]
    assert "https://www.bls.gov/schedule/" in found[current["revision_id"]]["snippet"]
    assert isinstance(found[current["revision_id"]]["rank"], float)
    with store.connection() as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='event_chunks'").fetchone()[0]
        assert "fts5" in sql.lower()
    assert counts() == {"event_revisions": 1, "event_heads": 1, "event_chunks": 1}


def test_identical_content_deduplicates_without_rewriting_first_observation():
    store.ingest_events([event()], observed_at=T1)
    first = revision_at()
    assert store.ingest_events([deepcopy(event())], observed_at=T2) == {"received": 1, "added": 0}
    assert revision_at() == first
    assert counts()["event_revisions"] == counts()["event_chunks"] == 1
    with store.connection() as conn:
        seen = conn.execute("SELECT last_seen_at FROM event_heads").fetchone()[0]
    assert seen == store.timestamp(T2)


def test_changed_values_create_a_new_revision_and_keep_previous_payload():
    store.ingest_events([event(actual="2.8")], observed_at=T1)
    first = revision_at()
    assert store.ingest_events([event(actual="3.1")], observed_at=T2)["added"] == 1
    second = revision_at()
    assert first["id"] == second["id"]
    assert first["revision_id"] != second["revision_id"]
    assert (first["actual"], second["actual"]) == ("2.8", "3.1")
    detail = store.get_event(first["id"])
    assert detail["event"] == second
    assert [row["actual"] for row in detail["revisions"]] == ["3.1", "2.8"]
    assert store.get_event("unknown-id") is None


def test_return_to_previous_content_creates_a_distinct_a_b_a_history():
    for observed, actual in ((T1, "2.8"), (T2, "3.1"), (T3, "2.8")):
        assert store.ingest_events([event(actual=actual)], observed_at=observed)["added"] == 1
    snapshots = [revision_at(as_of) for as_of in (T1, T2, T3)]
    assert [row["actual"] for row in snapshots] == ["2.8", "3.1", "2.8"]
    assert len({row["revision_id"] for row in snapshots}) == 3
    assert len(store.get_event(snapshots[-1]["id"])["revisions"]) == 3
    assert len(store.matching_chunks("CPI", as_of=T3)) == 1


def test_as_of_filters_observations_before_selecting_each_events_latest_revision():
    store.ingest_events([event(title="Original amberannouncement", description="Original")], observed_at=T1)
    first = revision_at()
    store.ingest_events([event(title="Revised cobaltannouncement", description="Revised")], observed_at=T2)
    second = revision_at()
    assert store.visible_events(as_of=T0) == []
    assert revision_at(T1) == first
    assert revision_at(T2) == second
    assert set(store.matching_chunks("amberannouncement", as_of=T1)) == {first["revision_id"]}
    assert store.matching_chunks("cobaltannouncement", as_of=T1) == {}
    # A superseded text match must not resurrect an obsolete event revision.
    assert store.matching_chunks("amberannouncement", as_of=T2) == {}
    assert set(store.matching_chunks("cobaltannouncement", as_of=T2)) == {second["revision_id"]}


def test_newly_collected_past_release_is_not_evidence_before_collection():
    store.ingest_events([event(scheduled_at="2020-01-01T13:30:00Z")], observed_at=T3)
    assert store.visible_events(as_of=T2) == []
    assert store.matching_chunks("CPI", as_of=T2) == {}
    assert len(store.visible_events(as_of=T3)) == 1


def test_same_observation_timestamp_uses_append_order_to_choose_latest_version():
    store.ingest_events([event(actual="2.8"), event(actual="3.1")], observed_at=T1)
    assert revision_at(T1)["actual"] == "3.1"
    assert len(store.get_event(revision_at(T1)["id"])["revisions"]) == 2


def test_backdated_ingestion_is_rejected_even_after_unchanged_refresh():
    store.ingest_events([event()], observed_at=T1)
    store.ingest_events([event()], observed_at=T3)
    before = revision_at()
    with pytest.raises(ValueError, match="倒填"):
        store.ingest_events([event(actual="9.9")], observed_at=T2)
    assert revision_at() == before
    assert counts()["event_revisions"] == 1


@pytest.mark.parametrize("sql", [
    "UPDATE event_revisions SET observed_at=1",
    "UPDATE event_revisions SET payload='{}'",
    "DELETE FROM event_revisions",
])
def test_database_triggers_reject_mutating_or_deleting_saved_revisions(sql):
    store.ingest_events([event()], observed_at=T1)
    original = revision_at()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.connection() as conn:
            conn.execute(sql)
    assert revision_at() == original


def test_returned_payload_mutations_do_not_modify_durable_evidence():
    store.ingest_events([event()], observed_at=T1)
    modified = revision_at()
    modified["actual"] = "999"
    assert revision_at()["actual"] == "2.8"


@pytest.mark.parametrize("query", [
    'CPI" OR * NEAR(', "CPI'); DROP TABLE event_revisions; --",
    'CPI OR revision_id:*', 'CPI AND NOT (title:"',
])
def test_malicious_match_syntax_is_tokenized_without_sql_or_fts_injection(query):
    store.ingest_events([
        event(), event(upstream_id="payroll", title="Payroll release", description="Employment", category="employment"),
    ], observed_at=T1)
    cpi = next(row for row in store.visible_events(as_of=T1) if row["upstream_id"] == "us-cpi-march-2026")
    assert set(store.matching_chunks(query, as_of=T1)) == {cpi["revision_id"]}
    assert counts()["event_revisions"] == 2
    assert len(store.visible_events(as_of=T1)) == 2


def test_chinese_bigrams_and_english_category_terms_find_evidence():
    store.ingest_events([event()], observed_at=T1)
    current = revision_at()
    for query in ("消费者物价", "通胀", "inflation", "consumer prices", "CPI"):
        assert current["revision_id"] in store.matching_chunks(query, as_of=T1), query
    assert store.matching_chunks("*\"()'", as_of=T1) == {}
    assert store.matching_chunks("unrelatedtoken", as_of=T1) == {}


def test_long_document_chunks_are_reduced_to_one_result_per_revision():
    description = ("Economic detail " * 200) + " uniquetailfinding "
    store.ingest_events([event(description=description)], observed_at=T1)
    current = revision_at()
    assert counts()["event_chunks"] > 1
    assert set(store.matching_chunks("uniquetailfinding", as_of=T1)) == {current["revision_id"]}
    assert len(store.matching_chunks("CPI", as_of=T1)) == 1


def test_zero_values_remain_evidence_and_missing_values_remain_none():
    store.ingest_events([event(actual=0, forecast=0.0, previous=None)], observed_at=T1)
    current = revision_at()
    assert current["actual"] == "0"
    assert current["forecast"] == "0.0"
    assert current["previous"] is None
    assert current["status"] == "released"
    assert "公布值：0 %" in store.document_text(current)
    assert "前值" not in store.document_text(current)


@pytest.mark.parametrize("bad", [True, False, float("nan"), float("inf"), float("-inf")])
def test_invalid_numeric_data_is_rejected_without_partial_ingest(bad):
    with pytest.raises(ValueError, match="数值"):
        store.ingest_events([event(), event(upstream_id="invalid", actual=bad)], observed_at=T1)
    assert store.visible_events(as_of=T3) == []


def test_future_actuals_are_removed_but_forecast_and_previous_are_preserved():
    store.ingest_events([event(scheduled_at=T3, actual="9.9", forecast=0, previous="2.0", status="released")], observed_at=T1)
    future = revision_at(T1)
    assert future["actual"] is None
    assert future["status"] == "scheduled"
    assert future["forecast"] == "0"
    assert future["previous"] == "2.0"
    assert "公布值" not in store.document_text(future)
    assert "9.9" not in store.document_text(future)


def test_cancelled_future_event_stays_cancelled_and_has_no_actual():
    store.ingest_events([event(scheduled_at=T3, status="cancelled", actual=5)], observed_at=T1)
    current = revision_at(T1)
    assert current["status"] == "cancelled"
    assert current["actual"] is None


def test_precise_events_use_shanghai_day_and_date_only_events_keep_source_day():
    raw = event(scheduled_at="2026-03-10T23:30:00-04:00", timezone="America/New_York")
    precise = store.normalise(raw, store.timestamp(T2))
    assert precise["scheduled_at"] == "2026-03-11T03:30:00+00:00"
    assert precise["scheduled_date"] == "2026-03-10"
    assert precise["calendar_date"] == "2026-03-11"
    date_only = store.normalise({**raw, "all_day": True}, store.timestamp(T2))
    assert date_only["scheduled_date"] == date_only["calendar_date"] == "2026-03-10"
    assert "准确发布时间待定" in store.document_text(date_only)


def test_dst_offsets_are_normalized_to_actual_utc_instants():
    before = store.normalise(event(scheduled_at="2026-03-08T01:30:00-05:00"), store.timestamp(T1))
    after = store.normalise(event(scheduled_at="2026-03-08T03:30:00-04:00"), store.timestamp(T1))
    assert store.timestamp(after["scheduled_at"]) - store.timestamp(before["scheduled_at"]) == 3600
    assert before["scheduled_date"] == after["scheduled_date"] == "2026-03-08"


@pytest.mark.parametrize("bad", [True, False, "2026-03-10T12:00:00", float("nan"), float("inf"), 0])
def test_invalid_or_timezone_naive_timestamps_cannot_enter_history(bad):
    with pytest.raises(ValueError):
        store.ingest_events([event(scheduled_at=bad)], observed_at=T1)


@pytest.mark.parametrize("patch", [
    None, [], {"unknown": True}, {"enabled": 1}, {"enabled": "false"},
    {"agents": []}, {"agents": {"new_agent": True}}, {"agents": {"research": 1}},
    {"lookback_days": -1}, {"lookback_days": 366}, {"lookback_days": True},
    {"lookahead_days": -1}, {"lookahead_days": 366}, {"lookahead_days": 1.0},
    {"min_importance": 0}, {"min_importance": 4}, {"top_k": 0}, {"top_k": 31}, {"top_k": "8"},
    {"regions": "US"}, {"regions": ["ZZ"]}, {"regions": ["US"] * 11},
    {"categories": "inflation"}, {"categories": ["unsupported"]},
])
def test_preferences_validate_types_enums_and_bounds_before_writing(patch):
    original = store.get_config(71)
    with pytest.raises(ValueError):
        store.update_config(71, patch)
    assert store.get_config(71) == original


def test_preferences_merge_individual_agents_and_isolate_users():
    defaults = deepcopy(store.DEFAULT_CONFIG)
    saved = store.update_config(71, {"agents": {"news": False}, "regions": ["US"], "categories": ["inflation"], "top_k": 4})
    assert saved["agents"] == {"news": False, "research": True, "strategies": True, "kline": True}
    assert saved["regions"] == ["US"]
    next_config = store.update_config(71, {"agents": {"kline": False}, "enabled": False})
    assert next_config["agents"]["news"] is False
    assert next_config["agents"]["kline"] is False
    assert next_config["top_k"] == 4
    assert next_config["enabled"] is False
    assert store.get_config(72) == defaults
    assert store.get_config(None) == defaults
    assert store.DEFAULT_CONFIG == defaults
    next_config["agents"]["research"] = False
    assert store.get_config(71)["agents"]["research"] is True


def test_preferences_allow_boundaries_and_explicitly_cleared_filters():
    store.update_config(71, {"regions": ["US", "CN"], "categories": ["central_bank"]})
    result = store.update_config(71, {"regions": [], "categories": [], "lookback_days": 0, "lookahead_days": 365, "top_k": 30, "min_importance": 1})
    assert result["regions"] == result["categories"] == []
    assert result["lookback_days"] == 0
    assert result["lookahead_days"] == 365
    assert result["top_k"] == 30
    assert result["min_importance"] == 1
