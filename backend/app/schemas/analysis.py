"""Strict, evidence-oriented event interpretation; scores use 0–100."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

AnalysisText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]


class RelevanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    stock_code: str = Field(min_length=1, max_length=24)
    stock_name: str = Field(max_length=120)
    relevance_score: int = Field(ge=0, le=100)
    relevance_type: Literal["direct", "industry", "supply_chain", "competitor", "macro", "weak"]
    reason: str = Field(min_length=10, max_length=3000)
    information_value: bool


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    source_id: str = Field(min_length=1, max_length=40)
    # Optional exact quote; when supplied it must occur in the referenced input.
    quote: str = Field(default="", max_length=500)


class ImpactResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    stock_code: str = Field(min_length=1, max_length=24)
    sentiment: Literal["positive", "neutral", "negative"]
    impact_direction: Literal["bullish", "neutral", "bearish"]
    impact_strength: int = Field(ge=0, le=100)
    time_horizon: Literal["intraday", "short_term", "medium_term", "long_term"]
    confidence: int = Field(ge=0, le=100)
    reasoning: str = Field(min_length=20, max_length=6000)
    event_summary: str = Field(min_length=10, max_length=600)
    event_type: Literal["earnings", "order", "policy", "industry", "corporate_action", "product", "risk", "macro", "other"]
    impact_channels: list[AnalysisText] = Field(min_length=2, max_length=6)
    realization_conditions: list[AnalysisText] = Field(min_length=1, max_length=6)
    expectation_gap: str = Field(min_length=10, max_length=1500)
    confidence_reason: str = Field(min_length=10, max_length=1500)
    counter_arguments: list[AnalysisText] = Field(min_length=1, max_length=6)
    watch_points: list[AnalysisText] = Field(min_length=1, max_length=6)
    invalidation_conditions: list[AnalysisText] = Field(min_length=1, max_length=6)
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=8)
    key_factors: list[AnalysisText] = Field(min_length=1, max_length=12)
    risk_points: list[AnalysisText] = Field(min_length=1, max_length=12)
    facts: list[AnalysisText] = Field(min_length=1, max_length=12)
    inferences: list[AnalysisText] = Field(min_length=1, max_length=12)
    uncertainties: list[AnalysisText] = Field(min_length=1, max_length=12)
