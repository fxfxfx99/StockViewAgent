"""Per-user strategy workspaces and deterministic, date-aware local retrieval.

Retrieval never downloads or seeds sources. Shared material is read from the
existing knowledge database; private notes and settings retain every revision.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from app.config import settings
from app.storage import strategy_workspace_store as store

_MARKET_TZ = ZoneInfo("Asia/Shanghai")


class RAGConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool = True
    top_k: int = Field(default=5, ge=1, le=20, strict=True)
    max_age_days: int | None = Field(default=None, ge=1, le=3650, strict=True)
    include_builtin: StrictBool = True
    prefer_recent: StrictBool = True


class KnowledgeDocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=20000)
    source_url: str = Field(default="", max_length=2048)
    published_at: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("source_url")
    @classmethod
    def source_url_is_web(cls, value: str) -> str:
        if not value:
            return value
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.netloc or parts.username or parts.password:
            raise ValueError("来源链接须为不含登录信息的 http/https URL")
        return value

    @field_validator("published_at")
    @classmethod
    def publication_is_date(cls, value: str | None) -> str | None:
        return _validate_day(value) if value else None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, values: list[str]) -> list[str]:
        tags = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(len(value) > 40 for value in tags):
            raise ValueError("每个标签最多 40 个字符")
        return tags


class RetrievalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(default="", max_length=2000)
    as_of: str | None = None

    @field_validator("as_of")
    @classmethod
    def as_of_is_date(cls, value: str | None) -> str | None:
        return _validate_day(value) if value else None


def _validate_day(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("日期须为 YYYY-MM-DD") from None
    return value


# Authored descriptions explain the existing rule-based lenses, rather than
# claiming current market facts or proven profitability.
_PROFILES: dict[str, dict[str, Any]] = {
    "trend_following_mid": {
        "lens": "从价格趋势已经形成、市场正在确认的右侧视角，观察趋势延续与破坏。",
        "principles": ["以中期均线、价格结构和相对强弱确认方向", "量价配合后再评估跟随条件", "趋势破坏时重新评估，不以预测底部为前提"],
        "suitable_for": ["方向明确且成交活跃的行情", "能容忍回撤并按周复核的持有周期"],
        "not_suitable_for": ["反复假突破的震荡区间", "单日急拉后无成交跟进"],
        "evidence_focus": ["20/60 日均线结构", "突破与成交量", "回撤、波动和行业相对强弱"],
        "update_policy": "随最新交易日重算趋势；新增反转或假突破复盘后修订适用条件。",
    },
    "short_term_trading": {
        "lens": "从短周期供需、动量和执行成本的视角，观察几天内的交易机会。",
        "principles": ["把短期动量与成交活跃度一起判断", "事先设定失效条件与持有窗口", "计入滑点、涨跌停和交易制度约束"],
        "suitable_for": ["流动性充足且信号明确的短周期行情", "能及时观察成交和风险变化"],
        "not_suitable_for": ["流动性不足或连续一字板", "无法执行预设退出条件"],
        "evidence_focus": ["近 1—5 日价格动量", "成交量与换手", "事件时间和短期波动"],
        "update_policy": "按最新交易日复核动量与执行条件；新事件和交易制度变化进入知识库。",
    },
    "swing_trading": {
        "lens": "从一段行情内部的回撤、支撑与恢复视角，寻找相对完整的价格波段。",
        "principles": ["区分主趋势与局部回撤", "同时检查支撑、成交和反弹力度", "用波段结构失效作为复核条件"],
        "suitable_for": ["趋势或区间边界相对清楚", "数周内有可验证的价格结构"],
        "not_suitable_for": ["结构持续跳空失真", "仅凭单个超买超卖指标入场"],
        "evidence_focus": ["阶段高低点和支撑阻力", "回撤幅度", "成交缩放和趋势恢复"],
        "update_policy": "每个交易日更新波段边界；新高、新低或结构失效后重新解释观点。",
    },
    "mean_reversion": {
        "lens": "从价格相对正常区间的偏离视角，判断修复可能性及均值本身是否失效。",
        "principles": ["先验证价格区间相对稳定", "区分短暂偏离与基本面永久变化", "极端偏离仍需反向风险边界"],
        "suitable_for": ["均值与波动结构相对稳定", "无重大基本面断裂的区间行情"],
        "not_suitable_for": ["单边趋势加速", "业绩、制度或流动性发生结构性变化"],
        "evidence_focus": ["价格相对均线的偏离", "历史波动区间", "新公告和趋势破坏证据"],
        "update_policy": "新价格更新偏离幅度；发现结构变化时暂停使用旧均值并记录反例。",
    },
    "value_investing": {
        "lens": "从企业长期现金创造能力和购买价格的视角，研究价值与安全边际。",
        "principles": ["估值必须和盈利质量、负债一起看", "使用可持续现金流而非一次性利润", "记录价值判断的关键假设及反证"],
        "suitable_for": ["经营可理解、财务可核验", "能够持续跟踪多个财报周期"],
        "not_suitable_for": ["仅凭低市盈率判断便宜", "盈利周期、资产减值或负债风险无法评估"],
        "evidence_focus": ["现金流与利润质量", "估值与行业历史区间", "资产负债及资本回报"],
        "update_policy": "随新财报、资本配置和经营证据修订价值假设；保留旧版本以比较判断变化。",
    },
    "quality_growth": {
        "lens": "从盈利增长的质量、持续性和竞争优势视角，评估成长是否能够兑现。",
        "principles": ["增长与现金回收、资本效率互相验证", "区分行业景气与企业自身优势", "把估值隐含预期作为成长约束"],
        "suitable_for": ["收入和盈利质量可以连续核验", "商业模式、竞争力与资本效率较清晰"],
        "not_suitable_for": ["只看增长率不看基数和现金流", "增长依赖高负债或不可持续补贴"],
        "evidence_focus": ["收入、利润与经营现金流", "ROE/资本回报", "竞争壁垒和估值预期"],
        "update_policy": "随财报、订单和竞争变化更新成长假设；兑现不足时降低论据强度。",
    },
    "dividend_yield": {
        "lens": "从持续现金分配和资本保护视角，判断股息收益的可持续性。",
        "principles": ["股息率要与现金流和派息能力一起看", "识别一次性分红与周期高点", "检查负债、资本开支和分红政策"],
        "suitable_for": ["现金流稳定且分红政策可核验", "偏重长期现金回报的研究目标"],
        "not_suitable_for": ["股价下跌造成的表面高股息", "高派息伴随现金流不足或负债攀升"],
        "evidence_focus": ["分红公告和派息率", "自由现金流覆盖", "利率环境与负债结构"],
        "update_policy": "分红方案、财报或资本开支变化后复核覆盖能力；除权信息按实际日期更新。",
    },
    "event_driven": {
        "lens": "从可核验事件、市场预期差与兑现时间窗视角，评估事件对价格的影响。",
        "principles": ["以正式披露确认事件与发生时间", "区分事件本身和已反映的市场预期", "设置延期、取消和兑现不及预期的情景"],
        "suitable_for": ["事件路径、时间与条件较清晰", "有原始公告可追踪验证"],
        "not_suitable_for": ["仅有传闻而无披露", "无法评估事件失败或延期风险"],
        "evidence_focus": ["公告原文与发布日期", "事件进度和触发条件", "价格反应与预期差"],
        "update_policy": "新公告、延期和结果披露到来时修订事件阶段；过期催化退出有效证据。",
    },
    "flow_confluence": {
        "lens": "从多周期资金与价格共同确认的视角，观察买卖力量是否持续一致。",
        "principles": ["核对资金口径后比较多日方向", "资金流必须与价格和成交相互验证", "大单分类只作代理，不等于识别真实机构"],
        "suitable_for": ["资金数据连续且口径一致", "多日量价与资金方向能够互证"],
        "not_suitable_for": ["将单日大单净流入等同确定性利好", "跨来源口径无法对齐"],
        "evidence_focus": ["近 5 日资金累计", "量价结构", "大单口径、数据日期与连续性"],
        "update_policy": "更新交易日资金序列并复核口径；出现价格背离或数据修订时调整解读。",
    },
    "sector_rotation": {
        "lens": "从行业之间相对收益、景气和资金迁移的视角，观察配置方向的变化。",
        "principles": ["比较行业相对强弱而非只看个股涨跌", "景气、估值与资金相互验证", "区分短期反弹和持续配置迁移"],
        "suitable_for": ["行业数据可比且阶段差异清楚", "希望研究中期行业配置"],
        "not_suitable_for": ["只有个股数据却推断整个行业", "行业分类或指数样本频繁变更而未校正"],
        "evidence_focus": ["行业相对收益与广度", "景气和盈利修订", "行业资金与估值分位"],
        "update_policy": "交易日更新行业强弱；财报、景气指标或政策变化后复核轮动阶段。",
    },
    "thematic_growth": {
        "lens": "从技术变革、产业渗透和商业化进程的视角，观察主题能否转化为实际增长。",
        "principles": ["把主题叙事拆成可核验的产业环节", "用订单、收入和盈利验证商业化", "同时观察拥挤程度与估值隐含预期"],
        "suitable_for": ["产业趋势存在可追踪数据", "公司受益路径和业务占比可验证"],
        "not_suitable_for": ["只凭概念名称判断公司受益", "技术采用与业绩兑现之间缺少证据"],
        "evidence_focus": ["产业渗透率与需求", "产品、订单和收入贡献", "政策催化、竞争格局和估值"],
        "update_policy": "随技术、政策和订单变化更新主题假设；保留证伪记录并淘汰过期叙事。",
    },
    "mutual_fund_crowding": {
        "lens": "从公募持仓集中度、共同持股和赎回压力的视角，分析机构共识与拥挤风险。",
        "principles": ["区分披露持仓日期与当前真实持仓", "持仓集中和新增配置分别判断", "将赎回、估值和流动性纳入反向情景"],
        "suitable_for": ["存在可核验的基金持仓披露", "研究数月内机构共识变化"],
        "not_suitable_for": ["把滞后季报当成实时买卖信号", "用股价上涨反推公募增持"],
        "evidence_focus": ["基金持仓披露日期和集中度", "机构持股变化", "估值、流动性与赎回风险"],
        "update_policy": "随新基金报告更新已披露持仓；明确数据滞后，不能用后续披露回填当时判断。",
    },
    "national_team_policy": {
        "lens": "从政策目标、公开持股和市场稳定机制视角，辨别政策资金的可核验影响。",
        "principles": ["以正式政策和持股披露作为直接证据", "区分政策支持与个股投资回报", "市场价格与资金代理不能识别资金身份"],
        "suitable_for": ["政策文件、ETF 或股东披露可验证", "研究政策与中长期配置的关系"],
        "not_suitable_for": ["将大单或拉升直接认定为国家队", "认为政策支持可以排除估值与经营风险"],
        "evidence_focus": ["正式政策与公告日期", "公开股东和 ETF 披露", "政策目标与公司实际业务关系"],
        "update_policy": "新政策、持股或执行进展披露后修订观点；区分确认事实和资金身份推测。",
    },
    "hot_money_dragon_tiger": {
        "lens": "从短线情绪周期、题材龙头地位和公开席位行为视角，观察高波动交易结构。",
        "principles": ["先判断情绪阶段与题材强度", "龙虎榜席位只代表披露交易行为", "将连板、断板和流动性约束作为重要边界"],
        "suitable_for": ["龙虎榜和交易披露可核验", "能够密切跟踪高波动与执行风险"],
        "not_suitable_for": ["把席位标签等同特定操盘者身份", "无法承担涨跌停导致的退出困难"],
        "evidence_focus": ["龙虎榜披露和买卖席位", "题材广度与龙头相对强弱", "成交、换手和连板结构"],
        "update_policy": "每个交易日复核情绪和公开席位；题材退潮或龙头切换时重建观察结论。",
    },
    "quant_liquidity": {
        "lens": "从成交、换手、波动和交易摩擦的微观结构视角，判断流动性条件。",
        "principles": ["先检查可交易性和成交容量", "区分统计特征与真实订单行为", "缺少盘口数据时明确使用日频代理"],
        "suitable_for": ["成交与波动数据连续可比", "研究交易执行和短周期结构"],
        "not_suitable_for": ["用日线数据声称识别高频策略", "忽视费用、冲击成本和成交容量"],
        "evidence_focus": ["成交额、换手和波动", "价差与盘口数据（如有）", "滑点、容量和数据采样口径"],
        "update_policy": "按可用数据频率重算流动性；新交易制度、采样口径或异常成交出现后复核。",
    },
    "northbound_institution": {
        "lens": "从跨境机构配置、风险偏好与公开持股视角，观察外资相关配置线索。",
        "principles": ["区分市场资金流与个股持仓变化", "确认公开数据口径和披露时点", "把汇率、海外环境和指数调整作为解释变量"],
        "suitable_for": ["有可核验的跨境或持股披露", "研究数周至数月的机构配置线索"],
        "not_suitable_for": ["用市场净流入证明某个股被增持", "将缺失或停止披露数据当成实时信号"],
        "evidence_focus": ["持股及资金披露口径", "汇率与海外风险环境", "指数变更及行业配置"],
        "update_policy": "随可用披露与数据制度变化更新；明确缺失范围，避免用旧口径推断实时行为。",
    },
}


def get_profile(strategy_id: str) -> dict[str, Any]:
    # Kept lazy because transaction_agent_service calls retrieval during scoring.
    from app.services.transaction_agent_service import STRATEGIES

    strategy = next((item for item in STRATEGIES if strategy_id in {item["id"], item["name"]}), None)
    if strategy is None:
        raise KeyError("策略不存在")
    return {**strategy, **_PROFILES[strategy["id"]], "version": 1}


def _config(user_id: int | None, strategy_id: str, *, before: str | None = None) -> dict[str, Any]:
    return store.get_config(user_id, strategy_id, before=before) or {
        **RAGConfigInput().model_dump(), "version": 0, "updated_at": None,
    }


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


def _methodology(profile: dict[str, Any]) -> dict[str, Any]:
    content = "\n".join([profile["lens"], *profile["principles"], *profile["evidence_focus"], *profile["not_suitable_for"]])
    return {
        "id": f"methodology:{profile['id']}", "strategy_id": profile["id"],
        "title": f"{profile['name']} · 策略方法与适用边界", "content": content,
        "summary": profile["lens"], "key_points": profile["principles"], "risks": profile["not_suitable_for"],
        "source_url": "", "url": "", "published_at": None, "created_at": None, "updated_at": None,
        "version": profile["version"], "tags": [profile["name"], profile["style"]],
        "origin": "methodology", "source_type": "methodology", "publisher": "StockViewAgent",
        "timeless": True,
    }


def _builtin_documents(profile: dict[str, Any]) -> list[dict[str, Any]]:
    documents = [_methodology(profile)]
    path = settings.data_dir / "strategy_knowledge.db"
    if not path.exists():
        return documents
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT ki.*, ks.publisher, ks.source_type
            FROM knowledge_items ki JOIN knowledge_sources ks ON ks.id=ki.source_id
            WHERE ki.strategy=? ORDER BY ki.id
        """, (profile["name"],)).fetchall()
    except sqlite3.OperationalError:
        # An uninitialized shared catalog does not prevent private workspaces.
        return documents
    finally:
        conn.close()
    for raw in rows:
        row = dict(raw)
        points = json.loads(row.get("key_points_json") or "[]")
        risks = json.loads(row.get("risks_json") or "[]")
        content = "\n".join(filter(None, [row.get("summary"), row.get("content_excerpt"), *points, *risks]))
        documents.append({
            "id": f"builtin:{row['id']}", "strategy_id": profile["id"], "title": row["title"],
            "content": content, "summary": row.get("summary") or "", "key_points": points, "risks": risks,
            "source_url": row.get("url") or "", "url": row.get("url") or "", "published_at": None,
            "created_at": row.get("collected_at"), "updated_at": row.get("updated_at"),
            "version": _hash([content, row.get("url")]), "tags": [profile["name"]],
            "origin": "builtin", "source_type": row.get("source_type"), "publisher": row.get("publisher"),
            "source_id": row.get("source_id"), "timeless": False,
        })
    return documents


