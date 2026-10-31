"""Macro/industry data validation and paid-request cache reuse without live keys."""
import json
import math
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from app.config import settings
from app.services import macro_tushare_service as macro
from app.services.market_time import SHANGHAI_TZ
from app.storage import tushare_daily_cache as cache


TOKEN = "fake-macro-token-for-tests"


def bars(**changes):
    return pd.DataFrame([{"trade_date": "20261030", "open": 10, "high": 12, "low": 9,
                          "close": 11, "vol": 100, "amount": 1100, "name": "农业", **changes}])


def cached_bars(close=11):
    return {"candles": [{"t": macro._trade_date_to_ts("20261030"), "o": 10, "h": 12,
                         "l": 9, "c": close, "v": 100, "amount": 1100}]}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(type(settings), "effective_tushare_token", property(lambda self: TOKEN))
    monkeypatch.setattr(macro, "is_configured", lambda: True)
    fake = SimpleNamespace(index_daily=lambda **kwargs: bars(), sw_daily=lambda **kwargs: bars())
    monkeypatch.setattr(macro, "_get_pro", lambda: fake)
    return fake


def expire(kind, key):
    path = cache.cache_file(kind, key)
    entry = json.loads(path.read_text())
    entry["saved_at"] = (datetime.now(SHANGHAI_TZ) - timedelta(days=2)).isoformat()
    path.write_text(json.dumps(entry))
    return path.read_bytes()


@pytest.mark.parametrize("change", [
    {"open": math.nan}, {"high": math.inf}, {"close": -1}, {"low": 11}, {"high": 10},
    {"trade_date": "20260230"}, {"trade_date": "20261301"}, {"trade_date": "2026103"},
    {"trade_date": pd.NA},
])
def test_invalid_candles_are_filtered_without_losing_valid_rows(change):
    frame = pd.concat([bars(trade_date="20261029"), bars(**change)], ignore_index=True)
    result = macro._df_to_candles(frame, "ohlc")
    assert len(result) == 1
    assert result[0]["t"] == macro._trade_date_to_ts("20261029")
    json.dumps(result, allow_nan=False)


def test_candles_are_ordered_unique_and_missing_volume_is_finite():
    frame = pd.concat([bars(vol="inf", amount=-1), bars(trade_date="20261029"), bars(close=10.5)], ignore_index=True)
    result = macro._df_to_candles(frame, "ohlc")
    assert len(result) == 2
    assert result[0]["t"] < result[1]["t"]
    assert result[1]["c"] == 10.5
    result = macro._df_to_candles(bars(vol="inf", amount=-1), "ohlc")
    assert result[0]["v"] == result[0]["amount"] == 0
    assert macro._df_to_candles(pd.DataFrame([{"close": 10}]), "ohlc") == []
    fx = bars().rename(columns={key: "bid_" + key for key in ("open", "high", "low", "close")})
    assert macro._df_to_candles(fx, "fx")[0]["c"] == 11


def test_macro_reuses_fresh_same_token_and_refetches_after_token_change(provider, monkeypatch):
    calls = []
    provider.index_daily = lambda **kwargs: calls.append(kwargs) or bars()
    first = macro.fetch_macro_kline("sh", "1y")
    second = macro.fetch_macro_kline("sh", "1y")
    assert len(calls) == 1
    assert first["candles"] == second["candles"]
    assert second["cache_hit"] and not second["cache_stale"]
    assert second["cache_ttl_seconds"] == macro.MACRO_CACHE_TTL_SECONDS
    monkeypatch.setattr(type(settings), "effective_tushare_token", property(lambda self: "another-fake-token"))
    macro.fetch_macro_kline("sh", "1y")
    assert len(calls) == 2


