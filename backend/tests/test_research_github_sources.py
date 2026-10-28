"""Pinned public research evidence and bounded, source-balanced prompts."""
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from app.services import research_generation as generation
from app.storage import research_store as store

USER = 83


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def github_sources(monkeypatch):
    sources = {(repo, path): {"sha": f"{index:040x}",
                            "content": f"# {repo}\n来源与历史证据\n".encode()}
               for index, (repo, path, _) in enumerate(generation.SOURCES, start=1)}
    mock = SimpleNamespace(sources=sources, requests=[], fail_host=None)
    original = httpx.AsyncClient

    def respond(request):
        mock.requests.append(request)
        if request.url.host == mock.fail_host:
            raise httpx.ConnectError("offline", request=request)
        if request.url.host == "api.github.com":
            repo = request.url.path.removeprefix("/repos/").removesuffix("/commits")
            assert request.url.params["per_page"] == "1"
            source = sources[(repo, request.url.params["path"])]
            return httpx.Response(200, json=[{"sha": source["sha"]}])
        assert request.url.host == "raw.githubusercontent.com"
        owner, name, sha, path = request.url.path.lstrip("/").split("/", 3)
        source = sources[(f"{owner}/{name}", path)]
        assert sha == source["sha"]
        return httpx.Response(200, content=source["content"])

    monkeypatch.setattr(generation.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    return mock


def source_state(repo, path, user_id=USER):
    key = hashlib.sha256(f"{repo}/{path}".encode()).hexdigest()[:20]
    return store.get_state(user_id, f"source:{key}")


def references(user_id=USER, context=None):
    return json.loads(generation._messages(user_id, context or {})[1]["content"])["reference_data"]


@pytest.mark.anyio
async def test_refresh_pins_commit_and_hash_for_each_source(github_sources):
    result = await generation.refresh_github_sources(USER)
    assert len(result["sources"]) == len(generation.SOURCES)
    assert all(row["status"] == "changed" for row in result["sources"])
    assert len(github_sources.requests) == 2 * len(generation.SOURCES)
    for repo, path, purpose in generation.SOURCES:
        source = github_sources.sources[(repo, path)]
        state = source_state(repo, path)
        card = store.get_entity(USER, "knowledge", state["card_id"])["payload"]
        assert card["repository"] == repo and card["path"] == path
        assert card["commit"] == state["commit"] == source["sha"]
        assert card["content_hash"] == state["content_hash"] == hashlib.sha256(source["content"]).hexdigest()
        assert card["url"] == f"https://github.com/{repo}/blob/{source['sha']}/{path}"
        assert card["license_url"] == f"https://github.com/{repo}/blob/{source['sha']}/LICENSE"
        assert card["purpose"] == purpose and card["performance_validated"] is False
        assert card["excerpt"] == source["content"].decode()
    cnequity = next(row for row in references() if "/rootSunc/CNEquity/" in row["source"])
    assert "/docs/zh/datasets/query-guide.md" in cnequity["source"]
    assert all(topic in cnequity["purpose"] for topic in ("不可变", "行级", "历史时点"))
    assert store.list_entities(USER + 1, "knowledge") == []


@pytest.mark.anyio
async def test_unchanged_content_does_not_duplicate_or_replace_pinned_evidence(github_sources, monkeypatch):
    selected = next(source for source in generation.SOURCES if source[0] == "rootSunc/CNEquity")
    monkeypatch.setattr(generation, "SOURCES", (selected,))
    repo, path, _ = selected
    await generation.refresh_github_sources(USER)
    original_state = source_state(repo, path)
    original_card = store.get_entity(USER, "knowledge", original_state["card_id"])
    source = github_sources.sources[(repo, path)]
    source["sha"] = "a" * 40
    unchanged = await generation.refresh_github_sources(USER)
    assert unchanged["sources"][0]["status"] == "unchanged"
    assert len(store.list_entities(USER, "knowledge")) == 1
    assert source_state(repo, path) == original_state

    source["sha"] = "b" * 40
    source["content"] += "新增可核验修订\n".encode()
    changed = await generation.refresh_github_sources(USER)
    assert changed["sources"][0]["status"] == "changed"
    assert len(store.list_entities(USER, "knowledge")) == 2
    assert source_state(repo, path)["card_id"] != original_card["id"]
    assert source_state(repo, path)["commit"] == "b" * 40
    assert store.get_entity(USER, "knowledge", original_card["id"]) == original_card
    assert "/blob/" + "b" * 40 + "/" in references()[0]["source"]


@pytest.mark.anyio
@pytest.mark.parametrize("fail_host", ["api.github.com", "raw.githubusercontent.com"])
async def test_network_failure_preserves_previous_cards_and_prompt(github_sources, fail_host):
    await generation.refresh_github_sources(USER)
    previous_cards = store.list_entities(USER, "knowledge")
    previous_states = [source_state(repo, path) for repo, path, _ in generation.SOURCES]
    previous_references = references()
    github_sources.fail_host = fail_host
    result = await generation.refresh_github_sources(USER)
    assert all(row["status"] == "unavailable" for row in result["sources"])
    assert store.list_entities(USER, "knowledge") == previous_cards
    assert [source_state(repo, path) for repo, path, _ in generation.SOURCES] == previous_states
    assert references() == previous_references


@pytest.mark.anyio
async def test_prompt_keeps_latest_card_per_configured_source_with_bounded_excerpts(github_sources, monkeypatch):
    configured = generation.SOURCES
    for source in github_sources.sources.values():
        source["content"] += ("完整资料" * 600).encode()
    await generation.refresh_github_sources(USER)
    # More than four newer cards from one source used to displace every other
    # source, including CNEquity, from the global latest-card query.
    busy = configured[0]
    monkeypatch.setattr(generation, "SOURCES", (busy,))
    for revision in range(6):
        source = github_sources.sources[busy[:2]]
        source["sha"] = f"{revision + 100:040x}"
        source["content"] = f"# Revision {revision}\n".encode() + b"x" * 2000
        await generation.refresh_github_sources(USER)
    monkeypatch.setattr(generation, "SOURCES", configured)
    store.put_entity(USER, "knowledge", {"url": "https://unconfigured.example/", "purpose": "ignored"})
    context = {"goal": "research", "holdout": "SECRET_HOLDOUT", "forward_metrics": "SECRET_FORWARD"}
    messages = generation._messages(USER, context)
    cards = json.loads(messages[1]["content"])["reference_data"]
    assert len(cards) == len(configured)
    assert sum(len(card["excerpt"]) for card in cards) <= 500 * len(configured)
    for card, (repo, path, purpose) in zip(cards, configured):
        state = source_state(repo, path)
        assert card["source"] == f"https://github.com/{repo}/blob/{state['commit']}/{path}"
        assert card["purpose"] == purpose and len(card["excerpt"]) == 500
    assert cards[0]["excerpt"].startswith("# Revision 5\n")
    assert "unconfigured.example" not in str(messages) and "SECRET" not in str(messages)
    assert references(USER + 1) == []
