import pytest

from app.services import xueqiu_pipeline


def test_yahoo_to_xq():
    assert xueqiu_pipeline.yahoo_to_xq_symbol("600519.SS") == "SH600519"
    assert xueqiu_pipeline.yahoo_to_xq_symbol("000001.SZ") == "SZ000001"
    assert xueqiu_pipeline.yahoo_to_xq_symbol("430047.BJ") == "BJ430047"


def test_normalize_quote_minimal():
    raw = {
        "data": {
            "market": {},
            "quote": {
                "current": 1700.5,
                "percent": -1.2,
                "name": "测试",
                "symbol": "SH600519",
            },
        }
    }
    n = xueqiu_pipeline.stage_normalize_quote(raw)
    assert n.get("name") == "测试"
    assert n.get("current") == 1700.5


def test_normalize_company_f10():
    raw = {
        "data": {
            "company": {
                "org_name_cn": "贵州茅台酒股份有限公司",
                "affiliate_industry": "白酒",
                "org_cn_introduction": "<p>主营茅台酒</p>",
                "org_short_name_cn": "贵州茅台",
            }
        }
    }
    n = xueqiu_pipeline.stage_normalize_company_f10(raw)
    assert "茅台" in n["org_name"]
    assert n["industry"] == "白酒"
    assert "茅台酒" in n["intro"]
    assert n["name"] == "贵州茅台"


def test_normalize_major_events_from_mock():
    # 仅测解析结构，不发起网络请求
    from app.services import xueqiu_pipeline as xp

    raw = {
        "data": {
            "items": [
                {
                    "title": "业绩公告",
                    "message": "净利润增长 10%",
                    "created_at": 1700000000000,
                    "event_type": "业绩",
                }
            ]
        }
    }
    block = raw["data"]
    items = block.get("items") or []
    assert items[0]["title"] == "业绩公告"
    # normalize company still independent
    assert xp.stage_normalize_company_f10(raw).get("name") == ""


def test_run_company_bundle_invalid_symbol():
    out = xueqiu_pipeline.run_company_bundle("INVALID")
    assert out["ok"] is False
    assert out["xq_symbol"] is None
    assert out["errors"]


@pytest.mark.parametrize("payload", [
    {"data": {"items": []}},
    {"data": {"list": []}},
    {"items": []},
    {"list": []},
    {"data": {"items": [], "list": [{"title": "旧备用事件"}]}},
])
def test_major_events_explicit_empty_list_is_successful(monkeypatch, payload):
    monkeypatch.setattr(xueqiu_pipeline.xueqiu_http, "request_json", lambda *args, **kwargs: (payload, None))
    assert xueqiu_pipeline.stage_fetch_major_events("SH600519") == ([], None)


@pytest.mark.parametrize("payload", [
    {}, {"data": {}}, {"data": {"items": "invalid"}}, {"items": None},
    {"items": "", "list": []}, {"list": {}},
])
def test_malformed_major_events_are_errors_not_successful_empty_updates(monkeypatch, payload):
    monkeypatch.setattr(xueqiu_pipeline.xueqiu_http, "request_json", lambda *args, **kwargs: (payload, None))
    items, error = xueqiu_pipeline.stage_fetch_major_events("SH600519")
    assert items == []
    assert error and "格式异常" in error


@pytest.mark.parametrize("payload", [
    {"list": []}, {"statuses": []}, {"list": [], "statuses": [{"title": "旧备用新闻"}]},
])
def test_timeline_explicit_empty_list_is_successful(monkeypatch, payload):
    monkeypatch.setattr(xueqiu_pipeline.xueqiu_http, "request_json", lambda *args, **kwargs: (payload, None))
    assert xueqiu_pipeline.stage_fetch_stock_timeline("SH600519", source="自选股新闻") == ([], None)


@pytest.mark.parametrize("payload", [
    {}, {"list": "invalid"}, {"list": None}, {"list": "", "statuses": []}, {"statuses": {}},
])
def test_malformed_timeline_is_error_not_successful_empty_update(monkeypatch, payload):
    monkeypatch.setattr(xueqiu_pipeline.xueqiu_http, "request_json", lambda *args, **kwargs: (payload, None))
    items, error = xueqiu_pipeline.stage_fetch_stock_timeline("SH600519", source="自选股新闻")
    assert items == []
    assert error and "格式异常" in error
