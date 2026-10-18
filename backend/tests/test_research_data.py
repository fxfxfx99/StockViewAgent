import asyncio
import copy
from datetime import datetime, timezone, timedelta

import pytest

from app.services.research_data import ResearchData, SnapshotIntegrityError


def sample(*, strict=False, dates=("2025-01-06", "2025-01-07")):
    bars = []
    for i, date in enumerate(dates):
        t = int(datetime.fromisoformat(date + "T00:00:00+08:00").timestamp())
        row = {"t": t, "o": 10 + i, "h": 12 + i, "l": 9 + i, "c": 11 + i, "v": 5000}
        if strict:
            row.update(source_time=t + 15 * 3600, available_at=t + 16 * 3600, tradable=True,
                       suspended=False, st=False, no_price_limit=False, limit_up=15.0, limit_down=8.0,
                       corporate_actions_checked=True)
        bars.append(row)
    return {"kline_source": "fixture_provider", "price_basis": "raw", "volume_unit": "shares", "candles": bars}


def provenance():
    return {"source": "fixture_provider", "fetched_at": "2025-01-08T12:00:00+08:00",
            "calendar": ["2025-01-06", "2025-01-07"], "corporate_actions": [],
            "instrument_history": [{"symbol": "600000.SS", "valid_from": "2020-01-01", "valid_to": None,
                                    "available_at": "2020-01-01T00:00:00+08:00"}],
            "coverage": {"start": "2025-01-06", "end": "2025-01-07", "symbols": ["600000.SS"],
                         "corporate_actions_complete": True, "instrument_history_complete": True}}


def test_immutable_snapshot_roundtrip_strict_and_user_isolation(tmp_path):
    data = ResearchData(tmp_path)
    snap = data.import_snapshot(1, {"600000.SS": sample(strict=True)}, metadata=provenance())
    assert snap["payload"]["strict"] is True
    assert snap["payload"]["quality"] == {"eligible": True, "issues": [], "warnings": []}
    rows = ResearchData(tmp_path).read_snapshot(1, snap["id"])
    assert len(rows) == 2 and rows[0]["v"] == 5000
    assert rows[0]["corporate_actions_checked"] is True
    assert rows[0]["source_time"] < rows[0]["available_at"]
    assert data.read_snapshot(1, snap["id"], as_of=rows[0]["available_at"] - 1) == []
    with pytest.raises(ValueError):
        data.read_snapshot(2, snap["id"])
    batch_id = snap["payload"]["batch_ids"][0]
    with pytest.raises(ValueError):
        data.create_snapshot(2, [batch_id], metadata=provenance())


def test_cache_style_input_is_exploratory_not_strict(tmp_path):
    data = ResearchData(tmp_path)
    batch = data.ingest_bundle(1, "600000.SS", sample(), metadata={"fetched_at": "2025-01-08T00:00:00Z"})
    snap = data.create_snapshot(1, [batch["id"]])
    assert not snap["payload"]["strict"]
    issues = snap["payload"]["quality"]["issues"]
    assert "historical_availability_missing" in issues
    assert "trading_calendar_missing" in issues
    assert "corporate_actions_history_missing" in issues


def test_snapshot_tampering_and_missing_batch_fail_closed(tmp_path):
    data = ResearchData(tmp_path)
    snap = data.import_snapshot(1, {"600000.SS": sample()}, metadata=provenance())
    path = data.batches / snap["payload"]["batch_ids"][0] / "bars.parquet"
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(SnapshotIntegrityError):
        data.read_snapshot(1, snap["id"])
    path.unlink()
    with pytest.raises(SnapshotIntegrityError):
        data.get_snapshot(1, snap["id"])


def test_manifest_tampering_and_invalid_bars(tmp_path):
    data = ResearchData(tmp_path)
    batch = data.ingest_bundle(1, "600000.SS", sample(), metadata=provenance())
    manifest = data.batches / batch["id"] / "manifest.json"
    manifest.write_text(manifest.read_text().replace('"row_count":2', '"row_count":99'))
    with pytest.raises(SnapshotIntegrityError):
        data.create_snapshot(1, [batch["id"]])
    bad = sample(); bad["candles"].append(bad["candles"][0])
    with pytest.raises(ValueError, match="Duplicate"):
        data.ingest_bundle(1, "600000.SS", bad)


