"""Fixtures exercise the structures verified on each official publisher page."""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app.services import event_calendar_sources as sources


FED = """
<h4>2026 FOMC Meetings</h4>
<div class="row fomc-meeting">
 <div class="fomc-meeting__month col-xs-5"><strong>September</strong></div>
 <div class="fomc-meeting__date">15-16*</div>
 <a href="/newsevents/pressreleases/monetary20260916a.htm">HTML</a>
</div>
<div class="fomc-meeting--shaded row fomc-meeting">
 <div class="fomc-meeting--shaded fomc-meeting__month">October</div>
 <div class="fomc-meeting__date">27-28</div>
</div>
<h4>2027 FOMC Meetings</h4>
<div class="row fomc-meeting">
 <div class="fomc-meeting__month">Apr/May</div>
 <div class="fomc-meeting__date">30-1</div>
</div>
"""
ECB = """
<dl>
 <dt>28/10/2026</dt><dd>Governing Council: monetary policy meeting (Day 1)<br></dd>
 <dt>29/10/2026</dt><dd>Governing Council: monetary policy meeting (Day 2), followed by press conference</dd>
 <dt>29/10/2026</dt><dd>Press conference following the Governing Council meeting</dd>
 <dt>25/11/2026</dt><dd>Governing Council: non-monetary policy meeting</dd>
 <dt>not a date</dt><dd>Monetary policy meeting</dd>
</dl>
"""
BOJ = """
<table><caption>Table : 2026</caption><thead><tr><th>Date of MPM</th></tr></thead>
<tbody>
 <tr><td><a href="/en/mopo/mpmdeci/mpr_2026/k260918a.pdf">Sept. 17 (Thurs.), 18 (Fri.) [PDF 204KB]</a></td>
 <td>-</td><td><a href="/opinions.pdf">Oct. 1 (Thurs.) [PDF 227KB]</a></td><td>Nov. 5 (Thurs.)</td></tr>
 <tr><td>Dec. 17 (Thurs.), 18 (Fri.)</td><td>-</td><td>Dec. 28 (Mon.)</td><td>Jan. 27 (Wed.), 2027</td></tr>
</tbody></table>
<p>Summary of Opinions and MPM Minutes: It will be released at 8:50 a.m.</p>
"""
BLS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:cpi-2026-11
DTSTART;TZID=US/Eastern:20261110T083000
SUMMARY:Consumer Price Index
DESCRIPTION:October data\\nSource release\\, with revised
  details
URL:https://www.bls.gov/news.release/cpi.htm
END:VEVENT
BEGIN:VEVENT
UID:jobs-2026-07
DTSTART:20260702T123000Z
SUMMARY:Employment Situation
END:VEVENT
BEGIN:VEVENT
UID:cancelled
DTSTART;VALUE=DATE:20261111
SUMMARY:Producer Price Index
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


def test_fed_uses_meeting_end_day_and_statement_evidence():
    events = sources.parse_fed_calendar(FED)
    assert [e["scheduled_date"] for e in events] == ["2026-09-16", "2026-10-28", "2027-05-01"]
    assert [e["status"] for e in events] == ["released", "scheduled", "scheduled"]
    assert events[0]["source_url"].endswith("monetary20260916a.htm")
    assert "SEP" in events[0]["description"]
    assert events[1]["scheduled_at"] == "2026-10-28T04:00:00+00:00"
    assert all(e["all_day"] and e["actual"] is None for e in events)


def test_ecb_has_distinct_day_and_press_events_and_europe_timezone():
    events = sources.parse_ecb_calendar(ECB)
    assert len(events) == 3
    assert len({e["upstream_id"] for e in events}) == 3
    assert events[0]["importance"] == 2
    assert events[1]["importance"] == 3
    assert events[1]["scheduled_at"] == "2026-10-28T23:00:00+00:00"
    assert all(e["all_day"] and e["status"] == "scheduled" for e in events)


