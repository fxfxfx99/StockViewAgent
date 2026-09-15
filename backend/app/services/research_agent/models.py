"""投研 Agent 结构化模型（与前端/导出共享）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ResearchQuery(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000)


class ResearchPlan(BaseModel):
    primary_ticker: str = ""
    secondary_ticker: str | None = None
    company_name: str = ""
    intent: str = ""
    tasks: list[str] = Field(default_factory=list)
    time_horizon: str = "medium"
    comparison_mode: bool = False
    language: str = "zh"
    data_sources_planned: list[str] = Field(default_factory=list)


class Citation(BaseModel):
    id: str
    title: str
    source_type: Literal["news", "filing", "market", "profile", "financial", "llm_inference", "other"] = "other"
    time: str | None = None
    url: str | None = None
    source_label: str = ""
    excerpt: str = ""


class AgentRunLog(BaseModel):
    steps: list[dict[str, Any]] = Field(default_factory=list)
    data_sources_used: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0


class SummaryCard(BaseModel):
    company_name: str = ""
    ticker: str = ""
    price: str | None = None
    currency: str | None = None
    change_1m_pct: float | None = None
    stance: Literal["偏积极", "中性", "偏谨慎"] = "中性"
    confidence: Literal["低", "中", "高"] = "中"
    updated_at: str = ""


class OnePager(BaseModel):
    one_line: str = ""
    core_logic: list[str] = Field(default_factory=list)
    main_risks: list[str] = Field(default_factory=list)
    watch_signals: list[str] = Field(default_factory=list)


class DeepDive(BaseModel):
    price_action: str = ""
    fundamentals: str = ""
    filings_notes: str = ""
    news_events: str = ""
    valuation: str = ""
    risk_list: str = ""
    bull_bear: str = ""


class ResearchReport(BaseModel):
    summary_card: SummaryCard = Field(default_factory=SummaryCard)
    one_pager: OnePager = Field(default_factory=OnePager)
    deep_dive: DeepDive = Field(default_factory=DeepDive)
    citations: list[Citation] = Field(default_factory=list)
    disclaimer: str = ""
    markdown_memo: str = ""
    raw_llm_used: bool = False


class ResearchRunRecord(BaseModel):
    id: str
    user_id: int
    query: str
    plan: ResearchPlan
    report: ResearchReport
    log: AgentRunLog
    created_at: float
    resolved: dict[str, Any] | None = None
