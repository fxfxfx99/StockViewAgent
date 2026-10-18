"""Actual dispatch accounting must not confuse reservations with provider usage."""
import asyncio
from copy import deepcopy
import json
import sys
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

import app.services
from app.schemas.research import StrategySpecV1
from app.security.user_context import current_user_id
from app.services import issuer_rag_service as rag, llm_client as llm, research_generation as research
from app.storage import research_store as store


@pytest.fixture
def telemetry(monkeypatch):
    events, timeline = [], []
    def start(kind, operation, **kwargs):
        uid = kwargs.get("user_id") if kwargs.get("user_id") is not None else current_user_id.get() or 1
        eid = f"event-{len(events)}"
        events.append({"id": eid, "kind": kind, "operation": operation, **kwargs, "user_id": uid})
        timeline.append(("start", eid))
        return eid
    def finish(event_id, **kwargs):
        row = next(row for row in events if row["id"] == event_id)
        assert "finish" not in row
        row["finish"] = deepcopy(kwargs)
        timeline.append(("finish", event_id))
    fake = SimpleNamespace(start_event=start, finish_event=finish)
    monkeypatch.setitem(sys.modules, "app.services.runtime_telemetry", fake)
    monkeypatch.setattr(app.services, "runtime_telemetry", fake, raising=False)
    return events, timeline


def settings(**changes):
    return SimpleNamespace(use_litellm=False, litellm_model="",
        kline_llm_key="PRIMARY-SECRET", kline_llm_key_backup="BACKUP-SECRET",
        kline_llm_base="https://primary.example/private-path/v1", kline_llm_model="primary-model",
        kline_llm_fallback_key="FALLBACK-SECRET", kline_llm_fallback_base="https://fallback.example/v1",
        kline_llm_fallback_model="fallback-model", **changes)


def install_client(monkeypatch, handler, timeline):
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            assert timeline[-1][0] == "start"
            timeline.append(("request", urlsplit_host(url)))
            return handler(url, kwargs)
    monkeypatch.setattr(llm.httpx, "Client", Client)


def urlsplit_host(url):
    from urllib.parse import urlsplit
    return urlsplit(url).hostname


def response(url, status=200, *, content="response-secret", usage=None):
    body = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(status, request=httpx.Request("POST", url), json=body)


def test_http_each_backup_and_fallback_has_own_event_with_only_reported_usage(monkeypatch, telemetry):
    events, timeline = telemetry
    monkeypatch.setattr(llm, "settings", settings())
    usage = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25,
             "prompt_tokens_details": {"cached_tokens": 7}, "completion_tokens_details": {"reasoning_tokens": 2}}
    def handle(url, kwargs):
        key = kwargs["headers"]["Authorization"]
        if "PRIMARY" in key:
            return response(url, 503, usage={"total_tokens": 3})
        if "BACKUP" in key:
            raise httpx.ReadTimeout("PRIVATE-ERROR token=DO-NOT-STORE", request=httpx.Request("POST", url))
        return response(url, usage=usage)
    install_client(monkeypatch, handle, timeline)
    token = current_user_id.set(41)
    try:
        assert llm.chat_completion_sync([{"role": "user", "content": "PROMPT-SECRET"}]) == ("response-secret", None)
    finally:
        current_user_id.reset(token)
    assert len(events) == 3 and {row["user_id"] for row in events} == {41}
    assert [row["source"] for row in events] == ["primary.example", "primary.example", "fallback.example"]
    assert [row["finish"]["status"] for row in events] == ["failed", "failed", "success"]
    assert [row["finish"]["error_code"] for row in events] == ["http_503", "timeout", None]
    assert [row["finish"]["usage"] for row in events] == [{"total_tokens": 3}, None, usage]
    serialized = json.dumps(events)
    assert all(secret not in serialized for secret in ("SECRET", "PRIVATE-ERROR", "response-secret", "private-path", "DO-NOT-STORE"))
    assert [phase for phase, _ in timeline] == ["start", "request", "finish"] * 3


@pytest.mark.parametrize("content", ["", None])
def test_invalid_response_retains_actual_usage(monkeypatch, telemetry, content):
    events, timeline = telemetry
    install_client(monkeypatch, lambda url, kwargs: response(url, content=content, usage={"total_tokens": 9}), timeline)
    _, error = llm._chat_completion_http_once("SECRET", [], api_base="https://model.example/v1",
                                            model="test", temperature=0.2, timeout=1)
    assert error
    assert events[0]["finish"] == {"status": "failed", "error_code": "empty_response", "usage": {"total_tokens": 9}, "user_id": None}


