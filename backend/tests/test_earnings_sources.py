import asyncio
from datetime import date
import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.services import earnings_sources as source


PDF = b"%PDF-1.7\nfixture PDF content\n%%EOF"
CN_PDF = "https://static.cninfo.com.cn/finalpage/2026-04-17/1225114741.PDF"
EM_ID = "AN202604161821275610"
EM_PDF = f"https://pdf.dfcfw.com/pdf/H2_{EM_ID}_1.pdf?1776355200000.pdf"


@pytest.fixture(autouse=True)
def clean_sources(monkeypatch):
    monkeypatch.setattr(source, "_ORG_CACHE", None)
    monkeypatch.setattr(source, "_HOST_NEXT", {})
    monkeypatch.setattr(source, "REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(source, "_today", lambda: date(2026, 10, 31))


def cn_row(**updates):
    row = {"secCode": "600519", "secName": "贵州茅台", "orgId": "gssh0600519", "announcementId": "1225114741",
           "announcementTitle": "贵州茅台2025年年度报告", "announcementTime": 1776355200000,
           "adjunctUrl": "finalpage/2026-04-17/1225114741.PDF", "adjunctType": "PDF"}
    return {**row, **updates}


def em_row(**updates):
    return {"art_code": EM_ID, "codes": [{"stock_code": "600519", "market_code": "1", "short_name": "贵州茅台"}],
            "title": "贵州茅台:贵州茅台2025年年度报告", "notice_date": "2026-04-17 00:00:00", **updates}


def cn_item(**updates):
    return source._normalise_cninfo(cn_row(**updates), "600519.SS", "gssh0600519")


def em_item(**updates):
    return source._normalise_eastmoney(em_row(**updates), "600519.SS")


def em_detail(**updates):
    return {"data": {"art_code": EM_ID, "security": [{"stock": "600519", "market_uni": "1"}],
                     "notice_title": em_row()["title"], "notice_date": "2026-04-17 00:00:00",
                     "attach_url_web": EM_PDF, **updates}}


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    calls = []

    async def respond(request):
        calls.append(request)
        return handler(request)

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(source.httpx, "AsyncClient", client)
    return calls


@pytest.mark.parametrize("title,period,kind,language,revision", [
    ("贵州茅台2025年年度报告", "2025-12-31", "full_report", "zh", False),
    ("贵州茅台2025年年度报告（英文版）", "2025-12-31", "full_report", "en", False),
    ("贵州茅台2025年年度报告摘要", "2025-12-31", "summary", "zh", False),
    ("东方财富：2026年半年度报告全文（修订版）", "2026-06-30", "full_report", "zh", True),
    ("东方财富2026年第一季度报告", "2026-03-31", "full_report", "zh", False),
    ("贵州茅台2026年第三季度报告", "2026-09-30", "full_report", "zh", False),
    ("关于2025年年度报告的更正公告", "2025-12-31", "correction", "zh", True),
    ("平安银行2025年度业绩预告", "2025-12-31", "forecast", "zh", False),
    ("平安银行2025年度业绩快报", "2025-12-31", "express", "zh", False),
    ("<em>贵州茅台</em> 2025 年年度报告（修订稿）", "2025-12-31", "full_report", "zh", True),
])
def test_classifies_report_period_and_document_kind(title, period, kind, language, revision):
    value = source.classify_report(title)
    assert value["report_period"] == period
    assert value["document_kind"] == kind
    assert value["language"] == language
    assert value["is_revision"] is revision
    assert value["is_interpretable"] is (kind == "full_report" and language == "zh")


@pytest.mark.parametrize("title", ["关于2025年年度报告的董事会决议", "2025年年度报告问询函的回复",
    "2025年年度报告(审计意见)", "2025年年度报告审计报告", "子公司2025年年度报告",
    "2025年年度报告披露提示性公告", "2025年年度报告延期披露公告", "2025年年度董事会工作报告",
    "2025年社会责任报告", "2025年第二季度报告", "财报", None])
def test_related_documents_are_not_mislabelled_as_full_reports(title):
    assert source.classify_report(title) is None


@pytest.mark.parametrize("symbol", ["600519.SZ", "000001.SS", "AAPL", "600519", "600519.SS/../../", "600519.SS?token=secret", None])
def test_invalid_symbol_never_sends_http(monkeypatch, symbol):
    calls = mock_http(monkeypatch, lambda request: pytest.fail("unexpected network request"))
    with pytest.raises(source.SourceError, match="A股") as error:
        asyncio.run(source.discover_reports(symbol))
    assert error.value.code == "invalid_symbol" and not calls


@pytest.mark.parametrize("kwargs", [{"start_date": "2026-02-30"}, {"start_date": "2026-10-31", "end_date": "2026-01-01"},
    {"end_date": "2026-11-01"}, {"start_date": "2000-01-01"}, {"report_type": "q2"}, {"report_year": True}, {"limit": 0}])
def test_invalid_query_range_never_sends_http(monkeypatch, kwargs):
    calls = mock_http(monkeypatch, lambda request: pytest.fail("unexpected request"))
    with pytest.raises(source.SourceError) as error:
        asyncio.run(source.discover_reports("600519.SS", **kwargs))
    assert error.value.code == "invalid_range" and not calls


def test_cninfo_uses_official_org_id_filters_company_and_preserves_revisions(monkeypatch):
    revised = cn_row(announcementId="1225114742", announcementTitle="贵州茅台2025年年度报告（修订版）",
                     adjunctUrl="finalpage/2026-04-17/1225114742.PDF")
    def handler(request):
        if request.url.path.endswith("szse_stock.json"):
            return httpx.Response(200, json={"stockList": [{"code": "600519", "orgId": "9900012345"}]})
        assert request.url.path == "/new/hisAnnouncement/query"
        form = parse_qs(request.content.decode())
        assert form["stock"] == ["600519,9900012345"]
        assert form["seDate"] == ["2023-10-31~2026-10-31"]
        return httpx.Response(200, json={"announcements": [
            cn_row(orgId="9900012345"), {**revised, "orgId": "9900012345"},
            cn_row(orgId="wrong"), cn_row(secCode="000001", orgId="9900012345"),
            cn_row(orgId="9900012345", announcementTitle="贵州茅台2025年年度报告摘要")], "hasMore": False})
    calls = mock_http(monkeypatch, handler)
    result = asyncio.run(source.discover_reports("600519.SH"))
    assert result["symbol"] == "600519.SS"
    assert len(result["items"]) == 2 and len(calls) == 2
    assert result["sources"] == [{"source": "cninfo", "status": "ok", "partial": False, "error": None, "error_code": None}]
    assert {row["source_id"] for row in result["items"]} == {"1225114741", "1225114742"}
    assert result["items"][0]["published_at"] == 1776355200
    assert result["items"][0]["announcement_date"] == "2026-04-17"


@pytest.mark.parametrize("changes", [{"announcementTime": float("nan")}, {"announcementTime": "1776355200000"},
    {"announcementTime": 1760000000000}, {"announcementId": "wrong"}, {"adjunctUrl": "https://evil.test/file.pdf"},
    {"adjunctUrl": "finalpage/2026-04-17/1225114742.PDF"}, {"secCode": "000001"}, {"orgId": "other"}])
def test_cninfo_rejects_invalid_identity_date_or_attachment(changes):
    assert cn_item(**changes) is None


@pytest.mark.parametrize("changes", [{"codes": []}, {"codes": 12}, {"codes": [{"stock_code": "600519", "market_code": "0"}]},
    {"codes": [{"stock_code": "000001", "market_code": "1"}]}, {"notice_date": "2026-02-30"}, {"art_code": "../123"}])
def test_eastmoney_requires_exact_company_market_date_and_id(changes):
    assert em_item(**changes) is None


def test_future_reporting_period_cannot_be_passed_off_as_published_summary():
    assert cn_item(announcementTitle="贵州茅台2026年年度报告摘要") is None
    assert em_item(title="贵州茅台:贵州茅台2026年年度报告摘要") is None


def test_fallback_retains_safe_failure_and_eastmoney_provenance(monkeypatch):
    def handler(request):
        if request.url.host == "www.cninfo.com.cn":
            return httpx.Response(403, text="private error details must not leak")
        assert request.url.host == "np-anotice-stock.eastmoney.com"
        assert request.url.params["f_node"] == "1"
        return httpx.Response(200, json={"success": 1, "data": {"list": [em_row()], "total_hits": 1}})
    calls = mock_http(monkeypatch, handler)
    result = asyncio.run(source.discover_reports("600519.SS", report_year=2025, report_type="annual"))
    assert len(calls) == 2 and len(result["items"]) == 1
    assert result["sources"][0]["error_code"] == "access_denied"
    assert "private" not in json.dumps(result)
    item = result["items"][0]
    assert item["pdf_url"] is None and item["source"] == "eastmoney"
    assert item["identity_key"] == f"eastmoney:600519.SS:{EM_ID}"


def test_failed_later_page_keeps_good_items_and_tries_fallback(monkeypatch):
    monkeypatch.setattr(source, "PAGE_SIZE", 1)
    def handler(request):
        if request.url.path.endswith("szse_stock.json"):
            return httpx.Response(200, json={"stockList": [{"code": "600519", "orgId": "gssh0600519"}]})
        if request.url.host == "www.cninfo.com.cn":
            if parse_qs(request.content.decode())["pageNum"] == ["1"]:
                return httpx.Response(200, json={"announcements": [cn_row()], "hasMore": True})
            return httpx.Response(429)
        return httpx.Response(200, json={"success": 1, "data": {"list": [], "total_hits": 0}})
    calls = mock_http(monkeypatch, handler)
    result = asyncio.run(source.discover_reports("600519.SS"))
    assert len(calls) == 4 and len(result["items"]) == 1
    assert result["partial"] is True and result["sources"][0]["status"] == "ok"
    assert result["sources"][0]["error_code"] == "rate_limited"
    assert result["sources"][1]["status"] == "empty"


def test_summary_only_primary_tries_full_report_fallback_and_limit_is_explicit(monkeypatch):
    async def primary(*args):
        return [cn_item(announcementTitle="贵州茅台2025年年度报告摘要")], False, None
    async def fallback(*args):
        return [em_item()], False, None
    monkeypatch.setattr(source, "_cninfo", primary)
    monkeypatch.setattr(source, "_eastmoney", fallback)
    result = asyncio.run(source.discover_reports("600519.SS", limit=1))
    assert result["items"][0]["is_interpretable"] is True
    assert result["total_found"] == 2 and result["partial"] is True


def test_repeated_pages_are_bounded_and_marked_partial(monkeypatch):
    monkeypatch.setattr(source, "PAGE_SIZE", 1)
    def handler(request):
        if request.url.path.endswith("szse_stock.json"):
            return httpx.Response(200, json={"stockList": [{"code": "600519", "orgId": "gssh0600519"}]})
        if request.url.host == "www.cninfo.com.cn":
            return httpx.Response(200, json={"announcements": [cn_row()], "hasMore": True})
        return httpx.Response(503)
    calls = mock_http(monkeypatch, handler)
    result = asyncio.run(source.discover_reports("600519.SS"))
    assert len(calls) == 4 and len(result["items"]) == 1 and result["partial"]


@pytest.mark.parametrize("url", ["http://static.cninfo.com.cn/finalpage/2026-04-17/1225114741.PDF",
    "https://static.cninfo.com.cn.evil.test/finalpage/2026-04-17/1225114741.PDF",
    "https://user:password@static.cninfo.com.cn/finalpage/2026-04-17/1225114741.PDF",
    "https://127.0.0.1/file.pdf", "https://static.cninfo.com.cn:444/finalpage/2026-04-17/1225114741.PDF",
    CN_PDF + "?redirect=https://evil.test", CN_PDF + "#fragment",
    "https://static.cninfo.com.cn/finalpage/2026-04-17/../1225114741.PDF",
    "https://static.cninfo.com.cn/finalpage/2026-02-30/1225114741.PDF",
    "https://pdf.dfcfw.com/pdf/arbitrary.pdf", EM_PDF + "&url=http://127.0.0.1"])
def test_pdf_url_allowlist_is_exact_and_no_network_occurs(monkeypatch, url):
    calls = mock_http(monkeypatch, lambda request: pytest.fail("unsafe URL reached network"))
    with pytest.raises(source.SourceError) as error:
        asyncio.run(source._request_bytes("GET", url, pdf=True))
    assert error.value.code == "unsafe_url" and not calls


@pytest.mark.parametrize("url", ["https://www.cninfo.com.cn/", source.CNINFO_STOCKS + "?url=evil", source.EASTMONEY_DETAIL + "?callback=evil"])
def test_metadata_urls_have_fixed_paths_and_allowed_query_names(url):
    with pytest.raises(source.SourceError) as error:
        source._safe_url(url)
    assert error.value.code == "unsafe_url"


@pytest.mark.parametrize("target", ["https://evil.test/file.pdf", "http://127.0.0.1/private",
    "https://static.cninfo.com.cn/finalpage/2026-04-17/1225114742.PDF"])
def test_redirect_is_checked_before_next_request(monkeypatch, target):
    calls = mock_http(monkeypatch, lambda request: httpx.Response(302, headers={"location": target}))
    with pytest.raises(source.SourceError) as error:
        asyncio.run(source.download_report_pdf(cn_item()))
    assert error.value.code == "unsafe_url" and len(calls) == 1


def test_redirect_loop_is_bounded(monkeypatch):
    calls = mock_http(monkeypatch, lambda request: httpx.Response(302, headers={"location": CN_PDF}))
    with pytest.raises(source.SourceError) as error:
        asyncio.run(source.download_report_pdf(cn_item()))
    assert error.value.code == "redirect_limit" and len(calls) == 4


@pytest.mark.parametrize("headers,body,expected", [
    ({"content-type": "text/html"}, b"<html>captcha</html>", "invalid_pdf"),
    ({"content-type": "application/pdf"}, b"<html>fake PDF</html>", "invalid_pdf"),
    ({"content-type": "application/pdf", "content-length": str(source.MAX_PDF_BYTES + 1)}, PDF, "too_large"),
])
def test_pdf_type_magic_and_declared_size_are_checked(monkeypatch, headers, body, expected):
    mock_http(monkeypatch, lambda request: httpx.Response(200, headers=headers, content=body))
    with pytest.raises(source.SourceError) as error:
        asyncio.run(source.download_report_pdf(cn_item()))
    assert error.value.code == expected


def test_download_cap_applies_when_server_omits_length(monkeypatch):
    monkeypatch.setattr(source, "MAX_PDF_BYTES", 10)
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"%PDF-1.7\n"
            yield b"overflow"
    mock_http(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "application/pdf"}, stream=Chunks()))
    with pytest.raises(source.SourceError) as error:
        asyncio.run(source.download_report_pdf(cn_item()))
    assert error.value.code == "too_large"


