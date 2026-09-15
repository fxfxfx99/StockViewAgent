from app.services.llm_provider_presets import infer_from_api_base, public_presets_list


def test_public_presets_include_kimi():
    ids = {p["id"] for p in public_presets_list()}
    assert "kimi" in ids
    assert "openai" in ids


def test_infer_moonshot_normalizes_chat_completions_suffix():
    r = infer_from_api_base("https://api.moonshot.cn/v1/chat/completions")
    assert r is not None
    assert r["preset_id"] == "kimi"
    assert r["model"] == "moonshot-v1-32k"


def test_infer_openai_host():
    r = infer_from_api_base("https://api.openai.com/v1")
    assert r is not None
    assert r["preset_id"] == "openai"


def test_infer_generic():
    r = infer_from_api_base("https://unknown-gateway.example/v1")
    assert r is not None
    assert r["preset_id"] == "generic_openai_compat"
    assert r["hint"]


def test_infer_invalid_empty():
    assert infer_from_api_base("") is None
    assert infer_from_api_base("not-a-url") is None
