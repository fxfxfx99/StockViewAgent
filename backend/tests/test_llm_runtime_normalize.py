from app.storage.llm_runtime import normalize_openai_api_base


def test_normalize_strips_chat_completions_suffix():
    assert normalize_openai_api_base("https://api.moonshot.cn/v1/chat/completions") == "https://api.moonshot.cn/v1"
    assert normalize_openai_api_base("https://api.moonshot.cn/v1/") == "https://api.moonshot.cn/v1"


def test_normalize_empty():
    assert normalize_openai_api_base("") == ""
    assert normalize_openai_api_base("   ") == ""