def test_boj_handles_pdf_sizes_minutes_time_and_next_year():
    events = sources.parse_boj_calendar(BOJ)
    assert len(events) == 6
    assert events[0]["scheduled_date"] == "2026-09-18"
    assert events[0]["status"] == "released"
    assert events[1]["scheduled_at"] == "2026-09-30T23:50:00+00:00"
    assert events[-1]["scheduled_date"] == "2027-01-27"
    assert events[-1]["scheduled_at"] == "2027-01-26T23:50:00+00:00"
    assert events[-1]["status"] == "scheduled"
    assert events[0]["all_day"] and not events[-1]["all_day"]


def test_boj_does_not_invent_announcement_hour():
    events = sources.parse_boj_calendar(BOJ.replace("8:50 a.m.", "To be announced"))
    assert all(event["all_day"] for event in events)


def test_nbs_rowspans_missing_months_and_two_releases_in_same_month():
    fixture = """
    <div>2028年国家统计局主要统计信息发布日程表</div>
    <table><tr><td>序号</td><td>内容</td><td>1月</td><td>2月</td><td>3月</td></tr>
    <tr><td rowspan="2">1</td><td rowspan="2">采购经理指数月度报告</td><td>31/一</td><td rowspan="2">……</td><td>4/三 注5 31/二</td></tr>
    <tr><td>9:30</td><td>9:30</td></tr>
    <tr><td rowspan="2">2</td><td rowspan="2">居民消费价格指数月度报告</td><td>9/一</td><td>10/二</td><td>9/四</td></tr>
    <tr><td>9:30</td><td>15:00</td><td>9:30</td></tr>
    <tr><td colspan="5">注：日期可能调整。</td></tr></table>
    """
    events = sources.parse_nbs_calendar(fixture)
    assert [e["scheduled_date"] for e in events[:3]] == ["2028-01-31", "2028-03-04", "2028-03-31"]
    assert len(events) == 6
    assert events[4]["scheduled_at"] == "2028-02-10T07:00:00+00:00"
    assert events[4]["category"] == "inflation"
    assert events[0]["category"] == "growth"
    assert all(e["status"] == "scheduled" for e in events)


def test_bls_ics_local_dst_utc_folding_and_explicit_cancellation():
    events = sources.parse_bls_calendar(BLS)
    assert len(events) == 3
    assert events[0]["scheduled_at"] == "2026-11-10T13:30:00+00:00"
    assert events[1]["scheduled_at"] == "2026-07-02T12:30:00+00:00"
    assert events[0]["description"] == "October data\nSource release, with revised details"
    assert events[0]["category"] == "inflation"
    assert events[1]["category"] == "employment"
    assert events[2]["status"] == "cancelled" and events[2]["all_day"]


def test_bls_discards_invalid_dates_and_timezone_and_untrusted_link():
    invalid = BLS.replace("20261110T083000", "not-a-date").replace("20260702T123000Z", "20261302T123000Z")
    assert len(sources.parse_bls_calendar(invalid)) == 1
    assert len(sources.parse_bls_calendar(BLS.replace("US/Eastern", "Invalid/Zone"))) == 2
    events = sources.parse_bls_calendar(BLS.replace("https://www.bls.gov/news.release/cpi.htm", "javascript:alert(1)"))
    assert events[0]["source_url"] == sources._SOURCES["us_bls"]["url"]


@pytest.mark.parametrize("alias", ["US/Eastern", "US-Eastern", "Eastern Standard Time", "America/New_York"])
def test_bls_common_eastern_timezone_aliases_preserve_dst(alias):
    rows = sources.parse_bls_calendar(BLS.replace("US/Eastern", alias))
    assert len(rows) == 3
    assert rows[0]["scheduled_at"] == "2026-11-10T13:30:00+00:00"


