from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from app.services import xueqiu_comments_pipeline as pipeline
from app.services.market_time import SHANGHAI_TZ
from app.services.xueqiu_http import XueqiuSessionError


DAY_START = int(datetime(2026, 9, 21, tzinfo=SHANGHAI_TZ).timestamp())


def raw_item(sid, timestamp=DAY_START + 60, text=None, **extra):
    return {"id": sid, "created_at": timestamp, "description": text or f"评论 {sid}", "user": {"id": 123, "screen_name": "公开作者"}, **extra}


@pytest.fixture
def install_pages(monkeypatch):
    def install(pages):
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            index = kwargs["params"]["page"] - 1
            page = pages[index] if index < len(pages) else []
            if isinstance(page, str):
                return None, page
            if isinstance(page, dict):
                return page, None
            return {"list": page}, None

        monkeypatch.setattr(pipeline.xueqiu_http, "request_json", request)
        return calls

    return install


def test_beijing_date_boundaries_skip_newer_posts_and_normalize_milliseconds(install_pages):
    calls = install_pages([
        [raw_item(9, DAY_START + 86400), raw_item(8, (DAY_START + 86399) * 1000), raw_item(7, DAY_START), raw_item(6, DAY_START - 1)],
        [raw_item(5, DAY_START - 2)],
    ])
    result = pipeline.fetch_comments("600519.SS", "2026-09-21")
    assert [item["id"] for item in result["items"]] == ["8", "7"]
    assert [item["created_at"] for item in result["items"]] == [DAY_START + 86399, DAY_START]
    assert result["raw_count"] == 2 and result["scanned_count"] == 5
    assert result["partial"] is False and result["truncated"] is False
    assert len(calls) == 2
    assert all(call[1] == pipeline.COMMENTS_URL for call in calls)
    assert calls[0][2]["params"]["symbol"] == "SH600519"
    assert calls[0][2]["params"]["source"] == "user"
    assert calls[0][2]["params"]["sort"] == "time"
    assert calls[0][2]["params"]["count"] == 20


def test_target_date_dedup_does_not_remove_yesterday_because_today_repeats_it(install_pages):
    install_pages([
        [raw_item(4, DAY_START + 86401, "重复观点")],
        [raw_item(3, DAY_START + 30, "重复观点")],
        [],
    ])
    result = pipeline.fetch_comments("SH600519", "2026-09-21")
    assert [item["id"] for item in result["items"]] == ["3"]
    assert not result["truncated"]


def test_dedup_by_id_and_normalized_body_and_empty_page_stops(install_pages):
    calls = install_pages([
        [raw_item(1, text="<p>Ａ 分析 &amp; 判断</p>"), raw_item(2, text="另外一条")],
        [raw_item(1, text="同 ID 新内容"), raw_item(3, text="A\n分析 &amp; 判断"), raw_item(4, text="独立观点")],
        [],
    ])
    result = pipeline.fetch_comments("000001.SZ")
    assert {item["id"] for item in result["items"]} == {"1", "2", "4"}
    assert len(calls) == 3
    assert result["raw_count"] == 3 and result["scanned_count"] == 5
    assert result["partial"] is False


def test_repeated_whole_page_marks_incomplete_and_stops(install_pages):
    page = [raw_item(1), raw_item(2)]
    calls = install_pages([page, page, [raw_item(3)]])
    result = pipeline.fetch_comments("600519.SS")
    assert len(calls) == 2
    assert len(result["items"]) == 2
    assert result["partial"] and result["truncated"]
    assert result["warnings"]


def test_repeated_malformed_page_also_stops_without_scanning_to_cap(install_pages):
    page = [{"id": 1, "description": "无时间的评论"}]
    calls = install_pages([page, page, [raw_item(3)]])
    result = pipeline.fetch_comments("600519.SS")
    assert len(calls) == 2 and result["items"] == []
    assert result["partial"] and result["truncated"]


