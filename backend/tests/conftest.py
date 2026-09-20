"""Isolate tests from developer data and credentials."""
import os
import tempfile

import pytest

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="stockviewagent-tests-")
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["COMPANY_AUTO_REFRESH_ENABLED"] = "false"
for _name in (
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_MODEL",
    "KIMI_API_KEY",
    "KLINE_ASSISTANT_API_KEY",
    "TUSHARE_TOKEN",
    "KLINESHARE_API_KEY",
):
    os.environ.setdefault(_name, "")


@pytest.fixture(autouse=True)
def isolate_runtime(tmp_path, monkeypatch):
    from app.config import settings
    from app.services import a_share_stocks, news_watchlist_matcher

    monkeypatch.setattr("app.config._DATA_DIR", tmp_path)
    monkeypatch.setattr(type(settings), "data_dir", property(lambda self: tmp_path))
    for name in (
        "openai_api_key",
        "kimi_api_key",
        "kline_assistant_api_key",
        "tushare_token",
        "klineshare_api_key",
    ):
        if hasattr(settings, name):
            monkeypatch.setattr(settings, name, "")
    a_share_stocks._cache_rows = None
    a_share_stocks._cache_mtime = None
    news_watchlist_matcher.invalidate_match_hints_cache()
    yield
    news_watchlist_matcher.invalidate_match_hints_cache()
