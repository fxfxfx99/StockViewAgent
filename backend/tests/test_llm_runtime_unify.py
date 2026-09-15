"""后端统一归一化 llm_runtime：模型别名、按 Base 补全向量等。"""
import json

import app.config as app_config
from app.services.llm_runtime_unify import resolve_chat_model_id, unify_llm_runtime_fields
from app.storage.llm_runtime import apply_llm_patch_and_unify


def test_unify_moonshot_empty_model_fills_from_infer():
    out = unify_llm_runtime_fields(
        {
            "api_key": "",
            "api_key_backup": "",
            "api_base": "https://api.moonshot.cn/v1",
            "model": "",
            "embedding_model": "",
            "embedding_api_base": "",
        }
    )
    assert out["model"] == "moonshot-v1-32k"
    assert out["embedding_model"] == "moonshot-v1-embedding-2"
    assert out["api_base"] == "https://api.moonshot.cn/v1"


def test_unify_kimi_marketing_name_to_api_id():
    out = unify_llm_runtime_fields(
        {
            "api_key": "sk-test",
            "api_key_backup": "",
            "api_base": "https://api.moonshot.cn/v1",
            "model": "kimi 2.5",
            "embedding_model": "",
            "embedding_api_base": "",
        }
    )
    assert out["model"] == "kimi-k2-turbo-preview"
    assert out["embedding_model"] == "moonshot-v1-embedding-2"


def test_unify_preserves_cross_provider_fallback():
    out = unify_llm_runtime_fields(
        {
            "api_base": "https://api.moonshot.cn/v1",
            "model": "moonshot-v1-32k",
            "fallback_api_key": " fallback-key ",
            "fallback_api_base": "https://openrouter.ai/api/v1/chat/completions",
            "fallback_model": "",
        }
    )
    assert out["fallback_api_key"] == "fallback-key"
    assert out["fallback_api_base"] == "https://openrouter.ai/api/v1"
    assert out["fallback_model"] == "openrouter/auto"


def test_resolve_chat_model_id_prefers_infer_when_empty():
    inf = {
        "preset_id": "kimi",
        "model": "moonshot-v1-32k",
        "chat_models": ["moonshot-v1-32k"],
    }
    assert resolve_chat_model_id("", inf) == "moonshot-v1-32k"


def test_settings_kline_llm_model_resolves_kimi_marketing_at_runtime(monkeypatch):
    """配置读取路径须与保存 unify 一致，避免 llm_runtime.json 仍为 kimi 2.5 时请求失败。"""

    def fake_load():
        return {
            "api_key": "sk-test",
            "api_base": "https://api.moonshot.cn/v1",
            "model": "kimi 2.5",
            "embedding_model": "moonshot-v1-embedding-2",
            "embedding_api_base": "",
        }

    monkeypatch.setattr("app.storage.llm_runtime.load_runtime", fake_load)
    assert app_config.settings.kline_llm_model == "kimi-k2-turbo-preview"


def test_apply_llm_patch_and_unify_writes_file(tmp_path, monkeypatch):
    import app.storage.llm_runtime as lr

    fake_file = tmp_path / "llm_runtime.json"
    monkeypatch.setattr(lr, "_FILE", fake_file)

    apply_llm_patch_and_unify(
        {
            "api_base": "https://api.moonshot.cn/v1",
            "model": "",
            "embedding_model": "",
            "embedding_api_base": "",
        }
    )
    data = json.loads(fake_file.read_text(encoding="utf-8"))
    assert data["model"] == "moonshot-v1-32k"
    assert data["embedding_model"] == "moonshot-v1-embedding-2"
