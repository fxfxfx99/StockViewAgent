"""Publisher fixtures match the online-verified official XML/JSON contracts."""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.services import public_data_sources as sources


NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
FX = '''<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref"><Cube>
 <Cube time="2026-10-23"><Cube currency="USD" rate="1.1193"/><Cube currency="CNY" rate="7.466"/></Cube>
 <Cube time="2026-10-22"><Cube currency="USD" rate="1.1205"/></Cube>
</Cube></gesmes:Envelope>'''
RSS = '''<rss version="2.0"><channel><item>
<title>Federal Reserve issues FOMC statement</title>
<link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm</link>
<pubDate>Wed, 16 Sep 2026 18:00:00 GMT</pubDate>
<description><![CDATA[<p>Policy summary</p><script>doNotRun()</script>]]></description>
<category>Monetary Policy</category></item></channel></rss>'''


def wb_document(values=(0, None, -1.5), **metadata):
    observations = [{"indicator": {"id": "NY.GDP.MKTP.KD.ZG", "value": "GDP growth (annual %)"},
                     "country": {"id": "CN", "value": "China"}, "countryiso3code": "CHN",
                     "date": str(2025 - n), "value": value, "unit": ""}
                    for n, value in enumerate(values)]
    return json.dumps([{"page": 1, "pages": 1, "total": len(observations), "lastupdated": "2026-10-08", **metadata}, observations])


def test_fx_preserves_direction_period_and_no_invented_publication_time():
    rows = sources.parse_ecb_fx(FX, now=NOW)
    assert len(rows) == 3
    row = rows[0]
    assert row["value"] == 1.1193
    assert row["unit"] == "USD/EUR"
    assert row["symbols"] == ["EUR", "USD"]
    assert row["period"] == "2026-10-23"
    assert row["published_at"] is None
    assert row["observed_at"] == NOW.isoformat()
    assert row["upstream_id"] == "EUR:USD:2026-10-23"
    assert row["metadata"]["base_currency"] == "EUR"


def test_fx_skips_invalid_rates_dates_and_future_observations():
    payload = FX.replace('rate="1.1193"', 'rate="NaN"').replace('rate="7.466"', 'rate="0"')
    assert len(sources.parse_ecb_fx(payload, now=NOW)) == 1
    assert not sources.parse_ecb_fx(FX.replace("2026-10", "2027-10"), now=NOW)
    assert len(sources.parse_ecb_fx(FX.replace("2026-10-23", "not-a-date"), now=NOW)) == 1
    assert len(sources.parse_ecb_fx(FX.replace('currency="CNY"', 'currency="<bad>"').replace("<bad>", "bad"), now=NOW)) == 2


def test_worldbank_zero_missing_and_negative_are_distinct():
    rows = sources.parse_worldbank(wb_document(), "wb_gdp_growth", now=NOW)
    assert [r["value"] for r in rows] == [0, None, -1.5]
    assert [r["metadata"]["value_status"] for r in rows] == ["observed", "missing", "observed"]
    assert all(r["published_at"] is None and r["unit"] == "%" for r in rows)
    assert rows[0]["metadata"]["dataset_updated_date"] == "2026-10-08"
    assert rows[0]["upstream_id"] == "NY.GDP.MKTP.KD.ZG:CN:2025"
    assert rows[0]["source_url"].endswith("format=json&per_page=1000&date=2017%3A2026")


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "invalid"])
def test_worldbank_invalid_numeric_is_marked_not_zero(value):
    row = sources.parse_worldbank(wb_document((value,)), "wb_gdp_growth", now=NOW)[0]
    assert row["value"] is None
    assert row["metadata"]["value_status"] == "invalid_numeric"


@pytest.mark.parametrize("metadata", [{"pages": 2}, {"page": 2}, {"total": 999}])
def test_worldbank_rejects_incomplete_pagination(metadata):
    with pytest.raises(sources.PublicDataSourceError, match="分页"):
        sources.parse_worldbank(wb_document(**metadata), "wb_gdp_growth", now=NOW)


@pytest.mark.parametrize("payload", ['{}', '[{},null]', '["bad",[]]', '{"error":"rate limit"}'])
def test_worldbank_rejects_error_payloads(payload):
    with pytest.raises(sources.PublicDataSourceError):
        sources.parse_worldbank(payload, "wb_gdp_growth", now=NOW)


def test_worldbank_filters_unconfigured_indicators_countries_and_periods():
    payload = json.loads(wb_document())
    payload[1][0]["country"]["id"] = "XX"
    payload[1][1]["indicator"]["id"] = "UNEXPECTED"
    payload[1][2]["date"] = "2027"
    assert not sources.parse_worldbank(json.dumps(payload), "wb_gdp_growth", now=NOW)
    assert not sources.parse_worldbank(wb_document().replace("2025", "2001").replace("2024", "2002").replace("2023", "2003"), "wb_gdp_growth", now=NOW)


