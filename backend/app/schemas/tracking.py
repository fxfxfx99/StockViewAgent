"""Small, versioned contracts for personal research notes and local evidence."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHANGHAI = ZoneInfo("Asia/Shanghai")


def today() -> date:
    return datetime.now(SHANGHAI).date()


def default_review_at() -> str:
    return (today() + timedelta(days=30)).isoformat()


def review_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("复盘日期须为 YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def canonical_symbol(value: str) -> str:
    value = value.strip().upper().replace(".SH", ".SS")
    if not re.fullmatch(r"\d{6}\.(SS|SZ|BJ)", value):
        raise ValueError("仅支持六位代码.SS/SZ/BJ 的 A 股标的")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceRef(StrictModel):
    kind: Literal["news", "calendar", "strategy"]
    id: str = Field(min_length=1, max_length=200)
    revision_id: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2000)
    published_at: str | float | None = None
    observed_at: str | float | None = None
    excerpt: str | None = Field(default=None, max_length=4000)
    snapshot: dict[str, Any] | None = None

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("证据链接须为公开 HTTP(S) 链接")
        return value

    @field_validator("snapshot")
    @classmethod
    def bounded_snapshot(cls, value):
        if value is not None:
            try:
                encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            except (ValueError, TypeError, RecursionError) as exc:
                raise ValueError("观点快照须为有效 JSON") from exc
            if len(encoded.encode()) > 32768:
                raise ValueError("观点快照不能超过 32 KiB")
        return value


class ThesisCreate(StrictModel):
    symbol: str = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=4000)
    watch_conditions: str = Field(default="", max_length=4000)
    invalidation_conditions: str = Field(default="", max_length=4000)
    review_at: str = Field(default_factory=default_review_at)
    signal_id: str | None = Field(default=None, min_length=1, max_length=200)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=30)

    _symbol = field_validator("symbol")(canonical_symbol)
    _date = field_validator("review_at")(review_date)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("判断理由不能为空")
        return value.strip()


class ThesisChanges(StrictModel):
    reason: str | None = Field(default=None, min_length=1, max_length=4000)
    watch_conditions: str | None = Field(default=None, max_length=4000)
    invalidation_conditions: str | None = Field(default=None, max_length=4000)
    evidence_refs: list[EvidenceRef] | None = Field(default=None, max_length=30)

    @model_validator(mode="after")
    def no_null_updates(self):
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("修改字段不能为 null")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("判断理由不能为空")
        return self


class ThesisPatch(ThesisChanges):
    revision: int = Field(ge=1, strict=True)
    review_at: str | None = None
    status: Literal["active", "closed"] | None = None

    @field_validator("review_at")
    @classmethod
    def valid_date(cls, value):
        return review_date(value) if value is not None else value

    @model_validator(mode="after")
    def nonempty_patch(self):
        if self.model_fields_set == {"revision"}:
            raise ValueError("请至少修改一个字段")
        return self


class ThesisReview(StrictModel):
    revision: int = Field(ge=1, strict=True)
    action: Literal["continue", "revise", "close"]
    reason: str = Field(min_length=1, max_length=4000)
    evidence_assessment: Literal["supports", "contradicts", "uncertain"]
    review_at: str | None = None
    changes: ThesisChanges | None = None

    @field_validator("review_at")
    @classmethod
    def valid_date(cls, value):
        return review_date(value) if value is not None else value

    @model_validator(mode="after")
    def valid_action(self):
        if not self.reason.strip():
            raise ValueError("复盘理由不能为空")
        if self.action == "revise" and (self.changes is None or not self.changes.model_fields_set):
            raise ValueError("修改判断时请填写修改内容")
        if self.action != "revise" and self.changes is not None:
            raise ValueError("仅修改判断时可提交 changes")
        if self.action != "close":
            self.review_at = self.review_at or default_review_at()
            if date.fromisoformat(self.review_at) <= today():
                raise ValueError("下一次复盘日期须晚于今天")
        return self


class ChangePatch(StrictModel):
    is_read: bool | None = Field(default=None, strict=True)
    feedback: Literal["useful", "irrelevant"] | None = None

    @model_validator(mode="after")
    def nonempty_patch(self):
        if not self.model_fields_set:
            raise ValueError("请提供已读状态或反馈")
        if "is_read" in self.model_fields_set and self.is_read is None:
            raise ValueError("is_read 不能为 null")
        return self