@pytest.mark.parametrize("status,code", [(401, "access_denied"), (403, "access_denied"), (429, "rate_limited"), (404, "not_found"), (503, "source_unavailable")])
def test_denied_failed_requests_are_safe_and_not_retried(monkeypatch, status, code):
    calls = mock_http(monkeypatch, lambda request: httpx.Response(status, text="secret-server-message"))
    with pytest.raises(source.SourceError) as error:
        asyncio.run(source.download_report_pdf(cn_item()))
    assert error.value.code == code and len(calls) == 1
    assert "secret" not in str(error.value)


def test_timeout_is_safe_and_not_retried(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("secret-url", request=request)
    calls = mock_http(monkeypatch, handler)
    with pytest.raises(source.SourceError) as error:
        asyncio.run(source.download_report_pdf(cn_item()))
    assert error.value.code == "timeout" and "secret" not in str(error.value) and len(calls) == 1


def test_download_accepts_only_catalog_records_and_retains_hash_and_origin(monkeypatch):
    calls = mock_http(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "application/pdf"}, content=PDF))
    with pytest.raises(source.SourceError) as error:
        asyncio.run(source.download_report_pdf(CN_PDF))
    assert error.value.code == "invalid_catalog_item" and not calls
    value = asyncio.run(source.download_report_pdf(cn_item()))
    assert value == {"content": PDF, "sha256": hashlib.sha256(PDF).hexdigest(), "size_bytes": len(PDF),
                     "content_type": "application/pdf", "url": CN_PDF}


