"""Account-owned monitoring plans and explicit human review decisions."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.tracking import canonical_symbol, review_date

SHANGHAI = ZoneInfo("Asia/Shanghai")
INDEX_KEYS = {"SH_COMP", "SZ_COMP", "CYB", "HS300", "SSE50"}


def _today() -> date:
    return datetime.now(SHANGHAI).date()


def default_review_at() -> str:
    return (_today() + timedelta(days=30)).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


Channel = Literal["news", "price", "indicators", "strategy", "announcements", "forum"]


class ResearchLink(StrictModel):
    dossier_id: str = Field(min_length=1, max_length=64)
    report_id: str = Field(min_length=1, max_length=64)
    observation_index: int = Field(ge=0, le=4, strict=True)


class PlanCreate(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    target_type: Literal["stock", "stock_list", "index", "topic"]
    target_code: str = Field(default="", max_length=40)
    target_symbols: list[str] = Field(default_factory=list, max_length=20)
    channels: list[Channel] = Field(default_factory=lambda: ["news", "price"], min_length=1, max_length=6)
    research_link: ResearchLink | None = None
    keywords: list[str] = Field(default_factory=list, max_length=10)
    goal: str = Field(min_length=1, max_length=4000)
    watch_conditions: str = Field(default="", max_length=2000)
    invalidation_conditions: str = Field(default="", max_length=2000)
    interval_minutes: Literal[60, 360, 1440] = 360
    review_at: str = Field(default_factory=default_review_at)
    status: Literal["active", "paused", "closed"] = "active"

    _date = field_validator("review_at")(review_date)

    @field_validator("title", "goal")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("标题和观察目标不能为空")
        return value.strip()

    @field_validator("keywords")
    @classmethod
    def valid_keywords(cls, values):
        if any(not value.strip() or len(value.strip()) > 100 for value in values):
            raise ValueError("关键词须为1–100个字符")
        return list(dict.fromkeys(value.strip() for value in values))

    @field_validator("target_symbols")
    @classmethod
    def valid_symbols(cls, values):
        return list(dict.fromkeys(canonical_symbol(value) for value in values))

    @field_validator("channels")
    @classmethod
    def distinct_channels(cls, values):
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def valid_target(self):
        code = self.target_code.strip()
        if self.target_type == "stock_list":
            if code:
                raise ValueError("股票列表不填写单一股票或指数代码")
            if not self.target_symbols:
                raise ValueError("股票列表至少需要一只股票")
            self.target_code = ""
            return self
        if self.target_symbols:
            raise ValueError("仅股票列表观察可填写多只股票")
        if self.target_type == "stock":
            self.target_code = canonical_symbol(code)
        elif self.target_type == "index":
            if code.upper() not in INDEX_KEYS:
                raise ValueError("请从5个A股大盘指数中选择")
            self.target_code = code.upper()
        else:
            if code:
                raise ValueError("主题观察不填写股票或指数代码")
            if not self.keywords:
                raise ValueError("主题观察至少需要一个关键词")
            self.target_code = ""
        return self


class PlanChanges(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    target_type: Literal["stock", "stock_list", "index", "topic"] | None = None
    target_code: str | None = Field(default=None, max_length=40)
    target_symbols: list[str] | None = Field(default=None, max_length=20)
    channels: list[Channel] | None = Field(default=None, min_length=1, max_length=6)
    keywords: list[str] | None = Field(default=None, max_length=10)
    goal: str | None = Field(default=None, min_length=1, max_length=4000)
    watch_conditions: str | None = Field(default=None, max_length=2000)
    invalidation_conditions: str | None = Field(default=None, max_length=2000)
    interval_minutes: Literal[60, 360, 1440] | None = None
    review_at: str | None = None

    @model_validator(mode="after")
    def no_null(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("修改字段不能为null")
        return self


class PlanPatch(PlanChanges):
    revision: int = Field(ge=1, strict=True)
    status: Literal["active", "paused", "closed"] | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if self.model_fields_set == {"revision"}:
            raise ValueError("请至少修改一个字段")
        return self


class PlanReview(StrictModel):
    revision: int = Field(ge=1, strict=True)
    action: Literal["continue", "revise", "close"]
    reason: str = Field(min_length=1, max_length=4000)
    evidence_assessment: Literal["supports", "contradicts", "uncertain"]
    next_review_at: str | None = None
    changes: PlanChanges | None = None

    @model_validator(mode="after")
    def valid_action(self):
        if not self.reason.strip():
            raise ValueError("复盘理由不能为空")
        self.reason = self.reason.strip()
        if self.action == "revise" and (self.changes is None or not self.changes.model_fields_set):
            raise ValueError("修改计划时请填写修改内容")
        if self.action != "revise" and self.changes is not None:
            raise ValueError("仅修改计划时可提交changes")
        if self.action != "close":
            self.next_review_at = review_date(self.next_review_at or default_review_at())
            if date.fromisoformat(self.next_review_at) <= _today():
                raise ValueError("下次复盘日期须晚于今天")
        elif self.next_review_at is not None:
            self.next_review_at = review_date(self.next_review_at)
        return self


class ConfigPatch(StrictModel):
    enabled: bool | None = Field(default=None, strict=True)
    daily_call_limit: int | None = Field(default=None, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def valid_patch(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("请提供有效的监控配置")
        return self


class NotificationPatch(StrictModel):
    is_read: bool = Field(default=True, strict=True)


class HandlingCreate(StrictModel):
    action: Literal["reviewed", "ignored", "research"]
    reason: str = Field(min_length=1, max_length=2000)
    dossier_id: str | None = Field(default=None, min_length=1, max_length=64)
    run_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid_handling(self):
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("请填写处理理由")
        if (self.action == "research") != bool(self.dossier_id):
            raise ValueError("转入研究时须关联研究档案；其他处理无需档案")
        return self