def test_latest_revision_preserves_older_snapshot_and_parent_rows(tmp_path):
    data = ResearchData(tmp_path)
    first = data.ingest_bundle(1, "600000.SS", sample(strict=True), metadata=provenance())
    old = data.create_snapshot(1, [first["id"]], metadata=provenance())
    delta = sample(strict=True, dates=("2025-01-07",))
    delta["candles"][0]["c"] = 10.5
    newer_meta = {**provenance(), "fetched_at": "2025-01-09T00:00:00Z", "parent_batch_ids": [first["id"]]}
    second = data.ingest_bundle(1, "600000.SS", delta, metadata=newer_meta)
    new = data.create_snapshot(1, [second["id"]], metadata=provenance())
    assert len(data.read_snapshot(1, new["id"])) == 2
    assert data.read_snapshot(1, new["id"])[-1]["c"] == 10.5
    assert data.read_snapshot(1, old["id"])[-1]["c"] == 12
    assert new["payload"]["batch_ids"] == [first["id"], second["id"]]


def test_missing_sessions_cannot_be_certified_by_declaration(tmp_path):
    data = ResearchData(tmp_path)
    meta = provenance(); meta["calendar"].append("2025-01-08"); meta["coverage"]["end"] = "2025-01-09"
    bundle = sample(strict=True, dates=("2025-01-06", "2025-01-09"))
    meta["calendar"].append("2025-01-09")
    snap = data.import_snapshot(1, {"600000.SS": bundle}, metadata=meta)
    assert "missing_session_bars" in snap["payload"]["blockers"]


def test_offline_cache_fallback_archives_staleness(tmp_path, monkeypatch):
    from app.storage import kline_bundle_cache
    from app.services import kline_pipeline
    async def fail(*args):
        raise RuntimeError("fixture offline")
    monkeypatch.setattr(kline_pipeline, "fetch_a_share_kline_with_fallbacks", fail)
    kline_bundle_cache.save_bundle("600000.SS", "5y", "1d", sample(strict=True))
    data = ResearchData(tmp_path)
    batch = asyncio.run(data.fetch_history(1, "600000.SS"))
    snap = data.create_snapshot(1, [batch["id"]], metadata=provenance())
    assert "offline_or_stale_cache" in snap["payload"]["blockers"]
    assert not snap["payload"]["quality"]["eligible"]


