"""
统一大模型调用：默认 OpenAI 兼容 HTTP；可选 LiteLLM 多模型路由。
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import re
from threading import Event
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import settings

_SAFE_ERRORS = {
    "LLM Base URL 配置无效",
    "LLM 返回空内容",
    "LLM 请求超时",
    "LLM 网络请求失败",
    "LLM 响应格式无效",
    "LLM 未返回合法 JSON",
    "LLM 请求失败",
    "分析已取消",
    "未配置大模型 API Key",
    "请配置大模型 Base URL 和 Model",
    "LLM 代理配置不可用",
    "未配置 API Key（请在控制台「大模型接口」保存主密钥或备用密钥，或配置环境变量）",
}
_KIMI_API_HOSTS = {"api.moonshot.cn", "api.moonshot.ai", "api.kimi.com"}
_KIMI_FIXED_TEMPERATURE_MODELS = {"kimi-k2.5", "kimi-k2.6"}


def _telemetry_source(api_base: str) -> str:
    try:
        return urlsplit(api_base).hostname or ""
    except (ValueError, TypeError):
        return ""


def _telemetry_error_code(error: BaseException) -> str:
    code = getattr(error, "status_code", None)
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
    if isinstance(code, int) and 100 <= code <= 599:
        return f"http_{code}"
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(error, httpx.HTTPError):
        return "network_error"
    if isinstance(error, (ValueError, TypeError, KeyError, IndexError, AttributeError)):
        return "invalid_response"
    return "request_failed"


def _response_usage(response: Any) -> Any:
    try:
        usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        # LiteLLM uses a Pydantic Usage object; preserve its reported fields without
        # inferring missing totals or substituting a budget reservation.
        return usage.model_dump() if callable(getattr(usage, "model_dump", None)) else usage
    except Exception:
        # Optional accounting metadata must never invalidate a usable answer.
        return None


@contextmanager
def _llm_attempt(operation: str, *, api_base: str, model: str, user_id: int | None = None):
    """One actually dispatched request, including failed/cancelled attempts."""
    from app.services import runtime_telemetry

    event_id = runtime_telemetry.start_event("llm", operation, source=_telemetry_source(api_base),
                                           model=model, user_id=user_id)
    result = {"status": "failed", "usage": None, "error_code": "request_failed"}
    try:
        yield result
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        result.update(status="cancelled", error_code="cancelled")
        raise
    except BaseException as exc:
        result.update(status="failed", error_code=_telemetry_error_code(exc))
        raise
    finally:
        runtime_telemetry.finish_event(event_id, user_id=user_id, **result)


def safe_error(error: object) -> str:
    """Only return fixed labels or an HTTP status; never stringify arbitrary provider errors."""
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        return f"LLM 服务返回 HTTP {code}" if 100 <= code <= 599 else "LLM 请求失败"
    if isinstance(error, httpx.TimeoutException):
        return "LLM 请求超时"
    if isinstance(error, httpx.HTTPError):
        return "LLM 网络请求失败"
    if isinstance(error, str):
        if error in _SAFE_ERRORS:
            return error
        if re.fullmatch(r"LLM 服务返回 HTTP [1-5][0-9]{2}", error):
            return error
        if error.startswith("HTTP "):
            match = re.match(r"HTTP ([1-5][0-9]{2})", error)
            if match:
                return f"LLM 服务返回 HTTP {match.group(1)}"
    return "LLM 请求失败"


def is_retryable_error(error: object) -> bool:
    """Configuration/authentication errors stop; transient transport/output failures may retry."""
    label = safe_error(error)
    match = re.fullmatch(r"LLM 服务返回 HTTP ([1-5][0-9]{2})", label)
    if match:
        code = int(match.group(1))
        return code in {408, 429} or code >= 500
    return label in {
        "LLM 请求超时",
        "LLM 网络请求失败",
        "LLM 返回空内容",
        "LLM 响应格式无效",
        "LLM 未返回合法 JSON",
    }


def _litellm_model_name() -> str:
    m = (settings.litellm_model or "").strip()
    if m:
        return m
    base = settings.kline_llm_model
    if "/" in base:
        return base
    return f"openai/{base}"


def _llm_keys_try_order() -> list[str]:
    """主 Key 优先，其次控制台备用 Key（同一 API Base / Model）。"""
    keys: list[str] = []
    for k in (settings.kline_llm_key, settings.kline_llm_key_backup):
        k = (k or "").strip()
        if k and k not in keys:
            keys.append(k)
    return keys


def _chat_completion_http_once(
    key: str,
    messages: list[dict[str, Any]],
    *,
    api_base: str,
    model: str,
    temperature: float,
    timeout: float,
) -> tuple[str | None, str | None]:
    try:
        parts = urlsplit(api_base)
    except ValueError:
        return None, "LLM Base URL 配置无效"
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        return None, "LLM Base URL 配置无效"
    body: dict[str, Any] = {"model": model, "messages": messages}
    if parts.hostname in _KIMI_API_HOSTS and model in _KIMI_FIXED_TEMPERATURE_MODELS:
        body["thinking"] = {"type": "disabled"}
        body["max_tokens"] = 16384
    else:
        body["temperature"] = temperature
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0))) as client:
            with _llm_attempt("chat", api_base=api_base, model=model) as event:
                response = client.post(
                    f"{api_base.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                if response.status_code >= 400:
                    event["error_code"] = f"http_{response.status_code}"
                    try:
                        event["usage"] = _response_usage(response.json())
                    except (ValueError, TypeError):
                        pass
                    return None, safe_error(f"LLM 服务返回 HTTP {response.status_code}")
                data = response.json()
                event["usage"] = _response_usage(data)
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    event["error_code"] = "empty_response"
                    return None, "LLM 返回空内容"
                event.update(status="success", error_code=None)
                return content.strip(), None
    except httpx.HTTPError as error:
        return None, safe_error(error)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None, "LLM 响应格式无效"


def chat_completion_sync(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
    timeout: float = 120.0,
    cancel_event: Event | None = None,
) -> tuple[str | None, str | None]:
    """
    同步 chat completions。返回 (content, error_message)。
    主 Key 失败时自动尝试备用 Key（若已在控制台配置）。
    """
    if cancel_event is not None and cancel_event.is_set():
        return None, "分析已取消"
    keys = _llm_keys_try_order()
    fallback_key = settings.kline_llm_fallback_key
    fallback_base = settings.kline_llm_fallback_base
    fallback_model = settings.kline_llm_fallback_model
    if not keys and not (fallback_key and fallback_base and fallback_model):
        return None, "未配置大模型 API Key"
    if settings.use_litellm:
        try:
            import litellm  # type: ignore
        except ImportError:
            litellm = None  # type: ignore[assignment]
        else:
            last_err: str | None = None
            for key in keys:
                if cancel_event is not None and cancel_event.is_set():
                    return None, "分析已取消"
                if not settings.kline_llm_base or not settings.kline_llm_model:
                    continue
                try:
                    model = _litellm_model_name()
                    with _llm_attempt("chat", api_base=settings.kline_llm_base, model=model) as event:
                        resp = litellm.completion(
                            model=model,
                            messages=messages,
                            temperature=temperature,
                            api_key=key,
                            api_base=settings.kline_llm_base,
                            timeout=timeout,
                            # Keep retries visible in our existing key/provider
                            # loop, instead of one event hiding several charges.
                            num_retries=0,
                            max_retries=0,
                            caching=False,
                        )
                        event["usage"] = _response_usage(resp)
                        ch = resp.choices[0].message
                        content = (ch.content or "").strip() if ch else ""
                        event.update(status="success" if content else "failed",
                                     error_code=None if content else "empty_response")
                        return content or None, None
                except Exception as e:  # noqa: BLE001
                    last_err = safe_error(str(e))
                    continue
            if last_err:
                pass
    last_err: str | None = None
    candidates = [(key, settings.kline_llm_base, settings.kline_llm_model) for key in keys]
    if fallback_key and fallback_base and fallback_model:
        candidates.append((fallback_key, fallback_base, fallback_model))
    for key, api_base, model in candidates:
        if cancel_event is not None and cancel_event.is_set():
            return None, "分析已取消"
        if not api_base or not model:
            last_err = "请配置大模型 Base URL 和 Model"
            continue
        content, err = _chat_completion_http_once(
            key,
            messages,
            api_base=api_base,
            model=model,
            temperature=temperature,
            timeout=timeout,
        )
        if content and not err:
            return content, None
        last_err = safe_error(err or "LLM 返回空内容")
    return None, last_err or "LLM 请求失败"


def parse_json_response(raw: str) -> Any:
    """Accept bare JSON or one fenced JSON block; reject trailing prose and NaN."""
    content = raw.strip()
    if content.startswith("```") and content.endswith("```"):
        lines = content.splitlines()
        if lines[0].strip().lower() not in {"```", "```json"}:
            raise ValueError("Unsupported JSON fence")
        content = "\n".join(lines[1:-1]).strip()
    elif content.startswith("```"):
        parts = content.split("```")
        content = parts[1] if len(parts) > 1 else content
        if content.lower().startswith("json"):
            content = content[4:].lstrip()

    def reject_constant(value: str):
        raise ValueError("Non-finite JSON number")

    return json.loads(content, parse_constant=reject_constant)


def chat_completion_json_array(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
    timeout: float = 90.0,
    cancel_event: Event | None = None,
) -> tuple[Any | None, str | None]:
    """尝试解析助手返回为 JSON（数组或对象）。失败返回 (None, error)。"""
    raw, err = chat_completion_sync(
        messages, temperature=temperature, timeout=timeout, cancel_event=cancel_event
    )
    if err or not raw:
        return None, safe_error(err or "LLM 返回空内容")
    try:
        return parse_json_response(raw), None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None, "LLM 未返回合法 JSON"