def test_missing_configuration_invalid_base_and_pre_cancel_are_not_calls(monkeypatch, telemetry):
    fake = settings()
    fake.kline_llm_key = fake.kline_llm_key_backup = fake.kline_llm_fallback_key = ""
    monkeypatch.setattr(llm, "settings", fake)
    assert llm.chat_completion_sync([])[1]
    fake.kline_llm_key = "SECRET"
    fake.kline_llm_base = ""
    assert llm.chat_completion_sync([])[1]
    assert llm._chat_completion_http_once("SECRET", [], api_base="https://user:password@model.example/v1",
                                         model="test", temperature=0.2, timeout=1)[1]
    cancellation = Event()
    cancellation.set()
    assert llm.chat_completion_sync([], cancel_event=cancellation)[1] == "分析已取消"
    assert telemetry[0] == []


def test_litellm_reports_sdk_usage_and_disables_invisible_retries(monkeypatch, telemetry):
    events, timeline = telemetry
    fake = settings()
    fake.use_litellm = True
    monkeypatch.setattr(llm, "settings", fake)
    class Usage:
        def model_dump(self):
            return {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
    def completion(**kwargs):
        assert timeline[-1][0] == "start"
        assert kwargs["num_retries"] == kwargs["max_retries"] == 0
        assert kwargs["caching"] is False
        return SimpleNamespace(usage=Usage(), choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    assert llm.chat_completion_sync([]) == ("ok", None)
    assert len(events) == 1 and events[0]["model"] == "openai/primary-model"
    assert events[0]["finish"]["usage"]["total_tokens"] == 6


def test_litellm_failures_and_http_fallbacks_all_remain_visible(monkeypatch, telemetry):
    events, timeline = telemetry
    fake = settings()
    fake.use_litellm = True
    monkeypatch.setattr(llm, "settings", fake)
    def completion(**kwargs):
        raise RuntimeError("DO-NOT-STORE")
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    install_client(monkeypatch, lambda url, kwargs: response(url, status=200 if "fallback" in url else 503), timeline)
    assert llm.chat_completion_sync([]) == ("response-secret", None)
    assert len(events) == 5
    assert [row["finish"]["status"] for row in events] == ["failed"] * 4 + ["success"]
    assert all(row["finish"]["usage"] is None for row in events)
    assert "DO-NOT-STORE" not in json.dumps(events)


def test_embedding_url_and_key_retries_each_have_their_own_event(monkeypatch, telemetry):
    events, timeline = telemetry
    monkeypatch.setattr(rag, "_embed_keys_try_order", lambda: ["primary-secret", "backup-secret"])
    monkeypatch.setattr(rag, "_embedding_model", lambda: "embedding-model")
    urls = ["https://embed.example/private/embeddings", "https://embed.example/v1/embeddings"]
    monkeypatch.setattr(rag, "_embedding_request_urls", lambda: urls)
    def handle(url, kwargs):
        if url == urls[0]:
            return httpx.Response(404, request=httpx.Request("POST", url), json={"error": {"message": "PRIVATE-ERROR"}})
        if "primary" in kwargs["headers"]["Authorization"]:
            return httpx.Response(500, request=httpx.Request("POST", url), json={"usage": {"total_tokens": 7}})
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 2, "total_tokens": 2}})
    install_client(monkeypatch, handle, timeline)
    assert rag._embed_batch(["PRIVATE-TEXT"]) == ([[0.1, 0.2]], None)
    assert len(events) == 4 and {row["operation"] for row in events} == {"embedding"}
    assert {row["source"] for row in events} == {"embed.example"}
    assert [row["finish"]["error_code"] for row in events] == ["http_404", "http_500", "http_404", None]
    assert events[1]["finish"]["usage"] == {"total_tokens": 7}
    assert "PRIVATE" not in json.dumps(events)


def test_embedding_empty_batch_and_missing_key_do_not_count(monkeypatch, telemetry):
    monkeypatch.setattr(rag, "_embed_keys_try_order", lambda: [])
    assert rag._embed_batch([]) == ([], None)
    assert rag._embed_batch(["text"])[1]
    assert telemetry[0] == []


def research_config(monkeypatch):
    monkeypatch.setattr(research, "settings", settings())


