import json

import pytest

from app.services import cn_headline_source_registry as registry


def test_custom_rss_round_trip(tmp_path, monkeypatch):
    target = tmp_path / "sources.json"
    monkeypatch.setattr(registry, "_override_path", lambda: target)

    saved = registry.save_custom_rss_sources(
        [
            {
                "id": "demo",
                "label": "示例源",
                "url": "https://news.example.org/feed.xml",
                "referer": "https://news.example.org/",
                "priority": 70,
            }
        ]
    )

    assert saved == registry.load_custom_rss_sources()
    assert json.loads(target.read_text(encoding="utf-8"))["rss"][0]["id"] == "demo"


def test_custom_rss_rejects_invalid_url(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_override_path", lambda: tmp_path / "sources.json")

    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        registry.save_custom_rss_sources(
            [{"id": "bad", "label": "坏地址", "url": "file:///tmp/feed.xml"}]
        )
