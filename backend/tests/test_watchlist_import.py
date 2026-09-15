from app.services import watchlist_import


def test_parse_explicit_suffix():
    text = "持仓 600519.SS 与 000001.SZ"
    syms, details = watchlist_import.parse_text_to_symbols(text)
    assert "600519.SS" in syms
    assert "000001.SZ" in syms
    assert len(details) >= 2


def test_parse_csv_row():
    text = "code,name\n600519.SS,茅台"
    syms, _ = watchlist_import.parse_text_to_symbols(text)
    assert "600519.SS" in syms
