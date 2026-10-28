"""Immutable research inputs: provider JSON, typed Parquet, hashed manifests.

The legacy chart cache is an acquisition fallback, never point-in-time evidence.
Strict imports must supply the actual historical trading/availability records and
coverage described by ``IMPORT_REQUIREMENTS``. Missing evidence fails closed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import threading
from typing import Any
from zoneinfo import ZoneInfo

from app.storage import research_store as store
from app.services import trading_rules

SHANGHAI = ZoneInfo("Asia/Shanghai")
_CATALOG_LOCK = threading.Lock()
IMPORT_REQUIREMENTS = {
    "bars": ["t,o,h,l,c,v", "price_basis=raw (or execution_o and execution_c)",
             "volume_unit=shares", "source_time", "available_at", "tradable", "suspended", "st",
             "limit_up and limit_down (or no_price_limit=true)", "corporate_actions_checked=true"],
    "metadata": ["source", "calendar: YYYY-MM-DD sessions", "instrument_history: symbol,valid_from,valid_to,available_at",
                 "corporate_actions: symbol,ex_date,available_at,cash_dividend,split_ratio",
                 "coverage: start,end,symbols,corporate_actions_complete=true,instrument_history_complete=true"],
    "daily_decision_window": "Final daily source_time and available_at must fall between 15:00 and 16:30 inclusive on that trading date (Asia/Shanghai). Later revisions are exploratory and cannot stand in for the original historically available bar.",
    "notes": "Empty corporate_actions requires explicit complete coverage. Cash/split events must also appear in the ex-date bar's corporate_action object/list, with cash_dividend per old share, split_ratio and an available_at timestamp strictly before 09:30 Asia/Shanghai. Cash dividends are exploratory only: gross before investor tax, credited on the ex-date, so actual tax and payment-date accounting are unverified and automatic promotion is prohibited. Fractional-share cash settlement is not inferred. Provider declarations are not an audit guarantee; current caches cannot certify historical availability.",
}


class SnapshotIntegrityError(ValueError):
    pass


class MarketDataValidationError(ValueError):
    """A returned source payload is malformed; do not conceal it with fallback."""


def _bar_content(row):
    """Market revisions exclude acquisition clocks, including inferred availability."""
    value = {k: v for k, v in row.items() if k not in ("fetched_at", "extra_json", "batch_id")}
    if value.get("availability_inferred"):
        value.pop("available_at", None)
    return store.canonical_json(value)


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _ts(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a timestamp")
    if isinstance(value, (float, int)):
        v = float(value)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("Timestamps must include a timezone")
        v = dt.timestamp()
    if not math.isfinite(v) or v <= 0:
        raise ValueError("Invalid timestamp")
    return v


def _arrow_schema():
    import pyarrow as pa
    fields = [("symbol", pa.string()), ("date", pa.string()), ("t", pa.int64())]
    fields += [(key, pa.float64()) for key in ("o", "h", "l", "c", "v", "amount", "source_time", "fetched_at", "available_at", "adjust_factor", "execution_o", "execution_c", "limit_up", "limit_down")]
    fields += [(key, pa.string()) for key in ("source", "price_basis", "volume_unit", "extra_json")]
    fields += [(key, pa.bool_()) for key in ("tradable", "suspended", "st", "no_price_limit", "corporate_actions_checked")]
    return pa.schema(fields)


def _normalise(symbol, bundle, metadata, fetched_at):
    rows, blockers = [], set()
    source = str(metadata.get("source") or bundle.get("kline_source") or "unknown")
    if source in ("unknown", "local_cache"):
        blockers.add("source_unknown")
    if source == "format_fixture_not_market_data" or any(obj.get("example_only") or obj.get("synthetic") for obj in (bundle, metadata)):
        blockers.add("example_or_synthetic_data")
    if bundle.get("cache_stale") or metadata.get("offline_fallback"):
        blockers.add("offline_or_stale_cache")
    blockers.update(metadata.get("quality_blockers") or [])
    seen, previous = set(), None
    schema_names = set(_arrow_schema().names)
    for raw in bundle.get("candles") or []:
        if raw.get("example_only") or raw.get("synthetic"):
            blockers.add("example_or_synthetic_data")
        t = int(_ts(raw.get("t")) or 0)
        if not t:
            raise ValueError("Candle timestamp missing")
        if t in seen:
            raise ValueError("Duplicate candle timestamp")
        if previous is not None and t <= previous:
            raise ValueError("Candle timestamps must be increasing")
        seen.add(t); previous = t
        row = {"symbol": symbol, "t": t, "date": datetime.fromtimestamp(t, SHANGHAI).date().isoformat(), "source": source, "fetched_at": fetched_at}
        for key in ("o", "h", "l", "c", "v"):
            value = float(raw[key])
            if not math.isfinite(value) or (value <= 0 if key != "v" else value < 0):
                raise ValueError(f"Invalid {key} at {t}")
            row[key] = value
        if row["h"] < max(row["o"], row["c"], row["l"]) or row["l"] > min(row["o"], row["c"]):
            raise ValueError("Inconsistent OHLC candle")
        for key in ("price_basis", "volume_unit"):
            row[key] = str(raw.get(key) or metadata.get(key) or bundle.get(key) or "unknown")
        for key in ("amount", "adjust_factor", "execution_o", "execution_c", "limit_up", "limit_down"):
            value = raw.get(key)
            row[key] = None if value is None else float(value)
            if row[key] is not None and not math.isfinite(row[key]):
                raise ValueError(f"Nonfinite {key}")
        for key in ("tradable", "suspended", "st", "no_price_limit", "corporate_actions_checked"):
            value = raw.get(key, metadata.get(key))
            if value is not None and type(value) is not bool:
                raise ValueError(f"{key} must be a boolean when provided")
            row[key] = value
        row["source_time"] = _ts(raw.get("source_time"))
        explicit_available = raw.get("available_at")
        row["available_at"] = _ts(explicit_available) if explicit_available is not None else fetched_at
        row["availability_inferred"] = explicit_available is None or raw.get("availability_inferred") is True
        if row["availability_inferred"] or row["source_time"] is None:
            blockers.add("historical_availability_missing")
        if row["available_at"] < t or (row["source_time"] is not None and row["available_at"] < row["source_time"]):
            blockers.add("invalid_availability_order")
        if row["available_at"] > fetched_at:
            blockers.add("availability_after_fetch")
        if row["source_time"] is not None and row["source_time"] < t:
            blockers.add("source_time_precedes_bar")
        blockers.update(trading_rules.daily_bar_availability_issues(row, row["date"], require_metadata=True))
        if row["price_basis"] != "raw" and not (row["execution_o"] and row["execution_c"] and row["execution_o"] > 0 and row["execution_c"] > 0):
            blockers.add("raw_execution_prices_missing")
        if row["volume_unit"] != "shares":
            blockers.add("volume_unit_unknown")
        if row["tradable"] is None or row["suspended"] is None or row["st"] is None:
            blockers.add("historical_trading_status_missing")
        if row["tradable"] is True and row["suspended"] is True:
            blockers.add("contradictory_trading_status")
        if row["no_price_limit"] is not True and not (row["limit_up"] is not None and row["limit_down"] is not None and 0 < row["limit_down"] < row["limit_up"]):
            blockers.add("historical_price_limits_missing")
        if row["corporate_actions_checked"] is not True:
            blockers.add("corporate_actions_unchecked")
        actions, action_issues = trading_rules.parse_corporate_actions(raw, row["date"])
        blockers.update(action_issues)
        if any(a["cash_dividend"] > 0 for a in actions):
            blockers.add("cash_dividend_net_payment_unverified")
        if row["date"] < "2015-08-01":
            blockers.add("historical_fee_schedule_requires_override")
        extras = {k: v for k, v in raw.items() if k not in schema_names}
        extras["availability_inferred"] = row["availability_inferred"]
        for flag in ("example_only", "synthetic"):
            if any(obj.get(flag) for obj in (raw, bundle, metadata)):
                extras[flag] = True
        row["extra_json"] = store.canonical_json(extras)
        rows.append(row)
    if not rows:
        raise ValueError("No candles to archive")
    return rows, sorted(blockers)


class ResearchData:
    def __init__(self, data_dir: str | Path | None = None):
        self.store = store.ResearchStore(data_dir)
        self.root = self.store.root / "research"
        self.batches = self.root / "batches"
        self.batches.mkdir(parents=True, exist_ok=True)

    def _batch_path(self, batch_id):
        if not re.fullmatch(r"[0-9a-f]{64}", str(batch_id)):
            raise SnapshotIntegrityError("Invalid batch id")
        return self.batches / batch_id

    def ingest_bundle(self, user_id, symbol, bundle, *, metadata=None, collection_stats=None):
        import duckdb
        import pyarrow as pa
        import pyarrow.parquet as pq

        symbol = str(symbol).strip().upper()
        if not symbol or not isinstance(bundle, dict):
            raise ValueError("Symbol and bundle required")
        metadata = dict(metadata or {})
        fetched_at = _ts(metadata.get("fetched_at")) or time.time()
        rows, blockers = _normalise(symbol, bundle, metadata, fetched_at)
        # JSON is the exact acquired bundle, plus separate provenance supplied by
        # the caller. Content identity includes acquisition time and provenance.
        raw = {"symbol": symbol, "bundle": bundle, "metadata": metadata, "fetched_at": fetched_at}
        batch_id = store.content_hash(raw)
        final = self._batch_path(batch_id)
        published = False
        if not final.exists():
            stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.batches))
            try:
                (stage / "raw.json").write_text(store.canonical_json(raw), encoding="utf-8")
                pq.write_table(pa.Table.from_pylist(rows, schema=_arrow_schema()), stage / "bars.parquet", compression="zstd")
                manifest = {"version": 1, "batch_id": batch_id, "symbol": symbol,
                            "row_count": len(rows), "start": rows[0]["date"], "end": rows[-1]["date"],
                            "source": rows[0]["source"], "fetched_at": fetched_at,
                            "price_basis": sorted({r["price_basis"] for r in rows}),
                            "volume_unit": sorted({r["volume_unit"] for r in rows}),
                            "blockers": blockers, "parent_batch_ids": list(metadata.get("parent_batch_ids") or []),
                            "files": {name: _hash_file(stage / name) for name in ("raw.json", "bars.parquet")}}
                (stage / "manifest.json").write_text(store.canonical_json(manifest), encoding="utf-8")
                for path in stage.iterdir():
                    with path.open("rb") as handle:
                        os.fsync(handle.fileno())
                try:
                    os.rename(stage, final)
                    published = True
                    if os.name != "nt":
                        directory_fd = os.open(self.batches, os.O_RDONLY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                except OSError:
                    if not final.exists():
                        raise
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        manifest = self._verify_batch(batch_id)
        # Atomic directory publication precedes the DB reference. A crash can
        # leave an unreferenced batch, never a visible partial batch.
        entity = self.store.put_entity(0, "data_batch", manifest, batch_id)
        self.store.put_entity(user_id, "batch_reference", {"batch_id": batch_id, "manifest_hash": entity["content_hash"]}, batch_id)
        # This catalogue is intentionally empty; DuckDB opens it read-only for
        # parameterized scans of immutable parquet files, without SQL from users.
        catalog = self.root / "read.duckdb"
        with _CATALOG_LOCK:
            if not catalog.exists():
                duckdb.connect(str(catalog)).close()
        self.store.set_state(user_id, f"data_latest:{symbol}:{bundle.get('range', '5y')}:{bundle.get('interval', '1d')}", batch_id)
        if collection_stats is not None:
            collection_stats["bytes_stored"] = sum((final / name).stat().st_size for name in ("raw.json", "bars.parquet")) if published else 0
        return {**manifest, "id": batch_id, "manifest_hash": entity["content_hash"], "quality": {"eligible": False, "issues": blockers or ["snapshot_metadata_not_validated"], "warnings": []}}

    def _verify_batch(self, batch_id):
        path = self._batch_path(batch_id)
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            if manifest["batch_id"] != batch_id or set(manifest["files"]) != {"raw.json", "bars.parquet"}:
                raise SnapshotIntegrityError("Invalid manifest")
            for name, expected in manifest["files"].items():
                if _hash_file(path / name) != expected:
                    raise SnapshotIntegrityError(f"Batch {batch_id} {name} checksum mismatch")
            raw = json.loads((path / "raw.json").read_text(encoding="utf-8"))
            if store.content_hash(raw) != batch_id:
                raise SnapshotIntegrityError("Raw batch identity mismatch")
            known = self.store.get_entity(0, "data_batch", batch_id)
            if known and known["content_hash"] != store.content_hash(manifest):
                raise SnapshotIntegrityError("Manifest checksum mismatch")
            return manifest
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise SnapshotIntegrityError(f"Missing or invalid immutable batch {batch_id}") from exc

    def _owned_batch(self, user_id, batch_id):
        ref = self.store.get_entity(user_id, "batch_reference", batch_id)
        if ref is None:
            raise ValueError("Unknown batch for this user")
        manifest = self._verify_batch(batch_id)
        if ref["payload"]["manifest_hash"] != store.content_hash(manifest):
            raise SnapshotIntegrityError("Batch reference hash mismatch")
        return manifest

    def _read_batches(self, batch_ids, *, as_of=None):
        import duckdb
        # The immutable manifest order is revision precedence. New snapshots may
        # supersede a bar; old snapshots retain their original batch list forever.
        # Filter visibility BEFORE choosing a revision: a later correction must
        # not hide the original bar that was available at the requested time.
        cutoff = _ts(as_of) if as_of is not None else None
        by_key = {}
        with duckdb.connect(str(self.root / "read.duckdb"), read_only=True) as conn:
            for batch_id in batch_ids:
                path = str(self._batch_path(batch_id) / "bars.parquet")
                query, params = "SELECT * FROM read_parquet(?)", [path]
                if cutoff is not None:
                    query += " WHERE available_at <= ?"
                    params.append(cutoff)
                result = conn.execute(query, params)
                names = [col[0] for col in result.description]
                for value in result.fetchall():
                    row = dict(zip(names, value))
                    extra = json.loads(row.pop("extra_json") or "{}")
                    # Derive provenance from the immutable file being read, not
                    # provider-supplied extras. Old Parquet files need no rewrite.
                    by_key[(row["symbol"], row["t"])] = {**extra, **row, "batch_id": batch_id}
        return sorted(by_key.values(), key=lambda r: (r["t"], r["symbol"]))

    def _expand_batches(self, user_id, batch_ids):
        result, visiting, completed = [], set(), set()
        for batch_id in batch_ids:
            stack = [(batch_id, False)]
            while stack:
                current, exiting = stack.pop()
                if exiting:
                    visiting.remove(current)
                    completed.add(current)
                    result.append(current)
                    continue
                if current in visiting:
                    raise SnapshotIntegrityError("Cycle in batch lineage")
                if current in completed:
                    continue
                visiting.add(current)
                manifest = self._owned_batch(user_id, current)
                stack.append((current, True))
                stack.extend((parent, False) for parent in reversed(manifest.get("parent_batch_ids", [])))
        return result

    @staticmethod
    def _metadata_blockers(rows, metadata):
        issues = set()
        for row in rows:
            # Recheck merged revisions, including batches created under earlier
            # quality policies, before certifying a new snapshot.
            issues.update(trading_rules.daily_bar_availability_issues(row, row["date"], require_metadata=True))
        symbols = {r["symbol"] for r in rows}
        start, end = min(r["date"] for r in rows), max(r["date"] for r in rows)
        coverage = metadata.get("coverage") or {}
        if not isinstance(coverage, dict):
            coverage = {}
        if not (isinstance(coverage.get("start"), str) and isinstance(coverage.get("end"), str) and isinstance(coverage.get("symbols"), list) and coverage["start"] <= start and coverage["end"] >= end and symbols <= set(coverage["symbols"])):
            issues.add("historical_evidence_coverage_missing")
        calendar = metadata.get("calendar")
        if not isinstance(calendar, list) or not calendar or not all(isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in calendar):
            issues.add("trading_calendar_missing")
            calendar = []
        calendar_set = set(calendar)
        if calendar and any(r["date"] not in calendar_set for r in rows):
            issues.add("bars_outside_calendar")
        history = metadata.get("instrument_history")
        if not isinstance(history, list) or not history or coverage.get("instrument_history_complete") is not True:
            issues.add("instrument_history_missing")
            history = []
        valid_history = []
        for item in history:
            if not isinstance(item, dict) or not isinstance(item.get("symbol"), str) or not isinstance(item.get("valid_from"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["valid_from"]) or "valid_to" not in item or (item["valid_to"] is not None and (not isinstance(item["valid_to"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["valid_to"]))) or not item.get("available_at"):
                issues.add("instrument_history_invalid")
                continue
            try:
                valid_history.append({**item, "available_at": _ts(item["available_at"])})
            except (ValueError, TypeError):
                issues.add("instrument_history_invalid")
        for symbol in symbols:
            symbol_rows = [r for r in rows if r["symbol"] == symbol]
            for row in symbol_rows:
                if not any(h["symbol"] == symbol and h["valid_from"] <= row["date"] <= (h["valid_to"] or "9999-12-31") and h["available_at"] <= row["available_at"] for h in valid_history):
                    issues.add("instrument_history_incomplete")
                    break
            if calendar:
                actual = {r["date"] for r in symbol_rows}
                expected = {d for d in calendar if start <= d <= end and any(h["symbol"] == symbol and h["valid_from"] <= d <= (h["valid_to"] or "9999-12-31") for h in valid_history)}
                if expected - actual:
                    issues.add("missing_session_bars")
        actions = metadata.get("corporate_actions")
        if not isinstance(actions, list) or coverage.get("corporate_actions_complete") is not True:
            issues.add("corporate_actions_history_missing")
        else:
            declared_actions = set()
            for action in actions:
                try:
                    if not isinstance(action, dict) or not {"symbol", "ex_date", "available_at", "cash_dividend", "split_ratio"} <= set(action):
                        raise ValueError()
                    if action["symbol"] not in symbols or not isinstance(action["ex_date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", action["ex_date"]) or _ts(action["available_at"]) is None or not math.isfinite(float(action["cash_dividend"])) or float(action["cash_dividend"]) < 0 or not math.isfinite(float(action["split_ratio"])) or float(action["split_ratio"]) <= 0:
                        raise ValueError()
                    parsed, invalid = trading_rules.parse_corporate_actions({"corporate_action": action}, action["ex_date"])
                    if start <= action["ex_date"] <= end:
                        issues.update(invalid)
                        declared_actions.update((action["symbol"], action["ex_date"], a["cash_dividend"], a["split_ratio"], a["available_at"]) for a in parsed)
                    # Actions must be represented in the bar payload consumed by
                    # the execution engine; separate metadata alone is inadequate.
                    matches = [r for r in rows if r["symbol"] == action["symbol"] and r["date"] == action["ex_date"]]
                    if start <= action["ex_date"] <= end and (not matches or not all(r.get("corporate_action") or r.get("corporate_actions") for r in matches)):
                        issues.add("corporate_action_execution_missing")
                except (ValueError, TypeError, KeyError):
                    issues.add("corporate_actions_history_invalid")
            observed_actions = set()
            for row in rows:
                parsed, invalid = trading_rules.parse_corporate_actions(row, row["date"])
                issues.update(invalid)
                observed_actions.update((row["symbol"], row["date"], a["cash_dividend"], a["split_ratio"], a["available_at"]) for a in parsed)
            if declared_actions - observed_actions:
                issues.add("corporate_action_execution_mismatch")
            if observed_actions - declared_actions:
                issues.add("corporate_action_history_incomplete")
        return sorted(issues)

    def create_snapshot(self, user_id, batch_ids, *, pool_version_id=None, metadata=None):
        if not batch_ids or len(set(batch_ids)) != len(batch_ids):
            raise ValueError("Snapshot requires unique batch ids")
        metadata = dict(metadata or {})
        batch_ids = self._expand_batches(user_id, batch_ids)
        manifests = [self._owned_batch(user_id, bid) for bid in batch_ids]
        if pool_version_id is not None and self.store.get_entity(user_id, "pool_version", pool_version_id) is None:
            raise ValueError("Unknown pool version for this user")
        rows = self._read_batches(batch_ids)
        blockers = set(self._metadata_blockers(rows, metadata))
        if metadata.get("example_only") or metadata.get("synthetic") or metadata.get("source") == "format_fixture_not_market_data":
            blockers.add("example_or_synthetic_data")
        if metadata.get("offline_fallback"):
            blockers.add("offline_or_stale_cache")
        for symbol in {m["symbol"] for m in manifests}:
            dimensions = {(m["source"], tuple(m["price_basis"]), tuple(m["volume_unit"])) for m in manifests if m["symbol"] == symbol}
            if len(dimensions) > 1:
                blockers.add("mixed_source_or_price_basis")
        for manifest in manifests:
            blockers.update(manifest["blockers"])
        has_cash_dividends = any(a["cash_dividend"] > 0 for row in rows for a in trading_rules.parse_corporate_actions(row, row["date"])[0])
        if has_cash_dividends:
            blockers.add("cash_dividend_net_payment_unverified")
        quality = {"eligible": not blockers, "issues": sorted(blockers),
                   "warnings": ["Cash dividends are modeled gross before investor tax and credited on the ex-date; payment-date differences are not modeled."] if has_cash_dividends else []}
        payload = {"version": 1, "batch_ids": list(batch_ids), "manifest": [{"batch_id": m["batch_id"], "manifest_hash": store.content_hash(m), "files": m["files"]} for m in manifests],
                   "symbols": sorted({r["symbol"] for r in rows}), "row_count": len(rows),
                   "start": min(r["date"] for r in rows), "end": max(r["date"] for r in rows),
                   "pool_version_id": pool_version_id, "metadata": metadata,
                   "quality": quality, "strict": not blockers, "blockers": sorted(blockers)}
        return self.store.put_entity(user_id, "snapshot", payload, store.content_hash(payload))

    def get_snapshot(self, user_id, snapshot_id, verify=True):
        snapshot = self.store.get_entity(user_id, "snapshot", snapshot_id)
        if snapshot is None:
            raise ValueError("Unknown snapshot for this user")
        if verify:
            for entry in snapshot["payload"]["manifest"]:
                manifest = self._owned_batch(user_id, entry["batch_id"])
                if store.content_hash(manifest) != entry["manifest_hash"]:
                    raise SnapshotIntegrityError("Snapshot manifest mismatch")
        return snapshot

    def read_snapshot(self, user_id, snapshot_id, *, as_of=None):
        """Read the last visible revision in manifest order, with batch provenance.

        ``as_of`` is an inclusive availability cutoff, not a quality certificate:
        inferred timestamps and the snapshot's original blockers remain unchanged.
        """
        snapshot = self.get_snapshot(user_id, snapshot_id)
        return self._read_batches(snapshot["payload"]["batch_ids"], as_of=as_of)

    def import_snapshot(self, user_id, bundles, *, metadata, pool_version_id=None):
        if not isinstance(bundles, dict) or not bundles:
            raise ValueError("bundles must map symbols to provider bundles")
        batches = [self.ingest_bundle(user_id, sym, bundle, metadata=metadata) for sym, bundle in bundles.items()]
        return self.create_snapshot(user_id, [b["id"] for b in batches], pool_version_id=pool_version_id, metadata=metadata)

    async def _acquire_bundle(self, user_id, symbol, range_param, interval, offline):
        from app.services import kline_pipeline
        from app.services.runtime_telemetry import record_collection
        from app.storage import kline_bundle_cache
        error = "Offline mode"
        if not offline:
            try:
                bundle = await kline_pipeline.fetch_a_share_kline_with_fallbacks(symbol, range_param, interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: market source unavailable"
                record_collection("market_history", "market_pipeline", status="failed", error_code="source_unavailable", user_id=user_id)
            else:
                # Validate outside the acquisition exception handler: corrupt
                # new data must neither overwrite a good cache nor look valid
                # merely because an older copy happens to exist.
                if not isinstance(bundle, dict) or not isinstance(bundle.get("candles"), list):
                    record_collection("market_history", "market_pipeline", status="failed", error_code="invalid_payload", user_id=user_id)
                    raise MarketDataValidationError("Market bundle requires a candles list")
                local = bool(bundle.get("served_from_cache")) or str(bundle.get("kline_source")) in {"local_cache", "free_stockdb_local"}
                if bundle["candles"] and interval == "1d":
                    try:
                        await asyncio.to_thread(_normalise, symbol, bundle, {}, time.time())
                    except (ValueError, TypeError, KeyError, OverflowError, AttributeError) as exc:
                        if not local:
                            record_collection("market_history", "market_pipeline", records_received=len(bundle["candles"]), status="failed", error_code="invalid_payload", user_id=user_id)
                        raise MarketDataValidationError(f"Invalid daily market data: {exc}") from exc
                if bundle["candles"]:
                    try:
                        kline_bundle_cache.save_bundle(symbol, range_param, interval, bundle)
                    except OSError:
                        pass
                return bundle, {"_network_collection": not local}
        cached = kline_bundle_cache.load_bundle(symbol, range_param, interval)
        if cached and (cached.get("payload") or {}).get("candles"):
            bundle = kline_bundle_cache.merge_stale_response(cached, fetch_error=error)
            return bundle, {"fetched_at": cached.get("saved_at"), "offline_fallback": True}
        head = self.store.get_state(user_id, f"history_head:{symbol}:{interval}")
        archived_id = head["id"] if head else self.store.get_state(user_id, f"data_latest:{symbol}:{range_param}:{interval}")
        if archived_id:
            bid = archived_id
            self._owned_batch(user_id, bid)
            raw = json.loads((self._batch_path(bid) / "raw.json").read_text())
            bundle = {**raw["bundle"], "cache_stale": True, "served_from_cache": True, "fetch_error_detail": error[:500]}
            return bundle, {"fetched_at": raw["fetched_at"], "offline_fallback": True}
        raise ValueError(f"Historical data unavailable and no local copy: {error[:500]}")

    async def fetch_bundle(self, user_id, symbol, range_param="5y", interval="1d", offline=False):
        from app.services.runtime_telemetry import record_collection
        symbol = symbol.strip().upper()
        bundle, metadata = await self._acquire_bundle(user_id, symbol, range_param, interval, offline)
        network = metadata.pop("_network_collection", False)
        received = len(bundle.get("candles") or []) if network else 0
        source = bundle.get("kline_source") or "market_pipeline"
        bundle = {**bundle, "range": range_param, "interval": interval}
        if interval != "1d":
            if network:
                record_collection("market_history", source, records_received=received, user_id=user_id)
            return bundle
        if not bundle.get("candles"):
            if network:
                record_collection("market_history", source, status="failed", error_code="empty_payload", user_id=user_id)
            # Empty provider responses remain renderable by legacy chart
            # clients, but are not historical evidence and never replace data.
            return {**bundle, "data_as_of": None, "served_from_cache": False,
                    "cache_stale": True, "research_quality": {"eligible": False, "issues": ["empty_market_data"]}}
        counts = {} if network else None
        try:
            result = await asyncio.to_thread(self._archive_acquired_bundle, user_id, symbol, range_param, interval, bundle, metadata, counts)
        except Exception:
            if network:
                record_collection("market_history", source, records_received=received, status="failed", error_code="storage_failed", user_id=user_id)
            raise
        if network:
            record_collection("market_history", source, records_received=received, user_id=user_id, **counts)
        return result

    def _archive_acquired_bundle(self, user_id, symbol, range_param, interval, bundle, metadata, collection_stats=None):
        import fcntl
        # API workers and the independent research worker share this publication
        # lock. A network request never holds the lock; head reads/updates do.
        with (self.root / "publication.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                return self._archive_acquired_locked(user_id, symbol, range_param, interval, bundle, metadata, collection_stats)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _archive_acquired_locked(self, user_id, symbol, range_param, interval, bundle, metadata, collection_stats=None):
        dimension = [bundle.get("kline_source", "unknown"), bundle.get("price_basis", "unknown"), bundle.get("volume_unit", "unknown")]
        branch_key = f"history_branch:{symbol}:{interval}:" + store.content_hash(dimension)
        branch = self.store.get_state(user_id, branch_key)
        head = self.store.get_state(user_id, f"history_head:{symbol}:{interval}")
        if branch:
            metadata["parent_batch_ids"] = [branch["id"]]
        if head and head["dimension"] != dimension:
            metadata["quality_blockers"] = ["source_or_price_basis_branch_changed"]
        previous_ids = self._expand_batches(user_id, [branch["id"]]) if branch and collection_stats is not None else []
        previous_rows = {(r["symbol"], r["t"]): _bar_content(r) for r in self._read_batches(previous_ids)} if previous_ids else {}
        batch = self.ingest_bundle(user_id, symbol, bundle, metadata=metadata, collection_stats=collection_stats)
        ids = self._expand_batches(user_id, [batch["id"]])
        manifests = [self._owned_batch(user_id, bid) for bid in ids]
        state = {"id": batch["id"], "dimension": dimension,
                 "start": min(m["start"] for m in manifests), "end": max(m["end"] for m in manifests),
                 "row_count": len(self._read_batches(ids)), "updated_at": time.time()}
        self.store.set_state(user_id, branch_key, state)
        self.store.set_state(user_id, f"history_head:{symbol}:{interval}", state)
        if collection_stats is not None:
            acquired_rows = self._read_batches([batch["id"]])
            collection_stats.update(
                records_added=sum(previous_rows.get((r["symbol"], r["t"])) != _bar_content(r) for r in acquired_rows),
            )
        # A delta or offline fallback must still serve the requested historical
        # range to quant/UI consumers, not silently return only the latest month.
        rows = self._read_batches(ids)
        # A historical/offline source still has a meaningful requested window.
        # Anchor that window to its latest available bar and expose staleness,
        # rather than deleting the whole archive against today's wall clock.
        anchor = datetime.fromisoformat(rows[-1]["date"])
        days = {"1d": 1, "5d": 5, "1mo": 31, "3mo": 93, "6mo": 186,
                "1y": 366, "2y": 732, "5y": 1830, "10y": 3660}
        start = anchor.replace(month=1, day=1) if range_param == "ytd" else anchor - timedelta(days=days.get(range_param, 36525) - 1)
        merged = [r for r in rows if r["date"] >= start.date().isoformat()]
        stale = bool(bundle.get("cache_stale")) or (datetime.fromtimestamp(time.time(), SHANGHAI).date() - anchor.date()).days > 7
        quality = dict(batch["quality"])
        if stale:
            quality = {**quality, "eligible": False, "issues": sorted(set(quality.get("issues", [])) | {"offline_or_stale_cache"})}
        return {**bundle, "candles": merged, "data_as_of": rows[-1]["date"],
                "cache_stale": stale, "served_from_cache": bool(bundle.get("served_from_cache")),
                "range_anchor": "latest_available_session", "research_batch_id": batch["id"],
                "research_batch_ids": ids, "research_quality": quality}

    async def fetch_history(self, user_id, symbol, *, range_param="5y", interval="1d", offline=False):
        symbol = symbol.strip().upper()
        head = self.store.get_state(user_id, f"history_head:{symbol}:{interval}")
        requested_range = range_param
        if range_param == "5y" and head:
            start = datetime.fromisoformat(head["start"]).replace(tzinfo=SHANGHAI).timestamp()
            end = datetime.fromisoformat(head["end"]).replace(tzinfo=SHANGHAI).timestamp()
            # A short delta is safe only when it overlaps the archive. After a
            # long offline gap, request the long window again to fill the gap.
            if interval == "1d" and head.get("row_count", 0) >= 1000 and start <= time.time() - 4.8 * 365.25 * 86400 and end >= time.time() - 25 * 86400:
                requested_range = "1mo"
        bundle = await self.fetch_bundle(user_id, symbol, requested_range, interval, offline)
        if not bundle.get("research_batch_id"):
            raise ValueError("No daily candles available to archive")
        batch_id = bundle["research_batch_id"]
        manifest = self._owned_batch(user_id, batch_id)
        return {**manifest, "id": batch_id, "batch_ids": bundle["research_batch_ids"],
                "quality": bundle["research_quality"], "acquisition_range": requested_range}


def ingest_bundle(user_id, symbol, bundle, *, metadata=None): return ResearchData().ingest_bundle(user_id, symbol, bundle, metadata=metadata)
def create_snapshot(user_id, batch_ids, *, pool_version_id=None, metadata=None): return ResearchData().create_snapshot(user_id, batch_ids, pool_version_id=pool_version_id, metadata=metadata)
def get_snapshot(user_id, snapshot_id, verify=True): return ResearchData().get_snapshot(user_id, snapshot_id, verify)
def read_snapshot(user_id, snapshot_id, *, as_of=None): return ResearchData().read_snapshot(user_id, snapshot_id, as_of=as_of)
def import_snapshot(user_id, bundles, *, metadata, pool_version_id=None): return ResearchData().import_snapshot(user_id, bundles, metadata=metadata, pool_version_id=pool_version_id)
async def fetch_history(user_id, symbol, *, range_param="5y", interval="1d", offline=False): return await ResearchData().fetch_history(user_id, symbol, range_param=range_param, interval=interval, offline=offline)

async def fetch_bundle(user_id, symbol, range_param="5y", interval="1d", offline=False): return await ResearchData().fetch_bundle(user_id, symbol, range_param, interval, offline)
