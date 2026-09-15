import asyncio
import pytest

from app.services import yahoo_market


def test_stooq_html_challenge_is_not_reported_as_csv_header(monkeypatch):
    class FakeResponse:
        text = "<!DOCTYPE html><html><body>This site requires JavaScript to verify your browser.</body></html>"

        def raise_for_status(self):
            return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(yahoo_market.httpx, "AsyncClient", lambda *args, **kwargs: FakeClient())
    with pytest.raises(ValueError, match="浏览器验证"):
        asyncio.run(yahoo_market._fetch_stooq_candles("000001.SS", "1y", "1d"))
