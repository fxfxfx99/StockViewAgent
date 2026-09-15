from app.services.research_agent.analyzers.price_action import analyze_price_action
from app.services.research_agent.normalizer import is_a_share, normalize_a_share_symbol
from app.services.research_agent.symbol_resolve import heuristic_resolve


def test_normalize_a_share():
    assert normalize_a_share_symbol("600519.SH") == "600519.SS"
    assert normalize_a_share_symbol("000001.SZ") == "000001.SZ"
    assert is_a_share("600519.SS")


def test_heuristic_us_ticker():
    r = heuristic_resolve("请分析 NVDA 未来两年逻辑")
    assert r is not None
    assert r.symbol == "NVDA"
    assert r.market == "global"


def test_price_action_empty():
    out = analyze_price_action({"primary_symbol": "TEST", "kline": {"candles": []}, "kline_metrics": {}})
    assert "未获取" in out["narrative"]