@pytest.mark.parametrize("kind,fetch,remote", [
    ("macro_kline", lambda: macro.fetch_macro_kline("sh"), "index_daily"),
    ("sw_daily_kline", lambda: macro.fetch_sw_daily_kline("801010.SI"), "sw_daily"),
])
@pytest.mark.parametrize("response", [pd.DataFrame(), bars(high=math.nan), bars(trade_date="20260230")])
def test_empty_or_invalid_remote_never_replaces_last_good_candles(provider, kind, fetch, remote, response):
    first = fetch()
    key = "sh_1y" if kind == "macro_kline" else "801010.SI_1y"
    previous = expire(kind, key)
    setattr(provider, remote, lambda **kwargs: response)
    fallback = fetch()
    assert fallback["cache_stale"]
    assert fallback["candles"] == first["candles"]
    assert cache.cache_file(kind, key).read_bytes() == previous


def test_sw_indices_validate_reuse_and_preserve_cache_on_empty(provider):
    calls = []
    provider.sw_daily = lambda **kwargs: calls.append(kwargs) or pd.DataFrame([
        {"ts_code": "801010.SI", "name": "农业"}, {"ts_code": "801010.SI", "name": "农林牧渔"},
        {"ts_code": "invalid", "name": "bad"}, {"ts_code": pd.NA, "name": pd.NA},
    ])
    first = macro.fetch_sw_daily_indices("20261030")
    assert first["items"] == [{"ts_code": "801010.SI", "name": "农林牧渔"}]
    assert macro.fetch_sw_daily_indices("20261030")["cache_hit"]
    assert len(calls) == 1
    previous = expire("sw_indices", "20261030")
    provider.sw_daily = lambda **kwargs: pd.DataFrame()
    fallback = macro.fetch_sw_daily_indices("20261030")
    assert fallback["cache_stale"] and fallback["items"] == first["items"]
    assert cache.cache_file("sw_indices", "20261030").read_bytes() == previous


def test_empty_initial_response_is_not_cached(provider):
    provider.index_daily = lambda **kwargs: pd.DataFrame()
    result = macro.fetch_macro_kline("sh")
    assert result["candles"] == [] and result["warning"]
    assert cache.load_entry("macro_kline", "sh_1y") is None


def test_unconfigured_fallback_and_provider_errors_do_not_expose_tokens(provider, monkeypatch):
    original = macro.fetch_macro_kline("sh")
    expire("macro_kline", "sh_1y")

    def fail(**kwargs):
        raise RuntimeError(f"https://provider.invalid?token={TOKEN} permission denied")

    provider.index_daily = fail
    fallback = macro.fetch_macro_kline("sh")
    assert TOKEN not in json.dumps(fallback)
    assert fallback["cache_stale"] and fallback["candles"] == original["candles"]
    with pytest.raises(ValueError) as caught:
        macro.fetch_macro_kline("sz")
    assert TOKEN not in str(caught.value)
    monkeypatch.setattr(macro, "is_configured", lambda: False)
    monkeypatch.setattr(type(settings), "effective_tushare_token", property(lambda self: ""))
    assert macro.fetch_macro_kline("sh")["cache_stale"]


@pytest.mark.parametrize("call", [
    lambda: macro.fetch_macro_kline("sh", "../wrong"),
    lambda: macro.fetch_sw_daily_kline("801010.SI", "10y"),
    lambda: macro.fetch_sw_daily_kline("../../unsafe"),
    lambda: macro.fetch_sw_daily_indices("20260230"),
    lambda: macro.fetch_sw_daily_indices("2026103"),
])
def test_invalid_request_is_rejected_before_provider(provider, call):
    provider.sw_daily = provider.index_daily = lambda **kwargs: pytest.fail("Invalid requests must not reach provider")
    with pytest.raises(ValueError):
        call()


def test_industry_discovery_stops_on_first_provider_error(provider):
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError(f"token={TOKEN}")

    provider.sw_daily = fail
    with pytest.raises(ValueError):
        macro.fetch_sw_daily_indices()
    assert len(calls) == 1


