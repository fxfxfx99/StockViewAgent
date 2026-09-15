from types import SimpleNamespace

from app.services import llm_client


def test_http_client_falls_back_to_second_provider(monkeypatch):
    fake_settings = SimpleNamespace(
        use_litellm=False,
        kline_llm_key="kimi-key",
        kline_llm_key_backup="",
        kline_llm_base="https://api.moonshot.cn/v1",
        kline_llm_model="moonshot-v1-32k",
        kline_llm_fallback_key="openrouter-key",
        kline_llm_fallback_base="https://openrouter.ai/api/v1",
        kline_llm_fallback_model="openrouter/auto",
    )
    monkeypatch.setattr(llm_client, "settings", fake_settings)
    calls = []

    def fake_once(key, messages, *, api_base, model, temperature, timeout):
        calls.append((key, api_base, model))
        if key == "kimi-key":
            return None, "primary unavailable"
        return "fallback ok", None

    monkeypatch.setattr(llm_client, "_chat_completion_http_once", fake_once)
    text, err = llm_client.chat_completion_sync([{"role": "user", "content": "ping"}])
    assert (text, err) == ("fallback ok", None)
    assert calls == [
        ("kimi-key", "https://api.moonshot.cn/v1", "moonshot-v1-32k"),
        ("openrouter-key", "https://openrouter.ai/api/v1", "openrouter/auto"),
    ]