@pytest.mark.parametrize("updates", [{"source_id": "1225114742"}, {"report_period": "2024-12-31"}, {"report_type": "q1"},
    {"pdf_url": EM_PDF}, {"identity_key": "cninfo:000001.SZ:1225114741"}, {"announcement_date": "2026-02-30"}])
def test_catalog_identity_tampering_is_rejected_before_network(monkeypatch, updates):
    calls = mock_http(monkeypatch, lambda request: pytest.fail("tampered catalog reached network"))
    with pytest.raises(source.SourceError) as error:
        asyncio.run(source.download_report_pdf({**cn_item(), **updates}))
    assert error.value.code == "invalid_catalog_item" and not calls


@pytest.mark.parametrize("updates", [{"art_code": "AN202604161821275611"}, {"security": []}, {"security": None},
    {"security": [{"stock": "000001", "market_uni": "1"}]}, {"security": [{"stock": "600519", "market_uni": "0"}]},
    {"notice_title": "贵州茅台:贵州茅台2025年年度报告（英文版）"}, {"notice_date": "2026-04-18 00:00:00"},
    {"notice_title": "贵州茅台:贵州茅台2024年年度报告"}, {"attach_url_web": "https://evil.test/file.pdf"},
    {"attach_url_web": "https://pdf.dfcfw.com/pdf/H2_AN202604161821275611_1.pdf"}])