def test_cache_follows_current_data_directory_and_writes_timezone(monkeypatch, tmp_path):
    cache.save_payload("macro_kline", "sh_1y", cached_bars(10), TOKEN)
    old_path = cache.cache_file("macro_kline", "sh_1y")
    assert "+08:00" in cache.load_entry("macro_kline", "sh_1y")["saved_at"]
    monkeypatch.setattr(type(settings), "data_dir", property(lambda self: tmp_path / "other"))
    assert cache.load_entry("macro_kline", "sh_1y") is None
    cache.save_payload("macro_kline", "sh_1y", cached_bars(12), TOKEN)
    assert old_path.is_file()
    assert cache.cache_file("macro_kline", "sh_1y") != old_path


def test_atomic_cache_failure_and_nonfinite_payload_preserve_prior_file(monkeypatch):
    cache.save_payload("macro_kline", "sh_1y", cached_bars(10), TOKEN)
    path = cache.cache_file("macro_kline", "sh_1y")
    previous = path.read_bytes()
    with pytest.raises(ValueError):
        cache.save_payload("macro_kline", "sh_1y", cached_bars(math.nan), TOKEN)
    assert path.read_bytes() == previous

    def fail_replace(*args):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(cache.os, "replace", fail_replace)
    with pytest.raises(OSError):
        cache.save_payload("macro_kline", "sh_1y", cached_bars(12), TOKEN)
    assert path.read_bytes() == previous
    assert not list(path.parent.glob("*.tmp"))


def test_legacy_naive_timestamp_can_be_fresh_without_disabling_stale_fallback():
    entry = {"token_fp": cache.token_fingerprint(TOKEN),
             "saved_at": datetime.now(SHANGHAI_TZ).replace(tzinfo=None).isoformat(),
             "payload": cached_bars()}
    assert cache.fresh_payload(entry, current_token=TOKEN, ttl_seconds=900)["cache_hit"]
    entry["saved_at"] = "unparseable legacy date"
    assert cache.fresh_payload(entry, current_token=TOKEN, ttl_seconds=900) is None
    assert cache.merge_stale_kline(entry, current_token="", fetch_error=None, no_token=True)["candles"]


def test_legacy_nonfinite_json_is_not_served_even_as_no_token_fallback(monkeypatch):
    path = cache.cache_file("macro_kline", "sh_1y")
    path.write_text(json.dumps({"token_fp": cache.token_fingerprint(TOKEN),
                               "saved_at": datetime.now(SHANGHAI_TZ).isoformat(),
                               "payload": cached_bars(math.nan)}))
    previous = path.read_bytes()
    assert cache.load_entry("macro_kline", "sh_1y") is None
    monkeypatch.setattr(macro, "is_configured", lambda: False)
    monkeypatch.setattr(type(settings), "effective_tushare_token", property(lambda self: ""))
    with pytest.raises(ValueError, match="未配置"):
        macro.fetch_macro_kline("sh")
    assert path.read_bytes() == previous


@pytest.mark.parametrize("broken", [
    {"candles": [{"t": 123, "o": 10, "h": 8, "l": 9, "c": 11}]},
    {"candles": ["bad legacy row"]},
    {"candles": [{"t": 123, "c": 11}]},
])
def test_bad_legacy_ohlc_cache_cannot_bypass_remote_repair(provider, broken):
    path = cache.cache_file("macro_kline", "sh_1y")
    path.write_text(json.dumps({"token_fp": cache.token_fingerprint(TOKEN),
                               "saved_at": datetime.now(SHANGHAI_TZ).isoformat(), "payload": broken}))
    calls = []
    provider.index_daily = lambda **kwargs: calls.append(kwargs) or bars()
    result = macro.fetch_macro_kline("sh")
    assert len(calls) == 1 and not result["cache_hit"]
    assert result["candles"][0]["c"] == 11
    assert cache.load_entry("macro_kline", "sh_1y")["payload"]["candles"] == result["candles"]
