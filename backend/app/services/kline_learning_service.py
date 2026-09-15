"""K线助手：本地资料 + Prompt + LLM。目录：项目根 kline_learning/"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.services import llm_client

INDEX_NAME, PROMPT_NAME, FILES_SUB = "materials.json", "prompt.json", "files"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_EXT = {".txt", ".md", ".markdown", ".pdf"}
DEFAULT_SYSTEM = (
    "你是熟悉技术分析与中国A股的助手。结合用户自编学习资料解读K线与量价："
    "先概括趋势与关键价位，再分析量价与结构；随后给出可讨论的短线/波段交易思路（须标注风险与假设），"
    "并明确声明仅为学习交流、不构成投资建议。"
    "全文分条阐述；结尾再次注明：以上内容仅供学习，不构成投资建议。"
)
DEFAULT_USER_TEMPLATE = (
    "标的：{symbol} 区间：{range_param} 周期：{interval}\n【指标】\n{metrics}\n"
    "【K线摘要】\n{candles_summary}\n【学习资料】\n{learning_context}\n"
    "请完成：1）技术面解读；2）与资料观点的对照；3）交易思路与风险提示（非投资建议）。"
)
DEFAULT_MARKET_SYSTEM = (
    "你是熟悉中国A股大盘与技术分析的助手。结合主要指数的K线与量能，从趋势、关键位、结构三方面简明解读；"
    "结尾注明：以上内容仅供学习，不构成投资建议。"
)
DEFAULT_MARKET_USER_TEMPLATE = (
    "指数：{index_label}（键 {index_key}） 区间：{range_param} 周期：{interval}\n【指标】\n{metrics}\n"
    "【K线摘要】\n{candles_summary}\n【学习资料】\n{learning_context}\n请从大盘视角解读，可对照学习资料观点。"
)

DEFAULT_STRATEGIES = {
    "comprehensive": {
        "label": "综合研判",
        "prompt": "综合趋势、技术指标、量价结构与风险，保持结论均衡，明确数据依据。",
    },
    "trend": {
        "label": "趋势分析",
        "prompt": "重点分析趋势方向、趋势强弱、均线结构、拐点信号与趋势失效条件。",
    },
    "technical": {
        "label": "技术指标",
        "prompt": "重点分析可由数据支持的技术指标、量价配合、背离与超买超卖风险，不虚构缺失指标。",
    },
    "short_term": {
        "label": "短线视角",
        "prompt": "从短线交易观察视角分析波动、量能、支撑压力和触发条件，只给条件式观察参考，不给确定性买卖指令。",
    },
    "long_term": {
        "label": "中长期视角",
        "prompt": "从中长期视角分析趋势结构、估值数据（若提供）、关键区间和主要不确定性，避免用短期波动替代长期判断。",
    },
    "risk": {
        "label": "风险提示",
        "prompt": "优先识别趋势失效、放量下跌、流动性、数据滞后与模型误判风险，并给出可验证的风险观察信号。",
    },
}

BASIC_PROMPT_MODULES = {
    "trend_structure": {
        "label": "趋势结构",
        "prompt": "识别主趋势、次级趋势、均线相对位置、趋势延续与失效条件。",
    },
    "volume_price": {
        "label": "量价关系",
        "prompt": "分析成交量、成交额、放量/缩量、量价背离、放量滞涨或缩量回落。",
    },
    "support_resistance": {
        "label": "支撑压力",
        "prompt": "给出可由近期高低点、均线、密集成交区支撑的关键支撑位与压力位。",
    },
    "risk_control": {
        "label": "风险控制",
        "prompt": "明确趋势失效、破位、数据滞后、流动性和模型误判风险，避免确定性买卖指令。",
    },
    "trade_plan": {
        "label": "交易计划",
        "prompt": "只输出条件式观察方案：触发条件、观察位、仓位约束、止损/止盈逻辑和不做条件。",
    },
    "data_boundary": {
        "label": "数据边界",
        "prompt": "说明本次结论依赖的行情源、K线周期、样本行数和缺失字段，缺数据时写明“数据不足”。",
    },
}

DEFAULT_QUICK_PROMPT_MODULES = ["trend_structure", "volume_price", "support_resistance", "risk_control"]

STRUCTURED_OUTPUT_INSTRUCTION = """
只返回一个 JSON 对象，不要 Markdown 代码围栏，不要额外文字。字段必须为：
{
  "market_overview": "行情概况，字符串",
  "trend_judgment": "趋势判断，字符串",
  "support_levels": ["支撑位及依据"],
  "resistance_levels": ["压力位及依据"],
  "volume_analysis": "成交量与量价关系，字符串",
  "indicator_analysis": "技术指标或结构分析，字符串",
  "risk_warnings": ["风险提示"],
  "action_reference": ["条件式观察或操作参考，不得使用确定性买入/卖出指令"],
  "data_basis": ["本次判断使用的数据或资料"],
  "disclaimer": "仅供分析参考，不构成投资建议。"
}
缺少依据时明确写“数据不足”，数组至少保留一项。不得承诺收益或给出确定性买卖建议。
""".strip()


def _root() -> Path:
    base = settings.kline_learning_dir
    (base / FILES_SUB).mkdir(parents=True, exist_ok=True)
    return base


def _ensure_index() -> dict[str, Any]:
    p = _root() / INDEX_NAME
    if not p.exists():
        d = {"items": []}
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        return d
    return json.loads(p.read_text(encoding="utf-8"))


def _save_index(data: dict[str, Any]) -> None:
    (_root() / INDEX_NAME).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_prompt() -> dict[str, str]:
    p = _root() / PROMPT_NAME
    if not p.exists():
        d = {
            "system": DEFAULT_SYSTEM,
            "user_template": DEFAULT_USER_TEMPLATE,
            "market_system": DEFAULT_MARKET_SYSTEM,
            "market_user_template": DEFAULT_MARKET_USER_TEMPLATE,
            "strategies": DEFAULT_STRATEGIES,
            "prompt_modules": BASIC_PROMPT_MODULES,
            "default_quick_modules": DEFAULT_QUICK_PROMPT_MODULES,
        }
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        return d
    r = json.loads(p.read_text(encoding="utf-8"))
    stored_strategies = r.get("strategies") if isinstance(r.get("strategies"), dict) else {}
    strategies = {
        key: {
            "label": str((stored_strategies.get(key) or {}).get("label") or value["label"]),
            "prompt": str((stored_strategies.get(key) or {}).get("prompt") or value["prompt"]),
        }
        for key, value in DEFAULT_STRATEGIES.items()
    }
    return {
        "system": r.get("system") or DEFAULT_SYSTEM,
        "user_template": r.get("user_template") or DEFAULT_USER_TEMPLATE,
        "market_system": r.get("market_system") or DEFAULT_MARKET_SYSTEM,
        "market_user_template": r.get("market_user_template") or DEFAULT_MARKET_USER_TEMPLATE,
        "strategies": strategies,
        "prompt_modules": BASIC_PROMPT_MODULES,
        "default_quick_modules": DEFAULT_QUICK_PROMPT_MODULES,
    }


def get_prompt() -> dict[str, str]:
    return _ensure_prompt()


def save_prompt(system: str, user_template: str) -> dict[str, str]:
    cur = _ensure_prompt()
    d = {
        "system": (system or DEFAULT_SYSTEM).strip(),
        "user_template": (user_template or DEFAULT_USER_TEMPLATE).strip(),
        "market_system": cur["market_system"],
        "market_user_template": cur["market_user_template"],
        "strategies": cur["strategies"],
    }
    (_root() / PROMPT_NAME).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def merge_prompt(
    *,
    system: str | None = None,
    user_template: str | None = None,
    market_system: str | None = None,
    market_user_template: str | None = None,
    strategies: dict[str, Any] | None = None,
) -> dict[str, str]:
    cur = _ensure_prompt()
    next_strategies = cur["strategies"]
    if strategies is not None:
        next_strategies = {
            key: {
                "label": str((strategies.get(key) or {}).get("label") or defaults["label"]).strip(),
                "prompt": str((strategies.get(key) or {}).get("prompt") or defaults["prompt"]).strip(),
            }
            for key, defaults in DEFAULT_STRATEGIES.items()
        }
    d = {
        "system": (system if system is not None else cur["system"]).strip() or DEFAULT_SYSTEM,
        "user_template": (user_template if user_template is not None else cur["user_template"]).strip()
        or DEFAULT_USER_TEMPLATE,
        "market_system": (market_system if market_system is not None else cur["market_system"]).strip()
        or DEFAULT_MARKET_SYSTEM,
        "market_user_template": (
            market_user_template if market_user_template is not None else cur["market_user_template"]
        ).strip()
        or DEFAULT_MARKET_USER_TEMPLATE,
        "strategies": next_strategies,
    }
    (_root() / PROMPT_NAME).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def list_materials() -> list[dict[str, Any]]:
    return list(_ensure_index().get("items") or [])


def _safe_name(name: str) -> str:
    b = Path(name).name
    b = re.sub(r"[^\w\u4e00-\u9fff\-. ()（）]+", "_", b)
    return b[:120] or "unnamed"


def _read_pdf(path: Path, lim: int = 50_000) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        r = PdfReader(str(path))
        out, n = [], 0
        for pg in r.pages[:40]:
            t = pg.extract_text() or ""
            out.append(t)
            n += len(t)
            if n >= lim:
                break
        return "\n".join(out)[:lim]
    except Exception:
        return ""


def extract_text(path: Path) -> str:
    e = path.suffix.lower()
    if e in (".txt", ".md", ".markdown"):
        return path.read_text(encoding="utf-8", errors="ignore")[:80_000]
    if e == ".pdf":
        return _read_pdf(path)
    return ""


def _fb_sum(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= 50 else t[:47] + "…"


def llm_short_summary(text: str) -> str | None:
    if not settings.any_llm_key_configured:
        return None
    o, _err = llm_client.chat_completion_sync(
        [
            {"role": "system", "content": "用中文写简介，30到50个汉字，不要引号。"},
            {"role": "user", "content": text[:4000]},
        ],
        temperature=0.3,
        timeout=60.0,
    )
    if not o:
        return None
    o = o.strip()
    return (o[:50] + "…") if len(o) > 52 else o


def llm_interpret(system: str, user_content: str) -> tuple[str | None, str | None]:
    if not settings.any_llm_key_configured:
        return None, "未配置 API Key（请在控制台「大模型接口」保存主密钥或备用密钥，或配置环境变量）"
    uc = user_content
    if len(uc) > 28_000:
        uc = uc[:27_500] + "\n\n…（上文已截断以适配模型上下文）"
    text, err = llm_client.chat_completion_sync(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": uc},
        ],
        temperature=0.35,
        timeout=180.0,
    )
    if err:
        return None, err
    if not (text or "").strip():
        return None, "大模型返回空内容，请检查模型名与 API Base 是否匹配，或稍后重试"
    return text.strip(), None


def _string_list(value: Any, fallback: str) -> list[str]:
    if isinstance(value, list):
        out = [str(x).strip() for x in value if str(x).strip()]
        if out:
            return out[:8]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return [fallback]


def normalize_structured_analysis(value: Any) -> dict[str, Any]:
    """收敛模型输出，确保前端与历史归档获得稳定结构。"""
    data = value if isinstance(value, dict) else {}
    return {
        "market_overview": str(data.get("market_overview") or "数据不足").strip(),
        "trend_judgment": str(data.get("trend_judgment") or "数据不足").strip(),
        "support_levels": _string_list(data.get("support_levels"), "未识别出可靠支撑位"),
        "resistance_levels": _string_list(data.get("resistance_levels"), "未识别出可靠压力位"),
        "volume_analysis": str(data.get("volume_analysis") or "数据不足").strip(),
        "indicator_analysis": str(data.get("indicator_analysis") or "数据不足").strip(),
        "risk_warnings": _string_list(data.get("risk_warnings"), "注意行情数据滞后与模型误判风险"),
        "action_reference": _string_list(data.get("action_reference"), "等待更多有效信号并结合自身风险承受能力判断"),
        "data_basis": _string_list(data.get("data_basis"), "K线与行情指标"),
        "disclaimer": "仅供分析参考，不构成投资建议。",
    }


def structured_analysis_markdown(data: dict[str, Any]) -> str:
    sections = [
        ("行情概况", data["market_overview"]),
        ("趋势判断", data["trend_judgment"]),
        ("关键支撑位", "\n".join(f"- {x}" for x in data["support_levels"])),
        ("关键压力位", "\n".join(f"- {x}" for x in data["resistance_levels"])),
        ("成交量变化", data["volume_analysis"]),
        ("技术指标", data["indicator_analysis"]),
        ("风险提示", "\n".join(f"- {x}" for x in data["risk_warnings"])),
        ("操作参考", "\n".join(f"- {x}" for x in data["action_reference"])),
    ]
    return "\n\n".join(f"## {title}\n{body}" for title, body in sections) + f"\n\n{data['disclaimer']}"


def llm_structured_interpret(system: str, user_content: str) -> tuple[dict[str, Any] | None, str | None]:
    if not settings.any_llm_key_configured:
        return None, "未配置 API Key（请在控制台「大模型接口」配置）"
    value, err = llm_client.chat_completion_json_array(
        [
            {"role": "system", "content": f"{system}\n\n{STRUCTURED_OUTPUT_INSTRUCTION}"},
            {"role": "user", "content": user_content[:27_500]},
        ],
        temperature=0.25,
        timeout=180.0,
    )
    if err:
        return None, err
    return normalize_structured_analysis(value), None


async def analyze_a_share_symbol(
    symbol: str,
    *,
    range_param: str = "1y",
    interval: str = "1d",
    candle_rows: int = 35,
    strategy: str = "comprehensive",
    prompt_modules: list[str] | None = None,
) -> dict[str, Any]:
    """
    个股 K 线 + 学习资料 + LLM 解读。供 HTTP 与盘后定时任务共用。
    失败：ValueError（多为 404 类），RuntimeError（多为 502/LLM）。
    """
    from app.services import kline_pipeline
    from app.storage import kline_bundle_cache

    sym = symbol.strip().upper()
    if not sym:
        raise ValueError("缺少代码")
    cached = kline_bundle_cache.load_bundle(sym, range_param, interval)
    if cached and (cached.get("payload") or {}).get("candles"):
        bundle = dict(cached["payload"])
        bundle["kline_source"] = "local_cache"
        bundle["cache_saved_at"] = cached.get("saved_at")
    else:
        try:
            bundle = await kline_pipeline.fetch_a_share_kline_with_fallbacks(sym, range_param, interval)
        except ValueError as e:
            raise ValueError(str(e)) from e
        except RuntimeError as e:
            raise RuntimeError(str(e)) from e
        except Exception as e:
            raise RuntimeError(f"拉取 K 线失败: {e!s}") from e

    candles = bundle.get("candles") or []
    metrics = bundle.get("metrics") or {}
    pr = get_prompt()
    strategy_key = strategy if strategy in pr["strategies"] else "comprehensive"
    strategy_meta = pr["strategies"][strategy_key]
    learning = build_learning_context(max_total=4000)
    try:
        user_msg = pr["user_template"].format(
            symbol=sym,
            range_param=range_param,
            interval=interval,
            metrics=escape_braces_for_str_format(format_metrics(metrics)),
            candles_summary=escape_braces_for_str_format(
                format_candles_summary(candles, candle_rows)
            ),
            learning_context=escape_braces_for_str_format(learning),
        )
    except KeyError as e:
        raise ValueError(f"提示词模板占位符与参数不匹配: {e!s}") from e

    user_msg += f"\n\n【本次解读策略】{strategy_meta['label']}\n{strategy_meta['prompt']}"
    selected_modules = [
        key
        for key in (prompt_modules if prompt_modules is not None else DEFAULT_QUICK_PROMPT_MODULES)
        if key in BASIC_PROMPT_MODULES
    ]
    if selected_modules:
        module_lines = [
            f"- {BASIC_PROMPT_MODULES[key]['label']}：{BASIC_PROMPT_MODULES[key]['prompt']}"
            for key in selected_modules
        ]
        user_msg += "\n\n【基础提示词模块】\n" + "\n".join(module_lines)
    if bundle.get("cache_saved_at"):
        user_msg += f"\n【数据说明】本次使用本地 K 线缓存，缓存时间：{bundle['cache_saved_at']}。请在风险提示中说明数据可能滞后。"
    analysis, err = await asyncio.to_thread(llm_structured_interpret, pr["system"], user_msg)
    if err:
        raise RuntimeError(err)
    text = structured_analysis_markdown(analysis or normalize_structured_analysis({}))
    return {
        "symbol": sym,
        "range": range_param,
        "interval": interval,
        "interpretation": text,
        "analysis": analysis,
        "strategy": strategy_key,
        "strategy_label": strategy_meta["label"],
        "prompt_modules": selected_modules,
        "model": settings.kline_llm_model,
        "kline_source": bundle.get("kline_source"),
        "tencent_mkline_slot": bundle.get("tencent_mkline_slot"),
    }


def add_material(data: bytes, original_name: str, summary_override: str | None) -> dict[str, Any]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("文件过大")
    on = _safe_name(original_name)
    ext = Path(on).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError("仅支持 txt/md/pdf")
    uid = str(uuid.uuid4())
    sn = f"{uid}_{on}"
    path = _root() / FILES_SUB / sn
    path.write_bytes(data)
    txt = extract_text(path)
    sm = (summary_override or "").strip()
    if sm and len(sm) > 80:
        sm = sm[:50] + "…"
    if not sm:
        sm = llm_short_summary(txt) or _fb_sum(txt)
    item = {
        "id": uid,
        "original_name": on,
        "stored_name": sn,
        "summary": sm,
        "size": len(data),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    d = _ensure_index()
    d["items"] = [item] + [x for x in d.get("items") or []]
    _save_index(d)
    return item


def delete_material(mid: str) -> bool:
    d = _ensure_index()
    items = d.get("items") or []
    hit = next((x for x in items if x.get("id") == mid), None)
    if not hit:
        return False
    p = _root() / FILES_SUB / hit.get("stored_name", "")
    if p.is_file():
        try:
            p.unlink()
        except OSError:
            pass
    d["items"] = [x for x in items if x.get("id") != mid]
    _save_index(d)
    return True


def escape_braces_for_str_format(s: str) -> str:
    """插入 str.format 前转义，避免学习资料/K 线文本中的 { } 触发 KeyError。"""
    return str(s).replace("{", "{{").replace("}", "}}")


def build_learning_context(max_total: int = 6000) -> str:
    parts, n = [], 0
    for it in _ensure_index().get("items") or []:
        p = _root() / FILES_SUB / (it.get("stored_name") or "")
        if not p.is_file():
            continue
        t = extract_text(p)
        if not t:
            continue
        chunk = f"——{it.get('original_name')}——\n{t}"
        if n + len(chunk) > max_total:
            chunk = chunk[: max_total - n]
        parts.append(chunk)
        n += len(chunk)
        if n >= max_total:
            break
    return "\n\n".join(parts) if parts else "（暂无学习资料。）"


def format_candles_summary(candles: list[dict[str, Any]], last_n: int = 35) -> str:
    tail = candles[-last_n:] if len(candles) > last_n else candles
    lines = []
    for c in tail:
        ts = c.get("t")
        try:
            ds = datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d") if ts else "?"
        except Exception:
            ds = "?"
        lines.append(f"{ds} O:{c.get('o')} H:{c.get('h')} L:{c.get('l')} C:{c.get('c')} V:{c.get('v')}")
    return "\n".join(lines)


def format_metrics(m: dict[str, Any]) -> str:
    if not m:
        return "—"
    ks = [
        "name",
        "latest_close",
        "change_pct",
        "limit_up",
        "limit_down",
        "volume_ratio",
        "inner_lots",
        "outer_lots",
        "pe_ttm",
        "total_market_cap_yuan",
        "latest_volume",
        "latest_turnover_rate",
        "data_source",
    ]
    return "\n".join(f"{k}: {m.get(k)}" for k in ks if m.get(k) is not None)