def strategy():
    return StrategySpecV1.model_validate({"name": "test", "hypothesis": "test", "entry": {
        "op": "gt", "args": [{"op": "field", "field": "c"}, {"op": "const", "value": 10}]}}).model_dump(mode="json")


def test_research_replay_has_one_explicit_account_event_and_unknown_usage(monkeypatch, telemetry):
    research_config(monkeypatch)
    events, timeline = telemetry
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            assert timeline[-1][0] == "start"
            return response(url, content=json.dumps(strategy()))
    monkeypatch.setattr(research.httpx, "AsyncClient", Client)
    async def run():
        first = await research.generate_candidate(41, {}, attempt_key="one")
        assert first["status"] == "generated"
        assert await research.generate_candidate(41, {}, attempt_key="one") == first
    token = current_user_id.set(99)
    try:
        asyncio.run(run())
    finally:
        current_user_id.reset(token)
    assert len(events) == 1 and events[0]["user_id"] == events[0]["finish"]["user_id"] == 41
    assert events[0]["operation"] == "research_proposal"
    assert events[0]["finish"]["usage"] is None
    assert store.budget_status(41)["used"]["tokens"] > 0  # reservation is not actual token telemetry


def test_cancelled_research_request_finishes_event_without_guessing_usage(monkeypatch, telemetry):
    research_config(monkeypatch)
    events, timeline = telemetry
    async def run():
        entered = asyncio.Event()
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, url, **kwargs):
                entered.set()
                await asyncio.Event().wait()
        monkeypatch.setattr(research.httpx, "AsyncClient", Client)
        task = asyncio.create_task(research.generate_candidate(42, {}, attempt_key="cancel"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await research.generate_candidate(42, {}, attempt_key="cancel"))["status"] == "in_progress"
    asyncio.run(run())
    assert len(events) == 1
    assert events[0]["finish"] == {"status": "cancelled", "usage": None, "error_code": "cancelled", "user_id": 42}


def test_research_no_config_or_budget_never_emits_event(monkeypatch, telemetry):
    fake = settings()
    fake.kline_llm_key = ""
    monkeypatch.setattr(research, "settings", fake)
    assert asyncio.run(research.generate_candidate(41, {}, attempt_key="no-key"))["status"] == "configuration_required"
    fake.kline_llm_key = "SECRET"
    store.update_config(41, {"daily_calls": 0})
    with pytest.raises(store.BudgetExceeded):
        asyncio.run(research.generate_candidate(41, {}, attempt_key="no-budget"))
    assert telemetry[0] == []


@pytest.mark.parametrize("usage", [{"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}, None])
def test_actual_chat_path_writes_real_sqlite_usage_and_original_actor(monkeypatch, usage):
    from app.services import runtime_telemetry

    fake = settings()
    fake.kline_llm_key_backup = fake.kline_llm_fallback_key = ""
    monkeypatch.setattr(llm, "settings", fake)
    requests = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            requests.append(url)
            # The durable request exists before transport dispatch. Finishing
            # must retain this actor even if ambient context later changes.
            assert runtime_telemetry.overview(41, "all")["llm"]["pending"] == 1
            current_user_id.set(42)
            return response(url, usage=usage)
    monkeypatch.setattr(llm.httpx, "Client", Client)
    token = current_user_id.set(41)
    try:
        assert llm.chat_completion_sync([{"role": "user", "content": "PROMPT-SECRET"}]) == ("response-secret", None)
    finally:
        current_user_id.reset(token)
    result = runtime_telemetry.overview(41, "all")
    assert len(requests) == 1
    assert result["llm"]["calls"] == result["llm"]["success"] == 1
    assert result["llm"]["pending"] == result["llm"]["failed"] == 0
    assert result["llm"]["reported_token_calls"] == (1 if usage else 0)
    assert result["llm"]["unknown_token_calls"] == (0 if usage else 1)
    assert result["llm"]["input_tokens"] == (20 if usage else None)
    assert result["llm"]["output_tokens"] == (5 if usage else None)
    assert result["llm"]["total_tokens"] == (25 if usage else None)
    assert result["recent"][0]["source"] == "primary.example"
    assert result["recent"][0]["operation"] == "chat"
    assert runtime_telemetry.overview(42, "all")["llm"]["calls"] == 0
    assert store.budget_status(41)["used"]["tokens"] == 0
    serialized = json.dumps(result)
    assert all(value not in serialized for value in ("SECRET", "response-secret", "private-path"))
