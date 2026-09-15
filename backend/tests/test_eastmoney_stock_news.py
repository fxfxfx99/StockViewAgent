from app.services.eastmoney_stock_news import _normalize_gsgg_row


def test_normalize_gsgg_row():
    row = {
        "art_code": "AN202607151826993776",
        "title": "大普微:2026年半年度业绩预告",
        "display_time": "2026-07-15 18:04:17:298",
    }
    it = _normalize_gsgg_row(row, "301666.SZ", "301666")
    assert it is not None
    assert "大普微" in it["title"]
    assert it["symbol_hint"] == "301666.SZ"
    assert "301666" in it["link"]
    assert it["id"].startswith("em-ann-")
