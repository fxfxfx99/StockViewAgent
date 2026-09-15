import time

from app.config import settings
from app.services import impact_analyzer


def test_filter_by_max_age_monkeypatch(monkeypatch):
    monkeypatch.setattr(settings, "news_max_age_days", 7)
    now = int(time.time())
    old = now - 10 * 86400
    items = [
        {"title": "a", "summary": "", "published_ts": now},
        {"title": "b", "summary": "", "published_ts": old},
        {"title": "c", "summary": ""},
    ]
    f = impact_analyzer._filter_by_max_age(items)
    assert len(f) == 2
    titles = {x["title"] for x in f}
    assert "a" in titles and "c" in titles