def get_workspace(user_id: int, strategy_id: str) -> dict[str, Any]:
    profile = get_profile(strategy_id)
    sid = profile["id"]
    docs = store.list_documents(user_id, sid)
    builtins = _builtin_documents(profile)
    config = _config(user_id, sid)
    version = _hash({"strategy_id": sid, "documents": docs, "builtin_documents": builtins, "config": config})
    timestamps = [item["updated_at"] for item in [*docs, *builtins, config] if item.get("updated_at")]
    return {
        "profile": profile, "rag_config": config, "documents": docs, "builtin_documents": builtins,
        "knowledge_version": version,
        "stats": {"document_count": len(docs), "builtin_count": len(builtins), "knowledge_version": version,
                  "last_updated_at": max(timestamps, default=None)},
    }


def save_config(user_id: int, strategy_id: str, data: dict[str, Any]) -> dict[str, Any]:
    profile = get_profile(strategy_id)
    validated = RAGConfigInput.model_validate(data).model_dump()
    store.save_config(user_id, profile["id"], validated)
    return get_workspace(user_id, profile["id"])


def save_document(user_id: int, strategy_id: str, data: dict[str, Any], *, document_id: str | None = None) -> dict[str, Any]:
    profile = get_profile(strategy_id)
    validated = KnowledgeDocumentInput.model_validate(data).model_dump()
    store.write_document(user_id, profile["id"], validated, document_id=document_id)
    return get_workspace(user_id, profile["id"])