def test_scan_cap_is_explicit_when_newer_pages_hide_target_date(install_pages, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_PAGES", 3)
    calls = install_pages([[raw_item(i, DAY_START + 86401)] for i in range(1, 5)])
    result = pipeline.fetch_comments("600519.SS", "2026-09-21")
    assert len(calls) == 3
    assert result["items"] == []
    assert result["raw_count"] == 0 and result["scanned_count"] == 3
    assert result["partial"] and result["truncated"]
    assert result["error"] is None


def test_latest_mode_keeps_newest_up_to_requested_cap(install_pages):
    install_pages([[raw_item(i, DAY_START + i) for i in range(1, 21)]])
    result = pipeline.fetch_comments("600519.SS", limit=3)
    assert [item["id"] for item in result["items"]] == ["20", "19", "18"]
    assert result["partial"] is False and result["truncated"] is True
    assert result["raw_count"] == 3 and result["scanned_count"] == 20


def test_full_200_item_limit_is_a_successful_bounded_fetch(install_pages):
    calls = install_pages([[raw_item(page * 20 + i + 1, DAY_START + page * 20 + i + 1) for i in range(20)] for page in range(11)])
    result = pipeline.fetch_comments("600519.SS", limit=200)
    assert len(calls) == 10
    assert result["raw_count"] == 200 and len(result["items"]) == 200
    assert result["scanned_count"] == 200
    assert result["truncated"] is True and result["partial"] is False
    assert result["error"] is None


def test_normalization_strips_html_and_only_constructs_valid_original_urls(install_pages):
    install_pages([[
        raw_item(1, text="<script>private instructions</script><p>正文 &amp; 信息</p>", target="https://evil.example/123/1", like_count="4", reply_count=-2),
        raw_item(2, text="x" * 5000, user={}, target="/456/2"),
        raw_item(3, user={}, target="//evil.example/456/3"),
        raw_item(4, created_at="NaN"),
    ]])
    result = pipeline.fetch_comments("600519.SS")
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id["1"]["text"] == "正文 & 信息"
    assert by_id["1"]["author"] == {"name": "公开作者", "uid": "123"}
    assert by_id["1"]["url"] == "https://xueqiu.com/123/1"
    assert by_id["1"]["like_count"] == 4 and by_id["1"]["reply_count"] == 0
    assert by_id["2"]["url"] == "https://xueqiu.com/456/2"
    assert len(by_id["2"]["text"]) == 4000
    assert by_id["3"]["url"] == ""
    assert "4" not in by_id
    assert result["partial"]


@pytest.mark.parametrize("configured, expected", [(False, "missing"), (True, "expired")])
def test_session_rejection_stops_without_endpoint_switch(install_pages, configured, expected):
    calls = install_pages([XueqiuSessionError(configured), [raw_item(1)]])
    result = pipeline.fetch_comments("600519.SS")
    assert result["auth_status"] == expected
    assert result["error"] and result["items"] == []
    assert len(calls) == 1


def test_session_interruption_keeps_already_fetched_items(install_pages):
    calls = install_pages([[raw_item(1)], XueqiuSessionError(True), [raw_item(2)]])
    result = pipeline.fetch_comments("600519.SS")
    assert [item["id"] for item in result["items"]] == ["1"]
    assert result["auth_status"] == "expired" and result["partial"]
    assert len(calls) == 2


@pytest.mark.parametrize("body, expected", [
    ({"success": False, "code": "401", "message": "unauthorized synthetic-private-value"}, "missing"),
    ({"success": False, "code": "AUTH_FAILED", "message": "请登录 synthetic-private-value"}, "missing"),
    ({"success": False, "code": "AUTH_FAILED", "message": "登录会话已过期 synthetic-private-value"}, "expired"),
    ({"success": False, "code": 403, "message": "captcha synthetic-private-value"}, "verification_required"),
    ({"success": False, "code": 500, "message": "synthetic-private-value"}, "unavailable"),
])
def test_success_code_business_errors_are_classified_without_echoing_message(install_pages, body, expected):
    calls = install_pages([body, [raw_item(1)]])
    result = pipeline.fetch_comments("600519.SS")
    assert result["error"] and result["auth_status"] == expected
    assert result["items"] == [] and len(calls) == 1
    assert "synthetic-private-value" not in json.dumps(result)


def test_success_code_200_with_list_remains_compatible(install_pages):
    install_pages([{"success": True, "code": 200, "list": [raw_item(1)]}])
    result = pipeline.fetch_comments("600519.SS")
    assert result["error"] is None and len(result["items"]) == 1


@pytest.mark.parametrize("page", [{"unexpected": []}, "error with synthetic sensitive data"])
def test_errors_are_not_reported_as_empty_success(install_pages, page):
    install_pages([page])
    result = pipeline.fetch_comments("600519.SS")
    assert result["error"]
    assert "synthetic sensitive data" not in result["error"]


@pytest.mark.parametrize("target_date", ["2026-09-31", "20260921", "yesterday"])
def test_invalid_date_never_requests_network(install_pages, target_date):
    calls = install_pages([])
    assert pipeline.fetch_comments("600519.SS", target_date)["error"]
    assert calls == []


def source_item(sid):
    return {"id": str(sid), "author": {"name": "公开作者", "uid": "123"}, "created_at": DAY_START + sid, "text": f"原评论 {sid}", "title": "", "url": f"https://xueqiu.com/123/{sid}", "like_count": 999, "reply_count": 2}


def score(sid, relevance=80, value=80, **extra):
    return {"id": str(sid), "relevance_score": relevance, "value_score": value, "summary": "作者认为需要继续验证", "reason": "包含具体推理", "risks": ["尚未独立核实"], "stance": "neutral", "category": "基本面", **extra}


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setattr(pipeline, "settings", SimpleNamespace(any_llm_key_configured=True, kline_llm_model="test-model"))
    calls = []

    def install(responder):
        def complete(messages, **kwargs):
            batch = json.loads(messages[-1]["content"].split("\n", 2)[2])
            calls.append((messages, batch))
            return responder(batch, len(calls))
        monkeypatch.setattr(pipeline.llm_client, "chat_completion_sync", complete)
        return calls

    return install


def test_all_200_comments_are_scored_in_ten_batches_with_progress(llm):
    calls = llm(lambda batch, number: (json.dumps([score(item["id"]) for item in batch]), None))
    progress = []
    result = pipeline.select_comments("600519.SS", [source_item(i) for i in range(1, 201)], lambda done, total: progress.append((done, total)))
    assert len(calls) == 10 and all(len(batch) == 20 for _, batch in calls)
    assert result["analyzed_count"] == 200
    assert len(result["items"]) == 20 and result["rejected_count"] == 180
    assert progress == [value for i in range(0, 200, 20) for value in ((i, 200), (i + 20, 200))]
    assert result["partial"] is False and result["error"] is None
    assert "不可信数据" in calls[0][0][0]["content"]
    assert "like_count" not in calls[0][1][0]


@pytest.mark.parametrize("bad_score", [True, 70.0, "80", -1, 101, float("nan"), float("inf")])
def test_scores_require_finite_strict_integers(llm, bad_score):
    llm(lambda batch, number: (json.dumps([score(1, value=bad_score)]), None))
    result = pipeline.select_comments("600519.SS", [source_item(1)])
    assert result["items"] == [] and result["analyzed_count"] == 0
    assert result["partial"] and result["error"]


def test_hallucinated_duplicate_ids_and_invalid_schema_never_replace_source(llm):
    llm(lambda batch, number: (json.dumps([
        score(1), score(999), score(2), score(2), score(3, stance=["bullish"]),
        score(4, author="伪造作者", url="https://evil.example"),
    ]), None))
    sources = [source_item(i) for i in range(1, 5)]
    result = pipeline.select_comments("600519.SS", sources)
    assert [item["id"] for item in result["items"]] == ["1"]
    assert result["items"][0]["author"] == sources[0]["author"]
    assert result["items"][0]["url"] == sources[0]["url"]
    assert result["items"][0]["text"] == sources[0]["text"]
    assert result["analyzed_count"] == 1 and result["partial"]


def test_both_thresholds_are_required_and_sort_uses_score_not_likes(llm):
    llm(lambda batch, number: (json.dumps([score(1, relevance=69), score(2, value=69), score(3, relevance=70, value=70), score(4, relevance=80, value=95)]), None))
    result = pipeline.select_comments("600519.SS", [source_item(i) for i in range(1, 5)])
    assert [item["id"] for item in result["items"]] == ["4", "3"]
    assert result["analyzed_count"] == 4 and result["rejected_count"] == 2
    assert not result["partial"]


def test_partial_batch_failure_keeps_successes_and_reports_attempted_progress(llm):
    def responder(batch, number):
        return (None, "synthetic-provider-secret") if number == 2 else (json.dumps([score(item["id"]) for item in batch]), None)

    calls = llm(responder)
    progress = []
    result = pipeline.select_comments("600519.SS", [source_item(i) for i in range(1, 46)], lambda done, total: progress.append((done, total)))
    assert len(calls) == 3
    assert result["analyzed_count"] == 25 and result["rejected_count"] == 5
    assert result["partial"] and result["error"]
    assert progress == [(0, 45), (20, 45), (20, 45), (40, 45), (40, 45), (45, 45)]
    assert "synthetic-provider-secret" not in json.dumps(result)


def test_no_key_blocks_without_requesting_llm(llm, monkeypatch):
    calls = llm(lambda *args: pytest.fail("must not call LLM"))
    monkeypatch.setattr(pipeline.settings, "any_llm_key_configured", False)
    result = pipeline.select_comments("600519.SS", [source_item(1)])
    assert result["error"] and result["analyzed_count"] == 0
    assert result["items"] == [] and calls == []


def test_long_comments_use_dynamic_batches_capped_at_12000_text_characters(llm):
    calls = llm(lambda batch, number: (json.dumps([score(item["id"]) for item in batch]), None))
    items = [{**source_item(i), "text": "文" * 4000} for i in range(1, 8)]
    progress = []
    result = pipeline.select_comments("600519.SS", items, lambda done, total: progress.append((done, total)))
    assert [len(batch) for _, batch in calls] == [3, 3, 1]
    assert all(sum(len(item["text"]) for item in batch) <= 12000 for _, batch in calls)
    assert result["analyzed_count"] == 7 and not result["partial"]
    assert all(len(item["text"]) == 4000 for item in result["items"])
    assert progress == [(0, 7), (3, 7), (3, 7), (6, 7), (6, 7), (7, 7)]


@pytest.mark.parametrize("fail_on_callback, expected_llm_calls", [(1, 0), (2, 1), (3, 1)])
def test_progress_failure_propagates_before_any_further_paid_calls(llm, fail_on_callback, expected_llm_calls):
    calls = llm(lambda batch, number: (json.dumps([score(item["id"]) for item in batch]), None))
    notifications = []

    def progress(done, total):
        notifications.append((done, total))
        if len(notifications) == fail_on_callback:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        pipeline.select_comments("600519.SS", [source_item(i) for i in range(1, 46)], progress)
    assert len(calls) == expected_llm_calls
