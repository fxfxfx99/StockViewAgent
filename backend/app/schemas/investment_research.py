"""Personal investment research inputs, separate from the quantitative lab."""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.tracking import canonical_symbol


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DossierCreate(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    target_type: Literal["stock", "stock_list", "topic"]
    target_code: str = Field(default="", max_length=40)
    target_symbols: list[str] = Field(default_factory=list, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=10)
    objective: str = Field(min_length=1, max_length=4000)
    thesis: str = Field(default="", max_length=4000)
    status: Literal["active", "archived"] = "active"
    monitor_run_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("title", "objective")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("标题和研究问题不能为空")
        return value.strip()

    @field_validator("target_symbols")
    @classmethod
    def symbols(cls, values):
        return list(dict.fromkeys(canonical_symbol(value) for value in values))

    @field_validator("keywords")
    @classmethod
    def terms(cls, values):
        if any(not value.strip() or len(value.strip()) > 100 for value in values):
            raise ValueError("关键词须为1–100个字符")
        return list(dict.fromkeys(value.strip() for value in values))

    @model_validator(mode="after")
    def target(self):
        if self.target_type == "stock":
            self.target_code = canonical_symbol(self.target_code)
            if self.target_symbols:
                raise ValueError("个股研究不填写股票列表")
        elif self.target_type == "stock_list":
            if self.target_code.strip() or not self.target_symbols:
                raise ValueError("股票列表须包含1–20只股票，单一代码留空")
            self.target_code = ""
        else:
            if self.target_code.strip() or self.target_symbols or not self.keywords:
                raise ValueError("主题研究须填写关键词，股票代码留空")
            self.target_code = ""
        return self


class DossierPatch(StrictModel):
    revision: int = Field(ge=1, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    target_type: Literal["stock", "stock_list", "topic"] | None = None
    target_code: str | None = Field(default=None, max_length=40)
    target_symbols: list[str] | None = Field(default=None, max_length=20)
    keywords: list[str] | None = Field(default=None, max_length=10)
    objective: str | None = Field(default=None, min_length=1, max_length=4000)
    thesis: str | None = Field(default=None, max_length=4000)
    status: Literal["active", "archived"] | None = None

    @model_validator(mode="after")
    def patch(self):
        if self.model_fields_set == {"revision"} or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("请提供有效的修改字段")
        return self


class QuestionCreate(StrictModel):
    text: str = Field(min_length=1, max_length=1000)
    status: Literal["open", "answered", "parked"] = "open"
    answer: str = Field(default="", max_length=4000)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("问题不能为空")
        return value.strip()


class QuestionPatch(StrictModel):
    revision: int = Field(ge=1, strict=True)
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    status: Literal["open", "answered", "parked"] | None = None
    answer: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def patch(self):
        if self.model_fields_set == {"revision"} or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("请提供有效的修改字段")
        return self


class MaterialCreate(StrictModel):
    kind: Literal["note", "text", "monitoring"] = "note"
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(default="", max_length=50000)
    source_url: str = Field(default="", max_length=2000)
    published_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    monitor_run_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("source_url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip()
        if value:
            parts = urlsplit(value)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or any(ord(c) < 32 for c in value):
                raise ValueError("来源链接须为无账号密码的HTTP(S)地址")
        return value

    @model_validator(mode="after")
    def contents(self):
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("材料标题不能为空")
        if self.kind == "monitoring":
            if not self.monitor_run_id or self.text or self.source_url or self.published_at is not None:
                raise ValueError("监控材料只接受任务标识，内容由后台核验导入")
        elif not self.text.strip() or self.monitor_run_id:
            raise ValueError("请填写材料正文；监控任务仅用于监控材料")
        return self


class GenerateBody(StrictModel):
    focus: str = Field(default="", max_length=2000)


class AdoptBody(StrictModel):
    revision: int = Field(ge=1, strict=True)


class ReadBody(StrictModel):
    is_read: bool = Field(default=True, strict=True)