def delete_document(user_id: int, strategy_id: str, document_id: str) -> dict[str, Any]:
    profile = get_profile(strategy_id)
    store.delete_document(user_id, profile["id"], document_id)
    return get_workspace(user_id, profile["id"])


def _timestamp(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        result = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except ValueError:
        return None


def _tokens(text: str) -> set[str]:
    # Latin words / symbols plus Chinese bigrams avoid a dependency on remote
    # embeddings while retaining explicit, inspectable relevance matches.
    text = text.casefold()
    terms = set(re.findall(r"[a-z0-9][a-z0-9._-]*", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        terms.update(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) <= 8:
            terms.add(run)
    return terms


def retrieve_strategy_knowledge(
    user_id: int | None, strategy_id: str, *, query: str = "", as_of: str | None = None,
) -> dict[str, Any]:
    profile = get_profile(strategy_id)
    sid = profile["id"]
    request = RetrievalInput(query=query, as_of=as_of)
    day = request.as_of
    cutoff = (datetime.combine(date.fromisoformat(day) + timedelta(days=1), time.min, _MARKET_TZ).astimezone(timezone.utc)
              if day else datetime.now(timezone.utc))
    before = cutoff.isoformat() if day else None
    config = _config(user_id, sid, before=before)
    private = store.list_documents(user_id, sid, before=before)
    builtins = _builtin_documents(profile) if config["include_builtin"] else []
    candidates = [*private, *builtins]
    query_terms = _tokens(request.query)
    stale_count = 0
    excluded_after_cutoff_count = 0
    eligible = []
    for document in candidates:
        doc = dict(document)
        observed = [_timestamp(doc.get(field)) for field in ("created_at", "updated_at")]
        observed = [value for value in observed if value is not None]
        published = (datetime.combine(date.fromisoformat(doc["published_at"]), time.min, _MARKET_TZ).astimezone(timezone.utc)
                     if doc.get("published_at") else None)
        known_at = max([*observed, *([published] if published else [])], default=None)
        if not doc.get("timeless") and (known_at is None or known_at >= cutoff):
            excluded_after_cutoff_count += 1
            continue
        # A newly imported old article retains its source date for freshness;
        # saved/edited time still gates when its contents were actually known.
        fresh_at = published or _timestamp(doc.get("updated_at"))
        age_days = max(0, (cutoff - fresh_at).days) if fresh_at else None
        if config["max_age_days"] is not None and age_days is not None and age_days > config["max_age_days"]:
            stale_count += 1
            continue
        matched = sorted(query_terms & _tokens(" ".join([doc["title"], doc["content"], *doc.get("tags", [])])))
        doc.update(matched_terms=matched, score=len(matched), age_days=age_days,
                   available_at=known_at.isoformat() if known_at else None,
                   freshness_at=fresh_at.isoformat() if fresh_at else None)
        doc.setdefault("summary", doc["content"][:400])
        doc.setdefault("key_points", [line.strip(" -•") for line in doc["content"].splitlines() if line.strip()][:6])
        doc.setdefault("risks", [])
        doc.setdefault("url", doc.get("source_url", ""))
        eligible.append(doc)
    # Zero-match entries are never silently represented as relevant evidence.
    relevant = [doc for doc in eligible if not query_terms or doc["matched_terms"]]
    relevant.sort(key=lambda doc: (
        -doc["score"],
        -(_timestamp(doc.get("freshness_at")).timestamp() if config["prefer_recent"] and doc.get("freshness_at") else 0),
        str(doc["id"]),
    ))
    selected = relevant[:config["top_k"]] if config["enabled"] else []
    # Fingerprint describes the eligible knowledge/config snapshot, and is
    # independent of wall-clock retrieval time or lexical ranking annotations.
    fingerprint_docs = [{key: value for key, value in doc.items() if key not in {"score", "matched_terms", "age_days"}}
                        for doc in eligible]
    version = _hash({"strategy_id": sid, "documents": fingerprint_docs, "config": config})
    timestamps = [doc.get("updated_at") for doc in eligible if doc.get("updated_at")]
    limitations = ["本地关键词检索；资料用于解释与风险提示，未经回测验证不改变规则分数。"]
    if day:
        limitations.append("个人笔记按当时已保存的版本回放；共享资料无历史版本时排除晚于节点的快照。")
    if any(doc["origin"] == "methodology" for doc in selected):
        limitations.append("策略方法是通用分析框架，不是该历史日期的市场事实。")
    return {
        "documents": selected, "config": config,
        "metadata": {
            "strategy_id": sid, "as_of": day, "query": request.query,
            "method": "local_keyword", "knowledge_version": version, "version": version,
            "config_version": config["version"], "enabled": config["enabled"],
            "candidate_count": len(candidates), "eligible_count": len(eligible), "relevant_count": len(relevant),
            "retrieved_count": len(selected), "selected_count": len(selected), "stale_count": stale_count,
            "excluded_after_cutoff_count": excluded_after_cutoff_count,
            "last_updated_at": max(timestamps, default=None),
            "retrieved_at": datetime.now(timezone.utc).isoformat(), "limitations": limitations,
        },
    }