def test_fetch_incremental_short_window_and_no_source_mixing(tmp_path, monkeypatch):
    from app.services import kline_pipeline
    now = datetime(2025, 1, 8, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("app.services.research_data.time.time", lambda: now)
    calls = []
    async def fetch(sym, range_param, interval):
        calls.append(range_param)
        if len(calls) == 1:
            dates = []
            day = datetime(2020, 1, 1)
            while day <= datetime(2025, 1, 7):
                if day.weekday() < 5:
                    dates.append(day.date().isoformat())
                day += timedelta(days=1)
            result = sample(dates=dates)
        else:
            result = sample(dates=("2025-01-07", "2025-01-08"))
        if len(calls) == 3:
            result["kline_source"] = "different_provider"
        return result
    monkeypatch.setattr(kline_pipeline, "fetch_a_share_kline_with_fallbacks", fetch)
    data = ResearchData(tmp_path)
    first = asyncio.run(data.fetch_history(1, "600000.SS"))
    second = asyncio.run(data.fetch_history(1, "600000.SS"))
    snap = data.create_snapshot(1, [second["id"]])
    assert calls == ["5y", "1mo"]
    assert len(data.read_snapshot(1, snap["id"])) == first["row_count"] + 1
    assert first["id"] in snap["payload"]["batch_ids"]
    third = asyncio.run(data.fetch_history(1, "600000.SS"))
    branch = data.create_snapshot(1, [third["id"]])
    assert len(branch["payload"]["batch_ids"]) == 1
    assert "source_or_price_basis_branch_changed" in branch["payload"]["blockers"]


def test_baostock_volumes_and_price_basis_are_exact(monkeypatch):
    from app.services import kline_baostock as bs
    daily = bs._rows_to_daily_candles([["2025-01-06", "10", "12", "9", "11", "5000", "55000"]])
    minute = bs._rows_to_minute_candles([["2025-01-06", "20250106103000000", "10", "12", "9", "11", "5000", "55000"]])
    assert daily[0]["v"] == minute[0]["v"] == 5000
    monkeypatch.setattr("app.services.eastmoney_market._build_metrics", lambda *args: {})
    bundle = bs._bundle_from_candles("600000.SS", "1mo", "1d", "1d", daily, "test")
    assert bundle["price_basis"] == "raw" and bundle["volume_unit"] == "shares"


def test_cross_source_manual_snapshot_is_blocked(tmp_path):
    data = ResearchData(tmp_path)
    first = data.ingest_bundle(1, "600000.SS", sample(strict=True), metadata=provenance())
    second_meta = {**provenance(), "source": "other_provider"}
    second = data.ingest_bundle(1, "600000.SS", sample(strict=True), metadata=second_meta)
    snapshot = data.create_snapshot(1, [first["id"], second["id"]], metadata=provenance())
    assert "mixed_source_or_price_basis" in snapshot["payload"]["blockers"]


def test_sparse_five_year_span_still_requests_full_history(tmp_path, monkeypatch):
    from app.services import kline_pipeline
    monkeypatch.setattr("app.services.research_data.time.time", lambda: datetime(2025, 1, 8, tzinfo=timezone.utc).timestamp())
    calls = []
    async def fetch(sym, range_param, interval):
        calls.append(range_param)
        return sample(dates=("2020-01-01", "2025-01-07"))
    monkeypatch.setattr(kline_pipeline, "fetch_a_share_kline_with_fallbacks", fetch)
    data = ResearchData(tmp_path)
    asyncio.run(data.fetch_history(1, "600000.SS"))
    asyncio.run(data.fetch_history(1, "600000.SS"))
    assert calls == ["5y", "5y"]


def test_offline_can_use_imported_immutable_data_without_chart_cache(tmp_path):
    data = ResearchData(tmp_path)
    snapshot = data.import_snapshot(1, {"600000.SS": sample(strict=True)}, metadata=provenance())
    archived = asyncio.run(data.fetch_history(1, "600000.SS", offline=True))
    assert archived["id"] != snapshot["payload"]["batch_ids"][0]
    assert "offline_or_stale_cache" in archived["blockers"]
    assert data.read_snapshot(1, snapshot["id"])[0]["corporate_actions_checked"] is True


@pytest.mark.parametrize("cash,ratio,eligible", [(0, 2, True), (.5, 1, False)])
def test_supported_action_roundtrips_but_gross_dividends_are_exploratory(tmp_path, cash, ratio, eligible):
    data = ResearchData(tmp_path)
    bundle, meta = sample(strict=True), provenance()
    action = {"symbol": "600000.SS", "ex_date": "2025-01-07", "available_at": "2025-01-06T18:00:00+08:00",
              "cash_dividend": cash, "split_ratio": ratio}
    meta["corporate_actions"] = [action]
    bundle["candles"][1]["corporate_action"] = action
    snap = data.import_snapshot(1, {"600000.SS": bundle}, metadata=meta)
    assert snap["payload"]["strict"] is eligible
    assert snap["payload"]["quality"]["eligible"] is eligible
    if cash:
        assert "cash_dividend_net_payment_unverified" in snap["payload"]["blockers"]
        assert "gross" in snap["payload"]["quality"]["warnings"][0]
    restored = data.read_snapshot(1, snap["id"])
    assert restored[1]["corporate_action"] == action


def test_action_late_or_mismatched_evidence_cannot_be_strict(tmp_path):
    data = ResearchData(tmp_path)
    bundle, meta = sample(strict=True), provenance()
    action = {"symbol": "600000.SS", "ex_date": "2025-01-07", "available_at": "2025-01-07T10:00:00+08:00",
              "cash_dividend": .5, "split_ratio": 1.0}
    meta["corporate_actions"] = [action]
    bundle["candles"][1]["corporate_action"] = action
    late = data.import_snapshot(1, {"600000.SS": bundle}, metadata=meta)
    assert "corporate_action_not_known_before_open" in late["payload"]["quality"]["issues"]
    action["available_at"] = "2025-01-06T18:00:00+08:00"
    bundle["candles"][1]["corporate_action"] = {**action, "cash_dividend": .6}
    mismatch = data.import_snapshot(1, {"600000.SS": bundle}, metadata=meta)
    assert "corporate_action_execution_mismatch" in mismatch["payload"]["quality"]["issues"]
    assert "corporate_action_history_incomplete" in mismatch["payload"]["quality"]["issues"]


def test_action_missing_metadata_cannot_hide_behind_complete_coverage(tmp_path):
    data = ResearchData(tmp_path)
    bundle, meta = sample(strict=True), provenance()
    bundle["candles"][1]["corporate_actions"] = [{"available_at": "2025-01-06T18:00:00+08:00",
                                                 "cash_dividend": 0, "split_ratio": 2}]
    snap = data.import_snapshot(1, {"600000.SS": bundle}, metadata=meta)
    assert "corporate_action_history_incomplete" in snap["payload"]["quality"]["issues"]


def test_fetch_old_data_keeps_requested_window_and_marks_stale(tmp_path, monkeypatch):
    from app.services import kline_pipeline
    async def fetch(*args):
        return sample(strict=True)
    monkeypatch.setattr(kline_pipeline, "fetch_a_share_kline_with_fallbacks", fetch)
    data = ResearchData(tmp_path)
    result = asyncio.run(data.fetch_bundle(1, "600000.SS", "1mo"))
    assert len(result["candles"]) == 2
    assert result["data_as_of"] == "2025-01-07"
    assert result["cache_stale"]
    assert result["range_anchor"] == "latest_available_session"


def test_bad_acquired_data_cannot_fallback_or_overwrite_good_cache(tmp_path, monkeypatch):
    from app.services import kline_pipeline
    from app.services.research_data import MarketDataValidationError
    from app.storage import kline_bundle_cache
    good = sample()
    kline_bundle_cache.save_bundle("600000.SS", "1y", "1d", good)
    async def fetch(*args):
        bad = sample()
        bad["candles"][0]["h"] = 1
        return bad
    monkeypatch.setattr(kline_pipeline, "fetch_a_share_kline_with_fallbacks", fetch)
    data = ResearchData(tmp_path)
    with pytest.raises(MarketDataValidationError, match="Inconsistent OHLC"):
        asyncio.run(data.fetch_bundle(1, "600000.SS", "1y"))
    assert kline_bundle_cache.load_bundle("600000.SS", "1y", "1d")["payload"] == good
    assert data.store.get_state(1, "history_head:600000.SS:1d") is None


def test_empty_acquisition_is_renderable_but_never_archived(tmp_path, monkeypatch):
    from app.services import kline_pipeline
    from app.storage import kline_bundle_cache
    good = sample()
    kline_bundle_cache.save_bundle("600000.SS", "1y", "1d", good)
    async def fetch(*args):
        return {"candles": [], "metrics": {}}
    monkeypatch.setattr(kline_pipeline, "fetch_a_share_kline_with_fallbacks", fetch)
    data = ResearchData(tmp_path)
    result = asyncio.run(data.fetch_bundle(1, "600000.SS", "1y"))
    assert result["candles"] == [] and not result["research_quality"]["eligible"]
    assert "research_batch_id" not in result
    assert kline_bundle_cache.load_bundle("600000.SS", "1y", "1d")["payload"] == good
    with pytest.raises(ValueError, match="No daily candles"):
        asyncio.run(data.fetch_history(1, "600000.SS"))


@pytest.mark.parametrize("level,flag", [("metadata", "example_only"), ("metadata", "synthetic"),
                                        ("bundle", "synthetic"), ("bar", "example_only")])
def test_explicit_examples_and_synthetic_bars_always_block_strict(tmp_path, level, flag):
    data = ResearchData(tmp_path)
    bundle, meta = sample(strict=True), provenance()
    target = meta if level == "metadata" else bundle if level == "bundle" else bundle["candles"][0]
    target[flag] = True
    snap = data.import_snapshot(1, {"600000.SS": bundle}, metadata=meta)
    assert not snap["payload"]["strict"]
    assert "example_or_synthetic_data" in snap["payload"]["blockers"]
    rows = data.read_snapshot(1, snap["id"])
    assert rows[0][flag] is True


def test_format_fixture_source_cannot_be_strict_without_example_flag(tmp_path):
    data = ResearchData(tmp_path)
    meta = {**provenance(), "source": "format_fixture_not_market_data"}
    snap = data.import_snapshot(1, {"600000.SS": sample(strict=True)}, metadata=meta)
    assert "example_or_synthetic_data" in snap["payload"]["blockers"]


@pytest.mark.parametrize("source_offset,available_offset,expected_issue", [
    (15 * 3600, 16 * 3600, None),
    (15 * 3600, 16 * 3600 + 30 * 60, None),
    (15 * 3600, 16 * 3600 + 30 * 60 + 1, "availability_after_decision_time"),
    (15 * 3600 - 1, 16 * 3600, "source_time_before_bar_close"),
    (15 * 3600 - 2, 15 * 3600 - 1, "daily_bar_available_before_close"),
])
def test_strict_daily_data_requires_final_bar_by_close_decision(tmp_path, source_offset, available_offset, expected_issue):
    data = ResearchData(tmp_path)
    bundle = sample(strict=True)
    for bar in bundle["candles"]:
        bar.update(source_time=bar["t"] + source_offset, available_at=bar["t"] + available_offset)
    snapshot = data.import_snapshot(1, {"600000.SS": bundle}, metadata=provenance())
    assert snapshot["payload"]["strict"] is (expected_issue is None)
    if expected_issue:
        assert expected_issue in snapshot["payload"]["blockers"]


def test_2026_arrival_cannot_certify_2021_bar_as_historically_available(tmp_path):
    data = ResearchData(tmp_path)
    dates = ("2021-01-06", "2021-01-07")
    bundle = sample(strict=True, dates=dates)
    meta = provenance()
    meta.update(calendar=list(dates), fetched_at="2026-01-08T12:00:00+08:00")
    meta["coverage"].update(start=dates[0], end=dates[-1])
    bundle["candles"][0]["available_at"] = "2026-01-08T11:00:00+08:00"
    snapshot = data.import_snapshot(1, {"600000.SS": bundle}, metadata=meta)
    assert not snapshot["payload"]["strict"]
    assert "availability_after_decision_time" in snapshot["payload"]["blockers"]


def test_late_revision_keeps_original_snapshot_but_cannot_be_strict(tmp_path):
    data = ResearchData(tmp_path)
    first = data.ingest_bundle(1, "600000.SS", sample(strict=True), metadata=provenance())
    original = data.create_snapshot(1, [first["id"]], metadata=provenance())
    delta = sample(strict=True, dates=("2025-01-07",))
    delta["candles"][0].update(c=10.5, available_at="2026-01-08T10:00:00+08:00")
    second = data.ingest_bundle(1, "600000.SS", delta,
        metadata={**provenance(), "fetched_at": "2026-01-08T12:00:00+08:00", "parent_batch_ids": [first["id"]]})
    revised = data.create_snapshot(1, [second["id"]], metadata=provenance())
    assert original["payload"]["strict"]
    assert data.read_snapshot(1, original["id"])[-1]["c"] == 12
    assert data.read_snapshot(1, revised["id"])[-1]["c"] == 10.5
    assert "availability_after_decision_time" in revised["payload"]["blockers"]
    assert len(data.read_snapshot(1, revised["id"], as_of="2025-01-07T16:30:00+08:00")) == 1


@pytest.mark.parametrize("explicit_flag", [False, True])
def test_inferred_arrival_is_preserved_but_never_strict(tmp_path, explicit_flag):
    from app.services.trading_rules import daily_bar_availability_issues
    data = ResearchData(tmp_path)
    bundle = sample(strict=True)
    for bar in bundle["candles"]:
        if explicit_flag:
            bar.update(availability_inferred=True, available_at="2026-01-08T10:00:00+08:00")
        else:
            bar.pop("available_at")
    snapshot = data.import_snapshot(1, {"600000.SS": bundle},
        metadata={**provenance(), "fetched_at": "2026-01-08T12:00:00+08:00"})
    assert not snapshot["payload"]["strict"]
    assert "historical_availability_missing" in snapshot["payload"]["blockers"]
    rows = data.read_snapshot(1, snapshot["id"])
    assert all(row["availability_inferred"] is True for row in rows)
    assert daily_bar_availability_issues(rows[0], rows[0]["date"]) == []
    assert "historical_availability_missing" in daily_bar_availability_issues(rows[0], rows[0]["date"], require_metadata=True)