def test_policy_parses_real_published_time_and_inert_summary():
    row = sources.parse_policy_rss(RSS, "fed_policy", now=NOW)[0]
    assert row["published_at"] == "2026-09-16T18:00:00+00:00"
    assert row["period"] is None and row["value"] is None and row["unit"] is None
    assert row["region"] == "US"
    assert row["text"].endswith("Policy summary")
    assert "doNotRun" not in row["text"] and "<p>" not in row["text"]
    assert row["metadata"]["content_scope"] == "rss_title_and_summary"


def test_policy_keeps_missing_time_unknown_and_omits_future_items():
    row = sources.parse_policy_rss(RSS.replace("Wed, 16 Sep 2026 18:00:00 GMT", "Unknown"), "fed_policy", now=NOW)[0]
    assert row["published_at"] is None
    assert row["metadata"]["publication_time_known"] is False
    assert not sources.parse_policy_rss(RSS.replace("16 Sep 2026", "16 Sep 2027"), "fed_policy", now=NOW)
    assert sources.parse_policy_rss(RSS.replace(" GMT", ""), "fed_policy", now=NOW)[0]["published_at"] is None


@pytest.mark.parametrize("link", ["javascript:alert(1)", "https://evil.example/fake", "https://www.federalreserve.gov:9999/test", "https://user:pass@www.federalreserve.gov/test", "https://www.federalreserve.gov:invalid/test", "https://[invalid/test"])
def test_policy_rejects_nonpublisher_links(link):
    payload = RSS.replace("https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm", link)
    row = sources.parse_policy_rss(payload, "fed_policy", now=NOW)[0]
    assert row["source_url"] == sources._SOURCES["fed_policy"]["url"]


def test_ecb_rss_supports_timezone_and_no_description():
    payload = RSS.replace("www.federalreserve.gov", "www.ecb.europa.eu").replace("Wed, 16 Sep 2026 18:00:00 GMT", "Fri, 23 Oct 2026 10:00:00 +0200")
    row = sources.parse_policy_rss(payload, "ecb_publications", now=NOW)[0]
    assert row["published_at"] == "2026-10-23T08:00:00+00:00"
    assert row["region"] == "EU"


@pytest.mark.parametrize("payload", ['<!DOCTYPE rss [<!ENTITY x "evil">]><rss/>', '<!ENTITY x SYSTEM "file:///etc/passwd"><rss/>', '<rss>'])
def test_xml_rejects_declarations_entities_and_malformed_documents(payload):
    with pytest.raises(sources.PublicDataSourceError):
        sources.parse_policy_rss(payload, "fed_policy", now=NOW)


def test_stable_upstream_ids_across_observation_times():
    later = datetime(2026, 10, 25, tzinfo=timezone.utc)
    assert sources.parse_ecb_fx(FX, now=NOW)[0]["upstream_id"] == sources.parse_ecb_fx(FX, now=later)[0]["upstream_id"]
    assert sources.parse_policy_rss(RSS, "fed_policy", now=NOW)[0]["upstream_id"] == sources.parse_policy_rss(RSS, "fed_policy", now=later)[0]["upstream_id"]