@pytest.mark.parametrize("source_id", tuple(sources._PARSERS))
def test_html_challenge_or_empty_page_is_not_a_calendar(source_id):
    assert sources._PARSERS[source_id]("<html><title>Access denied</title></html>") == []


@pytest.fixture
def http_mock(monkeypatch):
    original = httpx.AsyncClient
    calls = []

    def install(handler):
        def respond(request):
            calls.append(request)
            return handler(request)
        monkeypatch.setattr(sources.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
        return calls
    return install


def test_fetch_fixed_url_window_dedup_and_timeout(http_mock):
    calls = http_mock(lambda _: httpx.Response(200, text=FED))
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    rows = asyncio.run(sources.fetch_source("fed_fomc", now=now))
    assert [row["scheduled_date"] for row in rows] == ["2026-10-28", "2027-05-01"]
    assert str(calls[0].url) == sources._SOURCES["fed_fomc"]["url"]
    assert set(calls[0].extensions["timeout"].values()) == {15.0}


def test_recognized_source_can_have_no_dates_in_current_window(http_mock):
    http_mock(lambda _: httpx.Response(200, text=FED))
    assert asyncio.run(sources.fetch_source("fed_fomc", now=datetime(2030, 1, 1, tzinfo=timezone.utc))) == []


@pytest.mark.parametrize("response, error", [
    (httpx.Response(403, text="blocked"), "403"),
    (httpx.Response(200, text="<html>Access denied</html>"), "no recognizable"),
    (httpx.Response(302, headers={"location": "http://localhost/private"}), "302"),
    (httpx.Response(200, headers={"content-length": "2000001"}, text=""), "size limit"),
])
def test_fetch_exposes_access_format_redirect_and_size_errors(http_mock, response, error):
    calls = http_mock(lambda _: response)
    with pytest.raises(sources.CalendarSourceError, match=error):
        asyncio.run(sources.fetch_source("us_bls"))
    assert len(calls) == 1


def test_stream_size_limit_without_content_length(http_mock, monkeypatch):
    monkeypatch.setattr(sources, "MAX_RESPONSE_BYTES", 10)
    http_mock(lambda _: httpx.Response(200, content=b"a" * 11, headers={"content-length": "0"}))
    with pytest.raises(sources.CalendarSourceError, match="size limit"):
        asyncio.run(sources.fetch_source("us_bls"))


def test_unknown_source_does_not_make_request(http_mock):
    calls = http_mock(lambda _: httpx.Response(200, text=BLS))
    with pytest.raises(sources.CalendarSourceError, match="Unknown"):
        asyncio.run(sources.fetch_source("http://localhost"))
    assert calls == []


@pytest.mark.parametrize("status", [403, 429, 503])
def test_status_error_has_safe_diagnostic_code(http_mock, status):
    http_mock(lambda _: httpx.Response(status, text="private response body"))
    with pytest.raises(sources.CalendarSourceError) as caught:
        asyncio.run(sources.fetch_source("us_bls"))
    assert caught.value.error_code == f"http_{status}"
    assert "private response" not in str(caught.value)


@pytest.mark.parametrize("error, code", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network_error")])
def test_network_error_has_safe_diagnostic_code(http_mock, error, code):
    def fail(request):
        raise error("sensitive proxy configuration", request=request)
    http_mock(fail)
    with pytest.raises(sources.CalendarSourceError) as caught:
        asyncio.run(sources.fetch_source("us_bls"))
    assert caught.value.error_code == code
    assert "sensitive" not in str(caught.value)


def test_calendar_never_fabricates_actual_forecast_previous():
    for parser, fixture in ((sources.parse_fed_calendar, FED), (sources.parse_ecb_calendar, ECB),
                            (sources.parse_boj_calendar, BOJ), (sources.parse_bls_calendar, BLS)):
        for item in parser(fixture):
            assert item["actual"] is None and item["forecast"] is None and item["previous"] is None
            assert item["region"] and item["currency"] and item["timezone"]
