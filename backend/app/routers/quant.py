from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.deps.auth import get_current_user
from app.services import quant_backtest
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/quant", tags=["quant"])


class BacktestRequest(BaseModel):
    symbol: str = Field(..., description="A 股代码，如 600519.SS")
    range: str = Field(default="2y", description="K 线区间")
    interval: Literal["1d"] = Field(default="1d", description="可信回测仅支持日线")
    strategy_id: str = Field(default="ma_cross")
    params: dict[str, Any] = Field(default_factory=dict)
    initial_cash: float = Field(default=100000.0, gt=0)
    max_position_pct: float = Field(default=1.0, ge=0, le=1)
    t_plus_one: bool = True
    cost_model: dict[str, Any] | None = None


class PortfolioBacktestRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, description="股票池，至少 2 只")
    range: str = Field(default="2y", description="K 线区间")
    interval: Literal["1d"] = Field(default="1d", description="可信组合回测仅支持日线")
    params: dict[str, Any] = Field(default_factory=dict)
    initial_cash: float = Field(default=100000.0, gt=0)
    max_position_pct: float = Field(default=1.0, ge=0, le=1)
    t_plus_one: bool = True
    cost_model: dict[str, Any] | None = None


class DraftRequest(BaseModel):
    goal: str = Field(..., min_length=2, max_length=2000)
    symbol: str = ""


class OptimizeRequest(BacktestRequest):
    param_grid: dict[str, list[int | float]] = Field(default_factory=dict)
    train_ratio: float = Field(default=0.7, ge=0.55, le=0.85)
    objective: str = Field(default="sharpe", pattern="^(sharpe|return)$")


@router.get("/catalog")
def quant_catalog(_: Annotated[UserRecord, Depends(get_current_user)]):
    return {
        "strategies": quant_backtest.STRATEGY_CATALOG,
        "portfolio_strategies": quant_backtest.PORTFOLIO_STRATEGY_CATALOG,
        "joinquant_model": quant_backtest.JOINQUANT_MODEL,
        "engine": {
            "name": "StockViewAgent Quant Lab",
            "version": quant_backtest.ENGINE_VERSION,
            "mode": "template_safe_backtest_and_factor_rotation",
            "granularity": "daily_bar",
            "supports": ["按交易日T+1", "板块申报数量", "按历史日期计算税费", "完整费用预算", "下一日开盘撮合", "多股票因子TopN轮动", "LLM策略草案", "训练/验证/独立留出", "成本压力测试", "数据质量晋级门禁"],
            "limits": ["仅日线", "当前不执行任意用户 Python", "无真实盘口排队", "未知交易元数据或公司行动禁止自动晋级", "现金股息按税前除权日预记，仅限探索", "独立留出与自主研究共享每账户一次的访问门禁", "组合回测当前为等权多头轮动"],
        },
    }


@router.post("/backtest")
async def quant_backtest_run(
    body: BacktestRequest,
    _: Annotated[UserRecord, Depends(get_current_user)],
):
    try:
        return await quant_backtest.backtest_from_market(
            symbol=body.symbol,
            range_param=body.range,
            interval=body.interval,
            strategy_id=body.strategy_id,
            params=body.params,
            initial_cash=body.initial_cash,
            cost_model=body.cost_model,
            max_position_pct=body.max_position_pct,
            t_plus_one=body.t_plus_one,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"回测失败: {exc!s}") from exc


@router.post("/portfolio-backtest")
async def quant_portfolio_backtest_run(
    body: PortfolioBacktestRequest,
    _: Annotated[UserRecord, Depends(get_current_user)],
):
    try:
        return await quant_backtest.portfolio_factor_backtest_from_market(
            symbols=body.symbols,
            range_param=body.range,
            interval=body.interval,
            params=body.params,
            initial_cash=body.initial_cash,
            cost_model=body.cost_model,
            max_position_pct=body.max_position_pct,
            t_plus_one=body.t_plus_one,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"组合回测失败: {exc!s}") from exc


@router.post("/optimize")
async def quant_optimize(
    body: OptimizeRequest,
    _: Annotated[UserRecord, Depends(get_current_user)],
):
    try:
        return await quant_backtest.optimize_backtest_from_market(
            symbol=body.symbol, range_param=body.range, interval=body.interval,
            strategy_id=body.strategy_id, param_grid=body.param_grid,
            initial_cash=body.initial_cash, cost_model=body.cost_model,
            max_position_pct=body.max_position_pct, t_plus_one=body.t_plus_one,
            train_ratio=body.train_ratio, objective=body.objective,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"稳健性验证失败: {exc!s}") from exc


@router.post("/strategy-draft")
def quant_strategy_draft(
    body: DraftRequest,
    _: Annotated[UserRecord, Depends(get_current_user)],
):
    return quant_backtest.generate_strategy_draft(body.goal, symbol=body.symbol.strip().upper())