@pytest.fixture
def http_mock(monkeypatch):
    original = httpx.AsyncClient
    calls = []
    async def no_wait(host):
        return None
    monkeypatch.setattr(sources, "_wait_for_host", no_wait)
    def install(handler):
        def respond(request):
            calls.append(request)
            return handler(request)
        monkeypatch.setattr(sources.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
        return calls
    return install


def test_fetch_uses_fixed_url_bounded_timeout_and_no_auth(http_mock):
    calls = http_mock(lambda request: httpx.Response(200, text=FX))
    rows = asyncio.run(sources.fetch_source("ecb_fx", now=NOW))
    assert len(rows) == 3
    assert str(calls[0].url) == sources._SOURCES["ecb_fx"]["url"]
    assert set(calls[0].extensions["timeout"].values()) == {15.0}
    assert "authorization" not in calls[0].headers


def test_worldbank_fetch_has_fixed_countries_indicator_and_total_bound(http_mock):
    calls = http_mock(lambda request: httpx.Response(200, text=wb_document()))
    rows = asyncio.run(sources.fetch_source("wb_gdp_growth", now=NOW))
    assert len(rows) == 3
    assert calls[0].url.params["per_page"] == "1000"
    assert calls[0].url.params["date"] == "2017:2026"
    assert "/CN;US;JP;DE;GB;IN/indicator/NY.GDP.MKTP.KD.ZG" in calls[0].url.path


def test_unknown_source_and_invalid_context_never_request(http_mock):
    calls = http_mock(lambda request: httpx.Response(200, text=FX))
    for source, now, code in [("https://evil.example", NOW, "unknown_source"), ("ecb_fx", datetime(2026, 10, 24), "invalid_context")]:
        with pytest.raises(sources.PublicDataSourceError) as error:
            asyncio.run(sources.fetch_source(source, now=now))
        assert error.value.error_code == code
    assert calls == []


@pytest.mark.parametrize("status", [301, 302, 403, 429, 500])
def test_redirects_and_http_failures_are_not_followed(http_mock, status):
    calls = http_mock(lambda request: httpx.Response(status, headers={"Location": "https://evil.example/"}))
    with pytest.raises(sources.PublicDataSourceError) as error:
        asyncio.run(sources.fetch_source("ecb_fx", now=NOW))
    assert error.value.error_code == f"http_{status}"
    assert len(calls) == 1


@pytest.mark.parametrize("length_header", [False, True])
def test_response_size_enforced_before_parse(http_mock, monkeypatch, length_header):
    monkeypatch.setattr(sources, "MAX_RESPONSE_BYTES", 16)
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 8
            yield b"x" * 24
    http_mock(lambda request: httpx.Response(200, stream=Stream(), headers={"Content-Length": "32"} if length_header else {}))
    with pytest.raises(sources.PublicDataSourceError) as error:
        asyncio.run(sources.fetch_source("ecb_fx", now=NOW))
    assert error.value.error_code == "response_too_large"


def test_non_fx_xml_is_not_accepted_as_a_dataset():
    with pytest.raises(sources.PublicDataSourceError):
        sources.parse_ecb_fx('<html><Cube time="2026-10-23"><Cube currency="USD" rate="1"/></Cube></html>', now=NOW)


def test_fetch_rejects_invalid_utf8_and_too_many_records(http_mock, monkeypatch):
    http_mock(lambda request: httpx.Response(200, content=b"\xff\xfe"))
    with pytest.raises(sources.PublicDataSourceError) as error:
        asyncio.run(sources.fetch_source("ecb_fx", now=NOW))
    assert error.value.error_code == "invalid_payload"
    http_mock(lambda request: httpx.Response(200, text=FX))
    monkeypatch.setattr(sources, "MAX_RECORDS", 2)
    with pytest.raises(sources.PublicDataSourceError) as error:
        asyncio.run(sources.fetch_source("ecb_fx", now=NOW))
    assert error.value.error_code == "too_many_records"


@pytest.mark.parametrize("error_type,code", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network_error")])
def test_network_errors_do_not_leak_exception_details(http_mock, error_type, code):
    def fail(request):
        raise error_type("private proxy credentials", request=request)
    http_mock(fail)
    with pytest.raises(sources.PublicDataSourceError) as error:
        asyncio.run(sources.fetch_source("ecb_fx", now=NOW))
    assert error.value.error_code == code
    assert "private" not in str(error.value)


def test_empty_payload_is_failure_and_duplicates_are_deduplicated(http_mock):
    http_mock(lambda request: httpx.Response(200, text='<rss><channel/></rss>'))
    with pytest.raises(sources.PublicDataSourceError) as error:
        asyncio.run(sources.fetch_source("fed_policy", now=NOW))
    assert error.value.error_code == "empty_dataset"
    http_mock(lambda request: httpx.Response(200, text=FX.replace('<Cube currency="USD" rate="1.1193"/>', '<Cube currency="USD" rate="1.1193"/>' * 2)))
    assert len(asyncio.run(sources.fetch_source("ecb_fx", now=NOW))) == 3


def test_conflicting_duplicates_fail_instead_of_arbitrary_overwrite(http_mock):
    payload = FX.replace('<Cube currency="USD" rate="1.1193"/>', '<Cube currency="USD" rate="1.1193"/><Cube currency="USD" rate="9.9"/>')
    http_mock(lambda request: httpx.Response(200, text=payload))
    with pytest.raises(sources.PublicDataSourceError, match="冲突"):
        asyncio.run(sources.fetch_source("ecb_fx", now=NOW))


def test_catalog_is_small_official_classified_and_has_safe_refresh_policy():
    assert len(sources.SOURCE_CATALOG) == 6
    assert {source["category"] for source in sources.SOURCE_CATALOG} == {"fx", "macro", "policy"}
    assert all(source["official"] and source["url"].startswith("https://") and source["update_interval_seconds"] >= 3600 for source in sources.SOURCE_CATALOG)


def test_host_rate_limit_reserves_shared_spacing_without_blocking(monkeypatch):
    delays = []
    async def sleep(seconds):
        delays.append(seconds)
    monkeypatch.setattr(sources.time, "monotonic", lambda: 100)
    monkeypatch.setattr(sources.asyncio, "sleep", sleep)
    monkeypatch.setattr(sources, "_next_request", {})
    async def run():
        await sources._wait_for_host("api.worldbank.org")
        await sources._wait_for_host("api.worldbank.org")
        await sources._wait_for_host("www.ecb.europa.eu")
        await sources._wait_for_host("api.worldbank.org")
    asyncio.run(run())
    assert delays == [1.0, 2.0]
