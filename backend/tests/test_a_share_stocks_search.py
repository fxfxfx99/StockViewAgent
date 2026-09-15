from app.services import a_share_stocks


def test_name_search_keys_strips_uw_suffix():
    keys = a_share_stocks._name_search_keys("大普微-UW")
    assert "大普微" in keys
    assert "大普微-UW" in keys


def test_search_merged_includes_eastmoney_when_csv_missing_new_listing(monkeypatch):
    monkeypatch.setattr(
        a_share_stocks,
        "load_stocks",
        lambda: [{"code": "301666", "name": "大普微-UW", "symbol": "301666.SZ"}],
    )
    monkeypatch.setattr(
        "app.services.issuer_basic_info_universe.get_stocks_for_search_if_available",
        lambda: [{"code": "000001", "name": "平安银行", "symbol": "000001.SZ"}],
    )
    monkeypatch.setattr(a_share_stocks, "_fetch_suggest_rows", lambda q, limit: [])

    hits = a_share_stocks.search_stocks("大普微", 5, live_fallback=False)
    assert any(h.get("code") == "301666" for h in hits)


def test_search_live_fallback_when_local_miss(monkeypatch):
    monkeypatch.setattr(a_share_stocks, "_merged_search_rows", lambda: [])
    monkeypatch.setattr(a_share_stocks, "load_stocks", lambda: [])
    monkeypatch.setattr(
        a_share_stocks,
        "_fetch_suggest_rows",
        lambda q, limit: [{"code": "301666", "name": "大普微-UW", "symbol": "301666.SZ"}],
    )

    hits = a_share_stocks.search_stocks("大普微", 5)
    assert hits and hits[0]["code"] == "301666"
