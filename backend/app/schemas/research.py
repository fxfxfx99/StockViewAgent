"""Versioned, executable research rules. No Python or arbitrary function names."""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResearchExpr(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal[
        "field", "const", "volume", "return", "sma", "volatility", "rsi", "atr",
        "lag", "rolling_mean", "rolling_std", "rolling_min", "rolling_max",
        "add", "sub", "mul", "div", "abs", "neg", "rank",
        "gt", "gte", "lt", "lte", "eq", "and", "or", "not",
    ]
    args: tuple["ResearchExpr", ...] = ()
    field: Literal["o", "h", "l", "c", "v", "amount"] | None = None
    value: float | None = None
    window: int | None = Field(default=None, ge=1, le=252, strict=True)

    @model_validator(mode="after")
    def validate_shape(self) -> "ResearchExpr":
        arity = {
            "field": 0, "const": 0, "volume": 0, "atr": 0,
            "return": 1, "sma": 1, "volatility": 1, "rsi": 1, "lag": 1,
            "rolling_mean": 1, "rolling_std": 1, "rolling_min": 1, "rolling_max": 1,
            "abs": 1, "neg": 1, "rank": 1, "not": 1,
            "add": 2, "sub": 2, "mul": 2, "div": 2,
            "gt": 2, "gte": 2, "lt": 2, "lte": 2, "eq": 2,
        }
        if self.op in {"and", "or"}:
            if not 2 <= len(self.args) <= 8:
                raise ValueError("and/or require 2–8 arguments")
        elif len(self.args) != arity[self.op]:
            raise ValueError(f"{self.op} requires {arity[self.op]} arguments")
        rolling = {"return", "sma", "volatility", "rsi", "atr", "lag",
                   "rolling_mean", "rolling_std", "rolling_min", "rolling_max"}
        if (self.window is not None) != (self.op in rolling):
            raise ValueError("window is required only for past-window operators")
        if (self.field is not None) != (self.op == "field"):
            raise ValueError("field is required only for a field node")
        if (self.value is not None) != (self.op == "const"):
            raise ValueError("value is required only for a const node")
        if self.value is not None and (not math.isfinite(self.value) or abs(self.value) > 1e9):
            raise ValueError("constant must be finite and bounded")
        boolean_ops = {"gt", "gte", "lt", "lte", "eq", "and", "or", "not"}
        wants_boolean = self.op in {"and", "or", "not"}
        if any((arg.op in boolean_ops) != wants_boolean for arg in self.args):
            raise ValueError("expression argument type mismatch")
        return self


def expression_lookback(expr: ResearchExpr) -> int:
    child = max((expression_lookback(a) for a in expr.args), default=0)
    if expr.op in {"return", "lag", "rsi", "volatility", "atr"}:
        return child + int(expr.window or 0)
    if expr.op in {"sma", "rolling_mean", "rolling_std", "rolling_min", "rolling_max"}:
        return child + int(expr.window or 1) - 1
    return child


class StrategySpecV1(BaseModel):
    """Long-only equal-weight selections; close decisions execute next open.

    Entry is checked on rebalance sessions. Positions expire after holding_days;
    an explicit exit may close them sooner. Expired positions cannot re-enter on
    the same close. Weights are capped; unused allocation remains cash.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    name: str = Field(default="Research candidate", min_length=1, max_length=120)
    hypothesis: str = Field(default="", max_length=4000)
    parent_version_id: str | None = Field(default=None, max_length=120)
    sources: tuple[str, ...] = Field(default=(), max_length=30)
    applicability: str = Field(default="", max_length=2000)
    failure_conditions: tuple[str, ...] = Field(default=(), max_length=20)
    universe: tuple[str, ...] = Field(default=(), max_length=500)
    entry: ResearchExpr
    exit: ResearchExpr | None = None
    rank: ResearchExpr | None = None
    holding_days: int = Field(default=10, ge=5, le=20, strict=True)
    rebalance_days: int = Field(default=5, ge=1, le=20, strict=True)
    max_positions: int = Field(default=5, ge=1, le=30, strict=True)
    max_position_pct: float = Field(default=1.0, gt=0, le=1, allow_inf_nan=False)
    max_symbol_weight: float = Field(default=0.2, gt=0, le=1, allow_inf_nan=False)

    @field_validator("universe")
    @classmethod
    def normalize_universe(cls, symbols: tuple[str, ...]) -> tuple[str, ...]:
        import re

        out = tuple(sorted(set(s.strip().upper().replace(".SH", ".SS") for s in symbols)))
        if any(not re.fullmatch(r"\d{6}\.(SS|SZ|BJ)", s) for s in out):
            raise ValueError("universe accepts canonical A-share symbols only")
        return out

    @model_validator(mode="after")
    def validate_tree(self) -> "StrategySpecV1":
        boolean_ops = {"gt", "gte", "lt", "lte", "eq", "and", "or", "not"}
        if self.entry.op not in boolean_ops or (self.exit and self.exit.op not in boolean_ops):
            raise ValueError("entry and exit must be boolean expressions")
        if self.rank and self.rank.op in boolean_ops:
            raise ValueError("rank must be numeric")
        nodes = 0

        def visit(node: ResearchExpr, depth: int) -> None:
            nonlocal nodes
            nodes += 1
            if depth > 8 or nodes > 96:
                raise ValueError("strategy exceeds depth 8 or 96 expression nodes")
            for child in node.args:
                visit(child, depth + 1)

        for expr in (self.entry, self.exit, self.rank):
            if expr:
                visit(expr, 1)
                if expression_lookback(expr) > 756:
                    raise ValueError("combined lookback exceeds 756 sessions")
        return self