def test_eastmoney_attachment_detail_must_match_catalog_before_download(monkeypatch, updates):
    calls = mock_http(monkeypatch, lambda request: httpx.Response(200, json=em_detail(**updates)))
    with pytest.raises(source.SourceError) as error:
        asyncio.run(source.download_report_pdf(em_item()))
    assert error.value.code in {"invalid_catalog_item", "unsafe_url"} and len(calls) == 1


def test_eastmoney_resolves_official_detail_and_does_not_guess_pdf_url(monkeypatch):
    def handler(request):
        if request.url.host == "np-cnotice-stock.eastmoney.com":
            assert request.url.params["art_code"] == EM_ID
            return httpx.Response(200, json=em_detail())
        assert str(request.url) == EM_PDF
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=PDF)
    calls = mock_http(monkeypatch, handler)
    value = asyncio.run(source.download_report_pdf({**em_item(), "pdf_url": "https://evil.test/ignored.pdf"}))
    assert len(calls) == 2 and value["url"] == EM_PDF and value["content"] == PDF


def test_per_host_throttle_reserves_spacing_between_concurrent_requests(monkeypatch):
    monkeypatch.setattr(source, "REQUEST_INTERVAL_SECONDS", 1)
    monkeypatch.setattr(source.time, "monotonic", lambda: 100.0)
    waits = []
    async def sleep(seconds):
        waits.append(seconds)
    monkeypatch.setattr(source.asyncio, "sleep", sleep)
    async def check():
        await source._throttle("www.cninfo.com.cn")
        await source._throttle("www.cninfo.com.cn")
        await source._throttle("www.cninfo.com.cn")
        await source._throttle("pdf.dfcfw.com")
    asyncio.run(check())
    assert waits == [1.0, 2.0]
