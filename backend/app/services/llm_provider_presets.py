"""OpenAI 兼容大模型：提供商预设与根据 API Base 推断默认模型（与前端控制台共用逻辑）。"""
from __future__ import annotations

from urllib.parse import urlparse

from app.storage.llm_runtime import normalize_openai_api_base

# 静态预设：切换提供商时一键填入 Base / 默认对话与向量模型（向量可与对话同根或单独网关）
PROVIDER_PRESETS: list[dict[str, object]] = [
    {
        "id": "kimi",
        "label": "Kimi (Moonshot)",
        "api_base": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k",
        "embedding_model": "moonshot-v1-embedding-2",
        "embedding_api_base": "",
        "chat_models": [
            "moonshot-v1-8k",
            "moonshot-v1-32k",
            "moonshot-v1-128k",
            "kimi-k2-0905-preview",
            "kimi-k2-turbo-preview",
            "kimi-k2-thinking",
            "kimi-k2-thinking-turbo",
        ],
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "embedding_model": "text-embedding-3-small",
        "embedding_api_base": "",
        "chat_models": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "o1-mini", "o1"],
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "api_base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "embedding_model": "text-embedding-3-small",
        "embedding_api_base": "",
        "chat_models": ["deepseek-chat", "deepseek-reasoner"],
    },
    {
        "id": "dashscope",
        "label": "阿里云 DashScope（OpenAI 兼容）",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "embedding_model": "text-embedding-v4",
        "embedding_api_base": "",
        "chat_models": ["qwen-plus", "qwen-turbo", "qwen-max", "qwen-long"],
    },
    {
        "id": "siliconflow",
        "label": "SiliconFlow",
        "api_base": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "embedding_model": "BAAI/bge-m3",
        "embedding_api_base": "",
        "chat_models": ["Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"],
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "api_base": "https://openrouter.ai/api/v1",
        "model": "openrouter/auto",
        "embedding_model": "openai/text-embedding-3-small",
        "embedding_api_base": "",
        "chat_models": ["openrouter/auto", "openai/gpt-4o-mini", "google/gemini-2.5-flash"],
    },
    {
        "id": "ollama",
        "label": "Ollama（本地）",
        "api_base": "http://127.0.0.1:11434/v1",
        "model": "llama3.2",
        "embedding_model": "nomic-embed-text",
        "embedding_api_base": "",
        "chat_models": ["llama3.2", "qwen2.5", "deepseek-r1", "mistral"],
    },
]


def _host_path_key(url: str) -> tuple[str, str]:
    p = urlparse(url)
    return (p.netloc.lower(), (p.path or "").lower().rstrip("/"))


def infer_from_api_base(api_base: str) -> dict[str, object] | None:
    """
    根据用户粘贴的 API Base 推断提供商与推荐模型；无法识别时返回 None。
    embedding_api_base 空字符串表示与对话 API Base 相同。
    """
    b = normalize_openai_api_base(api_base)
    if not b or not b.startswith("http"):
        return None
    host, path = _host_path_key(b)

    # 精确匹配预设（归一化后比较）
    for preset in PROVIDER_PRESETS:
        pb = normalize_openai_api_base(str(preset["api_base"]))
        if pb and b.rstrip("/") == pb.rstrip("/"):
            return _preset_to_infer_result(preset, matched_by="preset_url")

    # 关键字 / 域名规则
    if "moonshot.cn" in host or "moonshot" in host:
        return _preset_to_infer_result(_preset_by_id("kimi"), matched_by="host_moonshot")
    if host.endswith("openai.com") or "api.openai.com" in host:
        return _preset_to_infer_result(_preset_by_id("openai"), matched_by="host_openai")
    if "deepseek.com" in host:
        return _preset_to_infer_result(_preset_by_id("deepseek"), matched_by="host_deepseek")
    if "dashscope.aliyuncs.com" in host:
        return _preset_to_infer_result(_preset_by_id("dashscope"), matched_by="host_dashscope")
    if "siliconflow" in host:
        return _preset_to_infer_result(_preset_by_id("siliconflow"), matched_by="host_siliconflow")
    if "openrouter.ai" in host:
        return _preset_to_infer_result(_preset_by_id("openrouter"), matched_by="host_openrouter")
    if "11434" in b or "ollama" in host:
        return _preset_to_infer_result(_preset_by_id("ollama"), matched_by="host_ollama")

    # 通用 OpenAI 兼容：仅给出保守默认，向量仍用常见 OpenAI 名（用户可改）
    if "/v1" in path or host:
        return {
            "preset_id": "generic_openai_compat",
            "label": "OpenAI 兼容（未识别厂商）",
            "api_base": b,
            "model": "gpt-4o-mini",
            "embedding_model": "text-embedding-3-small",
            "embedding_api_base": "",
            "chat_models": ["gpt-4o-mini", "gpt-4o"],
            "matched_by": "generic",
            "hint": "未识别具体厂商，已填入通用占位模型名；请按服务商文档改为实际模型 ID。",
        }

    return None


def _preset_by_id(pid: str) -> dict[str, object]:
    for p in PROVIDER_PRESETS:
        if p["id"] == pid:
            return dict(p)
    raise KeyError(pid)


def _preset_to_infer_result(preset: dict[str, object], *, matched_by: str) -> dict[str, object]:
    out = {
        "preset_id": preset["id"],
        "label": preset["label"],
        "api_base": preset["api_base"],
        "model": preset["model"],
        "embedding_model": preset["embedding_model"],
        "embedding_api_base": preset["embedding_api_base"],
        "chat_models": list(preset.get("chat_models") or []),
        "matched_by": matched_by,
        "hint": None,
    }
    return out


def public_presets_list() -> list[dict[str, object]]:
    """供 GET /presets：不含过大字段，仅展示与选择所需。"""
    out: list[dict[str, object]] = []
    for p in PROVIDER_PRESETS:
        out.append(
            {
                "id": p["id"],
                "label": p["label"],
                "api_base": p["api_base"],
                "model": p["model"],
                "embedding_model": p["embedding_model"],
                "embedding_api_base": p["embedding_api_base"],
                "chat_models": p["chat_models"],
            }
        )
    return out
