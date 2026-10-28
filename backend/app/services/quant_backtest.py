"""量化交易策略制定与回测。

设计取向：
- 借鉴聚宽的生命周期概念（初始化、盘前/盘中信号、收盘结算、绩效报告），但不执行任意用户 Python。
- 借鉴 agent-quantspace 的「信号/权重 -> 向量化回测 -> 报告」拆分。
- 借鉴 OSKHQuant 的 A 股交易成本、滑点、T+1 与风险约束。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import math
from datetime import datetime
from decimal import Decimal
from collections import deque
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.services import kline_pipeline, llm_client, trading_rules

TRADING_DAYS = 252
ENGINE_VERSION = "daily-target-replay-v2.2"

STRATEGY_CATALOG: list[dict[str, Any]] = [
    {
        "id": "buy_and_hold",
        "name": "买入并持有",
        "family": "baseline",
        "description": "首个可交易日开盘按目标仓位买入，持有至结束。",
        "params": [],
    },
    {
        "id": "ma_cross",
        "name": "双均线交叉",
        "family": "trend",
        "description": "短均线上穿长均线买入，下穿卖出。",
        "params": [
            {"key": "fast", "label": "快线周期", "type": "int", "default": 5, "min": 2, "max": 120},
            {"key": "slow", "label": "慢线周期", "type": "int", "default": 20, "min": 3, "max": 250},
        ],
    },
    {
        "id": "rsi_reversal",
        "name": "RSI 均值回归",
        "family": "mean_reversion",
        "description": "RSI 从超卖区回升时买入，从超买区回落时卖出。",
        "params": [
            {"key": "period", "label": "RSI 周期", "type": "int", "default": 14, "min": 3, "max": 60},
            {"key": "lower", "label": "超卖阈值", "type": "float", "default": 30, "min": 5, "max": 50},
            {"key": "upper", "label": "超买阈值", "type": "float", "default": 70, "min": 50, "max": 95},
        ],
    },
    {
        "id": "ma_reversion_atr_stop",
        "name": "均线回归 + ATR 止损",
        "family": "risk_controlled_reversion",
        "description": "价格低于均线时试探买入，持仓后用 ATR 移动止损退出。",
        "params": [
            {"key": "ma", "label": "均线周期", "type": "int", "default": 10, "min": 3, "max": 120},
            {"key": "atr", "label": "ATR 周期", "type": "int", "default": 14, "min": 3, "max": 80},
            {"key": "atr_multiplier", "label": "ATR 倍数", "type": "float", "default": 2.0, "min": 0.5, "max": 8.0},
        ],
    },
]

PORTFOLIO_STRATEGY_CATALOG: list[dict[str, Any]] = [
    {
        "id": "factor_rotation_topn",
        "name": "多因子 TopN 轮动",
        "family": "cross_sectional_factor",
        "description": "在股票池内按动量、均线趋势、波动率、量能等因子打分，定期选择 Top N 等权持有。",
        "params": [
            {"key": "top_n", "label": "持仓数量", "type": "int", "default": 3, "min": 1, "max": 30},
            {"key": "rebalance_days", "label": "调仓间隔（日）", "type": "int", "default": 20, "min": 1, "max": 120},
            {"key": "momentum_lookback", "label": "动量窗口", "type": "int", "default": 60, "min": 5, "max": 250},
            {"key": "trend_ma", "label": "趋势均线", "type": "int", "default": 20, "min": 5, "max": 250},
            {"key": "volatility_lookback", "label": "波动率窗口", "type": "int", "default": 20, "min": 5, "max": 120},
            {"key": "w_momentum", "label": "动量权重", "type": "float", "default": 1.0, "min": -5, "max": 5},
            {"key": "w_trend", "label": "趋势权重", "type": "float", "default": 0.6, "min": -5, "max": 5},
            {"key": "w_low_vol", "label": "低波权重", "type": "float", "default": 0.4, "min": -5, "max": 5},
            {"key": "w_volume", "label": "量能权重", "type": "float", "default": 0.2, "min": -5, "max": 5},
        ],
    }
]

JOINQUANT_MODEL = {
    "lifecycle": [
        "initialize(context): 初始化参数、股票池和风险约束",
        "before_trading_start(context): 盘前准备数据和调度",
        "handle_data(context, data): 按 bar 生成目标仓位/订单",
        "after_trading_end(context): 收盘结算、归因与报告",
    ],
    "order_api_mapping": [
        "order_target_percent -> 本系统 target_weight",
        "order_target_value -> 本系统 target_value",
        "set_order_cost / set_slippage -> 本系统 cost_model",
        "run_daily/run_weekly -> 本系统 rebalance_frequency（当前日线逐 bar）",
    ],
}


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    window: deque[float] = deque()
    total = 0.0
    for value in values:
        window.append(value)
        total += value
        if len(window) > period:
            total -= window.popleft()
        out.append(total / period if len(window) == period else None)
    return out


def _rsi(values: list[float], period: int) -> list[float | None]:
    if period <= 1:
        raise ValueError("RSI 周期须大于 1")
    out: list[float | None] = [None] * len(values)
    gains: deque[float] = deque()
    losses: deque[float] = deque()
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
        if len(gains) > period:
            gains.popleft()
            losses.popleft()
        if len(gains) == period:
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
            out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _atr(bars: list[dict[str, Any]], period: int) -> list[float | None]:
    trs: list[float] = []
    out: list[float | None] = []
    prev_close: float | None = None
    for bar in bars:
        high = _num(bar.get("h"))
        low = _num(bar.get("l"))
        close = _num(bar.get("c"))
        tr = high - low if prev_close is None else max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
        if len(trs) >= period:
            out.append(sum(trs[-period:]) / period)
        else:
            out.append(None)
    return out


def _clamp_params(strategy_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
    params = dict(params or {})
    if strategy_id == "ma_cross":
        fast = max(2, min(120, int(params.get("fast") or 5)))
        slow = max(3, min(250, int(params.get("slow") or 20)))
        if fast >= slow:
            slow = min(250, fast + 5)
        return {"fast": fast, "slow": slow}
    if strategy_id == "rsi_reversal":
        lower = max(5.0, min(50.0, _num(params.get("lower"), 30.0)))
        upper = max(50.0, min(95.0, _num(params.get("upper"), 70.0)))
        if lower >= upper:
            lower, upper = 30.0, 70.0
        return {"period": max(3, min(60, int(params.get("period") or 14))), "lower": lower, "upper": upper}
    if strategy_id == "ma_reversion_atr_stop":
        return {
            "ma": max(3, min(120, int(params.get("ma") or 10))),
            "atr": max(3, min(80, int(params.get("atr") or 14))),
            "atr_multiplier": max(0.5, min(8.0, _num(params.get("atr_multiplier"), 2.0))),
        }
    return {}


def _clamp_portfolio_params(params: dict[str, Any] | None, pool_size: int) -> dict[str, Any]:
    params = dict(params or {})
    top_n = max(1, min(max(1, pool_size), int(params.get("top_n") or 3)))
    return {
        "top_n": top_n,
        "rebalance_days": max(1, min(120, int(params.get("rebalance_days") or 20))),
        "momentum_lookback": max(5, min(250, int(params.get("momentum_lookback") or 60))),
        "trend_ma": max(5, min(250, int(params.get("trend_ma") or 20))),
        "volatility_lookback": max(5, min(120, int(params.get("volatility_lookback") or 20))),
        "w_momentum": max(-5.0, min(5.0, _num(params.get("w_momentum"), 1.0))),
        "w_trend": max(-5.0, min(5.0, _num(params.get("w_trend"), 0.6))),
        "w_low_vol": max(-5.0, min(5.0, _num(params.get("w_low_vol"), 0.4))),
        "w_volume": max(-5.0, min(5.0, _num(params.get("w_volume"), 0.2))),
    }


def _signal_weights(bars: list[dict[str, Any]], strategy_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    closes = [_num(b.get("c")) for b in bars]
    highs = [_num(b.get("h")) for b in bars]
    weights = [0.0] * len(bars)
    reasons = [""] * len(bars)
    if strategy_id == "buy_and_hold":
        for i in range(len(bars)):
            weights[i] = 1.0
            reasons[i] = "基准策略持有"
    elif strategy_id == "ma_cross":
        fast, slow = int(params["fast"]), int(params["slow"])
        ma_fast = _sma(closes, fast)
        ma_slow = _sma(closes, slow)
        pos = 0.0
        for i in range(1, len(bars)):
            f0, f1, s0, s1 = ma_fast[i - 1], ma_fast[i], ma_slow[i - 1], ma_slow[i]
            if None not in (f0, f1, s0, s1):
                if f0 <= s0 and f1 > s1:
                    pos = 1.0
                    reasons[i] = f"MA{fast} 上穿 MA{slow}"
                elif f0 >= s0 and f1 < s1:
                    pos = 0.0
                    reasons[i] = f"MA{fast} 下穿 MA{slow}"
            weights[i] = pos
    elif strategy_id == "rsi_reversal":
        rsi = _rsi(closes, int(params["period"]))
        lower, upper = float(params["lower"]), float(params["upper"])
        pos = 0.0
        for i in range(2, len(bars)):
            r0, r1 = rsi[i - 1], rsi[i]
            if r0 is not None and r1 is not None:
                if r0 < lower <= r1:
                    pos = 1.0
                    reasons[i] = f"RSI 上穿 {lower:g}"
                elif r0 > upper >= r1:
                    pos = 0.0
                    reasons[i] = f"RSI 下穿 {upper:g}"
            weights[i] = pos
    elif strategy_id == "ma_reversion_atr_stop":
        ma = _sma(closes, int(params["ma"]))
        atr = _atr(bars, int(params["atr"]))
        mul = float(params["atr_multiplier"])
        pos = 0.0
        high_water = 0.0
        stop = 0.0
        for i in range(1, len(bars)):
            if ma[i] is None or atr[i] is None:
                weights[i] = pos
                continue
            close = closes[i]
            if pos <= 0 and close < float(ma[i]):
                pos = 1.0
                high_water = max(close, highs[i])
                stop = high_water - mul * float(atr[i])
                reasons[i] = "价格低于均线，回归试探"
            elif pos > 0:
                high_water = max(high_water, close, highs[i])
                stop = max(stop, high_water - mul * float(atr[i]))
                if close <= stop:
                    pos = 0.0
                    reasons[i] = "触发 ATR 移动止损"
            weights[i] = pos
    else:
        raise ValueError(f"未知策略: {strategy_id}")
    return [{"target_weight": weights[i], "reason": reasons[i]} for i in range(len(bars))]


def _slip_price(price: float, side: str, cost: dict[str, Any]) -> float:
    slippage = cost.get("slippage") or {}
    stype = str(slippage.get("type") or "ratio")
    decimals = int(cost.get("price_decimals", 2))
    if stype == "tick":
        delta = _num(slippage.get("tick_size"), 0.01) * _num(slippage.get("tick_count"), 2)
        return trading_rules.price_round(price + delta if side == "buy" else price - delta, decimals)
    ratio = _num(slippage.get("ratio"), 0.001) / 2.0
    return trading_rules.price_round(price * (1 + ratio) if side == "buy" else price * (1 - ratio), decimals)


def _fees(symbol: str, price: float, shares: int, side: str, cost: dict[str, Any], date: str) -> dict[str, float]:
    return trading_rules.fees(symbol, price, shares, side, cost, date)


def _default_cost_model(cost_model: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "commission_rate": 0.0003,
        "min_commission": 5.0,
        "stamp_tax_rate": None,
        "transfer_fee_rate": None,
        "flow_fee": 0.0,
        "price_decimals": 2,
        "max_volume_participation": 0.1,
        "fee_multiplier": 1.0,
        "slippage": {"type": "ratio", "ratio": 0.001, "tick_size": 0.01, "tick_count": 2},
    }
    for k, v in (cost_model or {}).items():
        if k == "slippage" and isinstance(v, dict):
            out["slippage"] = {**out["slippage"], **v}
        else:
            out[k] = v
    for key in ("commission_rate", "min_commission", "stamp_tax_rate", "transfer_fee_rate", "flow_fee", "fee_multiplier"):
        if out[key] is None and key in {"stamp_tax_rate", "transfer_fee_rate"}:
            continue
        if not math.isfinite(float(out[key])) or float(out[key]) < 0:
            raise ValueError(f"{key} 须为有限非负数")
        out[key] = float(out[key])
    participation = float(out["max_volume_participation"])
    if not math.isfinite(participation) or not 0 < participation <= 1:
        raise ValueError("max_volume_participation 须大于0且不超过1")
    out["max_volume_participation"] = participation
    if not 0 <= int(out["price_decimals"]) <= 6:
        raise ValueError("price_decimals 超出范围")
    slippage = out["slippage"]
    if not isinstance(slippage, dict):
        raise ValueError("slippage 须为对象")
    if slippage.get("type") not in {"ratio", "tick"}:
        raise ValueError("slippage.type 须为 ratio 或 tick")
    for key in ("ratio", "tick_size", "tick_count"):
        if not math.isfinite(float(slippage[key])) or float(slippage[key]) < 0:
            raise ValueError(f"slippage.{key} 须为有限非负数")
    if float(slippage["ratio"]) >= 2:
        raise ValueError("slippage.ratio 须小于2")
    return out


def _lot_shares(value: float, price: float) -> int:
    if value <= 0 or price <= 0:
        return 0
    return max(0, int(value / price) // 100 * 100)


def _annualized_return(total_return: float, n: int) -> float:
    if n <= 1:
        return total_return
    base = 1.0 + total_return
    if base <= 0:
        return -1.0
    return math.exp(min(700.0, math.log(base) * TRADING_DAYS / n)) - 1.0


def _performance(equity_curve: list[dict[str, Any]], trades: list[dict[str, Any]], initial_cash: float) -> dict[str, Any]:
    equities = [float(x["equity"]) for x in equity_curve]
    if not equities:
        return {}
    final = equities[-1]
    total_ret = final / initial_cash - 1.0
    previous = [initial_cash, *equities[:-1]]
    returns = [(current / prior - 1.0) for current, prior in zip(equities, previous) if prior > 0]
    avg = sum(returns) / len(returns) if returns else 0.0
    var = sum((r - avg) ** 2 for r in returns) / (len(returns) - 1) if len(returns) > 1 else 0.0
    vol = math.sqrt(var) * math.sqrt(TRADING_DAYS)
    sharpe = (avg / math.sqrt(var) * math.sqrt(TRADING_DAYS)) if var > 0 else 0.0
    peak = initial_cash
    max_dd = 0.0
    for eq in equities:
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0.0)
    sells = [t for t in trades if t["side"] == "sell"]
    return {
        "final_equity": round(final, 2),
        "total_return_pct": round(total_ret * 100, 4),
        "annualized_return_pct": round(_annualized_return(total_ret, len(equities)) * 100, 4),
        "annualized_volatility_pct": round(vol * 100, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "trade_count": len(trades),
        "sell_count": len(sells),
        "buy_count": sum(t["side"] == "buy" for t in trades),
        "completed_trade_count": sum(bool(t.get("position_closed")) for t in sells),
        "turnover_pct": round(sum(float(t["amount"]) for t in trades) / initial_cash * 100, 4),
        "total_fees": round(sum(float(t["fee"]["total"]) for t in trades), 4),
    }


def _day_timestamp(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


def _daily_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for original in rows:
        day = trading_rules.trade_date(original.get("trade_date") or original["t"])
        if day in out:
            raise ValueError("当前引擎仅支持日线，每标的每交易日须唯一（重复日期或分钟线）")
        row = dict(original)
        row["t"], row["trade_date"] = _day_timestamp(day), day
        for field in ("o", "c"):
            if row.get(field) is not None and (not math.isfinite(float(row[field])) or float(row[field]) < 0):
                raise ValueError(f"无效行情字段 {field}")
        out[day] = row
    return dict(sorted(out.items()))


def _fraction(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} 须介于0和1之间")
    return value


def run_target_backtest(
    *, bars_by_symbol: dict[str, list[dict[str, Any]]],
    targets_by_t: dict[int, dict[str, float]], initial_cash: float = 100_000.0,
    cost_model: dict[str, Any] | None = None, max_position_pct: float = 1.0,
    max_symbol_weight: float = 1.0, t_plus_one: bool = True,
    evaluation_start: int | None = None, evaluation_end: int | None = None,
    precommitted_targets_by_t: dict[int, dict[str, float]] | None = None,
    initial_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure daily replay shared by research and paper trading.

    targets_by_t contains complete portfolios committed at the CLOSE; execute
    once at the next union-calendar OPEN. Missing symbols imply zero weight.
    precommitted_targets_by_t contains portfolios committed before an explicitly
    named execution day. It is reserved for frozen paper orders, not optimizers.
    Warmup bars update context only; no warmup holdings, trades or returns enter
    evaluation. Capacity uses previous-day volume, never today's final volume.
    """
    if not math.isfinite(float(initial_cash)) or initial_cash <= 0:
        raise ValueError("initial_cash 须为有限正数")
    max_position_pct = _fraction(max_position_pct, "max_position_pct")
    max_symbol_weight = _fraction(max_symbol_weight, "max_symbol_weight")
    cost = _default_cost_model(cost_model)
    daily = {str(sym).upper(): _daily_rows(rows) for sym, rows in bars_by_symbol.items()}
    symbols = sorted(daily)
    dates = sorted({day for rows in daily.values() for day in rows})
    if not dates:
        raise ValueError("没有可回放日线")
    start = trading_rules.trade_date(evaluation_start) if evaluation_start is not None else dates[0]
    end = trading_rules.trade_date(evaluation_end) if evaluation_end is not None else dates[-1]
    if start > end:
        raise ValueError("评价起始日不能晚于结束日")

    def normalize_targets(raw: dict[int, dict[str, float]]) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        for timestamp, weights in raw.items():
            day = trading_rules.trade_date(timestamp)
            if day in result:
                raise ValueError("同一交易日只能提交一份目标")
            normalized = {str(sym).upper(): _fraction(weight, "target_weight") for sym, weight in weights.items()}
            if set(normalized) - set(symbols):
                raise ValueError("目标包含未提供行情的标的")
            limited = {sym: min(normalized.get(sym, 0.0), max_symbol_weight) for sym in symbols}
            total = sum(limited.values())
            scale = min(1.0, max_position_pct / total) if total else 0.0
            result[day] = {sym: weight * scale for sym, weight in limited.items()}
        return result

    close_targets = normalize_targets(targets_by_t)
    committed = normalize_targets(precommitted_targets_by_t or {})
    # bisect handles targets from the last close of a previous persisted batch.
    from bisect import bisect_right
    execution_targets: dict[str, tuple[str, dict[str, float]]] = {}
    for day, weights in sorted(close_targets.items()):
        next_index = bisect_right(dates, day)
        if next_index < len(dates):
            execution_targets[dates[next_index]] = (day, weights)
    for day, weights in committed.items():
        if day in execution_targets:
            raise ValueError("预承诺目标与收盘目标冲突")
        execution_targets[day] = ("precommitted", weights)

    state = dict(initial_state or {})
    cash = float(state.get("cash", initial_cash))
    shares = {sym: int((state.get("positions") or {}).get(sym, 0)) for sym in symbols}
    if not math.isfinite(cash) or cash < 0 or any(value < 0 for value in shares.values()):
        raise ValueError("初始账户不支持负现金或空头")
    if set(state.get("positions") or {}) - set(symbols):
        raise ValueError("初始持仓缺少对应行情")
    available = {sym: int((state.get("available") or {}).get(sym, shares[sym])) for sym in symbols}
    if any(available[sym] < 0 or available[sym] > shares[sym] for sym in symbols):
        raise ValueError("初始可卖数量无效")
    last_prices = {sym: _num((state.get("last_prices") or {}).get(sym)) for sym in symbols}
    basis = {sym: _num((state.get("cost_basis") or {}).get(sym), shares[sym] * last_prices[sym]) for sym in symbols}
    applied_actions = set(state.get("applied_corporate_actions") or [])
    cash_distributions = {sym: _num((state.get("cash_distributions") or {}).get(sym)) for sym in symbols}
    last_day = state.get("last_trade_date")
    if any(shares[sym] and last_prices[sym] <= 0 for sym in symbols):
        raise ValueError("初始持仓必须提供last_prices")
    opening_equity = cash + sum(shares[sym] * last_prices[sym] for sym in symbols)
    if opening_equity <= 0:
        raise ValueError("初始净值须为正数")
    previous: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    issues: set[str] = {"symbol_history_missing"} if any(not rows for rows in daily.values()) else set()
    warnings: set[str] = set()
    if any(value > 0 for value in cash_distributions.values()):
        issues.add("cash_dividend_net_payment_unverified")
        warnings.add("恢复账户包含税前股息，投资者股息税及实际派款日差异未模拟")
    action_blocked: set[str] = set(state.get("unresolved_corporate_actions") or [])
    if action_blocked:
        issues.add("unresolved_corporate_action_in_account")
    action_ledger: list[dict[str, Any]] = []
    prev_weights = {sym: 0.0 for sym in symbols}

    def reject(day: str, sym: str, side: str, reason: str, requested: int = 0) -> None:
        if reason == "missing_bar":
            issues.add("missing_execution_bar")
        rejected.append({"t": _day_timestamp(day), "trade_date": day, "symbol": sym,
                         "side": side, "reason": reason, "requested_volume": requested})

    for day in dates:
        if day > end:
            break
        bars = {sym: daily[sym].get(day) for sym in symbols}
        in_evaluation = day >= start and (not state.get("last_trade_date") or day > state["last_trade_date"])
        actions_today: dict[str, list[dict[str, Any]]] = {}
        for sym, bar in bars.items():
            if not bar:
                continue
            issues.update(trading_rules.daily_bar_availability_issues(bar, day))
            if bar.get("availability_inferred") is True:
                issues.add("historical_availability_missing")
            if bar.get("example_only") or bar.get("synthetic") or bar.get("source") == "format_fixture_not_market_data":
                issues.add("example_or_synthetic_data")
            if bar.get("price_basis") != "raw" and not (bar.get("execution_o") and bar.get("execution_c")):
                issues.add("raw_execution_price_unverified")
            if _num(bar.get("execution_o"), _num(bar.get("o"))) <= 0 or _num(bar.get("execution_c"), _num(bar.get("c"))) <= 0:
                issues.add("nonpositive_execution_price")
            if not any(type(bar.get(key)) is bool for key in ("tradable", "suspended")) and bar.get("trade_status") not in {"trading", "suspended", "delisted"}:
                issues.add("historical_trading_status_missing")
            if bar.get("volume_unit") != "shares":
                issues.add("volume_unit_unverified")
            if bar.get("corporate_actions_checked") is not True:
                issues.add("corporate_action_coverage_unverified")
            if trading_rules.price_limits(sym, bar, previous.get(sym), day)[2]:
                issues.add("historical_price_limits_inferred")
            actions, action_issues = trading_rules.parse_corporate_actions(bar, day)
            issues.update(action_issues)
            if any(a["cash_dividend"] > 0 for a in actions):
                issues.add("cash_dividend_net_payment_unverified")
            if action_issues:
                if in_evaluation and shares[sym]:
                    action_blocked.add(sym)
            elif actions:
                actions_today[sym] = actions
            if day < "2015-08-01":
                issues.add("historical_fee_schedule_requires_override")
        if in_evaluation:
            if day != last_day:
                available = dict(shares)
            # Corporate actions belong to the overnight holders, before any
            # opening trades. The ledger travels with paper state on restart.
            for sym, actions in actions_today.items():
                pending = [a for a in actions if f"{sym}:{a['fingerprint']}" not in applied_actions]
                if not pending:
                    continue
                old_shares = shares[sym]
                old_available = available[sym]
                ratio = Decimal(1)
                dividend = Decimal(0)
                for action in pending:
                    ratio *= Decimal(str(action["split_ratio"]))
                    dividend += Decimal(str(action["cash_dividend"]))
                if not math.isfinite(float(ratio)) or not math.isfinite(float(dividend)):
                    issues.add("corporate_action_invalid_values")
                    action_blocked.add(sym)
                    reject(day, sym, "corporate_action", "invalid_values")
                    continue
                new_shares = Decimal(old_shares) * ratio
                new_available = Decimal(old_available) * ratio
                raw_reference = last_prices[sym] or _num((previous.get(sym) or {}).get("execution_c")) or _num((previous.get(sym) or {}).get("c"))
                reference = (Decimal(str(raw_reference)) - dividend) / ratio if raw_reference else Decimal(0)
                if new_shares != new_shares.to_integral_value() or new_available != new_available.to_integral_value():
                    issues.add("corporate_action_fractional_share_settlement_missing")
                    action_blocked.add(sym)
                    reject(day, sym, "corporate_action", "fractional_share_settlement_missing")
                    continue
                if (old_shares or raw_reference > 0) and reference <= 0:
                    issues.add("corporate_action_invalid_ex_reference")
                    action_blocked.add(sym)
                    reject(day, sym, "corporate_action", "invalid_ex_reference")
                    continue
                cash_credit = float(Decimal(old_shares) * dividend)
                cash += cash_credit
                cash_distributions[sym] += cash_credit
                shares[sym], available[sym] = int(new_shares), int(new_available)
                # Keep total acquisition basis; splits dilute average cost.
                # Dividends are separately recorded cash income, not a second
                # reduction of acquisition basis / realized trading P&L.
                if reference > 0:
                    last_prices[sym] = float(reference)
                    if sym in previous:
                        previous[sym] = {**previous[sym], "execution_c": float(reference),
                                         "v": _num(previous[sym].get("v")) * float(ratio)}
                action_ids = [f"{sym}:{action['fingerprint']}" for action in pending]
                applied_actions.update(action_ids)
                action_ledger.append({"t": _day_timestamp(day), "trade_date": day, "symbol": sym,
                                      "action_ids": action_ids, "old_shares": old_shares,
                                      "new_shares": shares[sym], "available_shares": available[sym],
                                      "split_ratio": float(ratio), "cash_dividend_per_old_share": float(dividend),
                                      "gross_cash_credit": cash_credit, "cost_basis": basis[sym],
                                      "average_cost": basis[sym] / shares[sym] if shares[sym] else 0.0,
                                      "ex_reference_price": float(reference) if reference > 0 else None,
                                      "dividend_tax_basis": "gross_before_investor_tax"})
                if dividend > 0:
                    warnings.add("现金股息按税前总额于除权日记账；投资者持有期股息税及实际派款日差异未模拟")
            open_prices = {sym: _num((bars[sym] or {}).get("execution_o")) or _num((bars[sym] or {}).get("o")) or last_prices[sym] for sym in symbols}
            equity_open = cash + sum(shares[sym] * open_prices[sym] for sym in symbols)
            target_entry = execution_targets.get(day)
            if target_entry:
                signal_day, weights = target_entry
                prev_weights = dict(weights)
                for side in ("sell", "buy"):
                    for sym in symbols:
                        bar = bars[sym]
                        px0 = open_prices[sym]
                        if side == "buy" and weights[sym] > 0 and px0 <= 0:
                            reject(day, sym, side, "missing_bar" if bar is None else "invalid_open")
                            continue
                        desired = int(equity_open * weights[sym] / px0) if px0 > 0 else 0
                        # Round target holdings to the board's increment; order minimum
                        # applies to the delta, not an already-existing small position.
                        step = 1 if trading_rules.board_for(sym, bar) in {"star", "beijing"} else 100
                        desired = desired // step * step
                        delta = desired - shares[sym]
                        if (side == "buy" and delta <= 0) or (side == "sell" and delta >= 0):
                            continue
                        requested = abs(delta)
                        px = _slip_price(px0, side, cost)
                        reason = "unmodeled_corporate_action" if sym in action_blocked else trading_rules.execution_block_reason(sym, bar, previous.get(sym), side, px, day)
                        if reason:
                            reject(day, sym, side, reason, requested)
                            continue
                        prior = previous.get(sym) or {}
                        # available_volume denotes a separately supplied pre-open auction
                        # capacity; otherwise use a conservative prior-day participation cap.
                        known_volume = _num(bar.get("available_volume")) if "available_volume" in bar else _num(prior.get("v"))
                        if (bar.get("volume_unit") if "available_volume" in bar else prior.get("volume_unit")) == "lots":
                            known_volume *= 100
                        capacity = int(known_volume * cost["max_volume_participation"])
                        if capacity <= 0:
                            reject(day, sym, side, "volume_capacity_unavailable", requested)
                            continue
                        quantity = min(requested, capacity)
                        if side == "sell":
                            quantity = min(quantity, available[sym] if t_plus_one else shares[sym])
                        quantity = trading_rules.order_quantity(sym, quantity, side=side, held=shares[sym], bar=bar)
                        if side == "buy" and quantity:
                            # Binary search reserves all fees, including minimum commission.
                            low, high = 0, quantity
                            while low < high:
                                mid = (low + high + 1) // 2
                                gross = px * mid
                                total = gross + _fees(sym, px, mid, side, cost, day)["total"]
                                if total <= cash + 1e-8:
                                    low = mid
                                else:
                                    high = mid - 1
                            quantity = trading_rules.order_quantity(sym, low, side=side, held=shares[sym], bar=bar)
                        if quantity <= 0:
                            reason = "t_plus_one" if side == "sell" and available[sym] <= 0 else "insufficient_cash_or_order_size" if side == "buy" else "order_size_or_capacity"
                            reject(day, sym, side, reason, requested)
                            continue
                        fee = _fees(sym, px, quantity, side, cost, day)
                        gross = px * quantity
                        if side == "sell" and cash + gross < fee["total"]:
                            reject(day, sym, side, "insufficient_cash_for_fees", requested)
                            continue
                        realized = None
                        if side == "buy":
                            cash -= gross + fee["total"]
                            basis[sym] += gross + fee["total"]
                            shares[sym] += quantity
                            if not t_plus_one:
                                available[sym] += quantity
                        else:
                            removed_basis = basis[sym] * quantity / shares[sym]
                            basis[sym] -= removed_basis
                            realized = gross - fee["total"] - removed_basis
                            cash += gross - fee["total"]
                            shares[sym] -= quantity
                            available[sym] = max(0, available[sym] - quantity)
                        cash = max(0.0, cash) if cash > -1e-8 else cash
                        if cash < 0:
                            raise AssertionError("Execution produced negative cash")
                        trades.append({"t": _day_timestamp(day), "trade_date": day, "symbol": sym,
                                       "signal_date": signal_day, "side": side, "price": px,
                                       "volume": quantity, "amount": round(gross, 4), "fee": fee,
                                       "reason": "已承诺目标仓位", "requested_volume": requested,
                                       "partial": quantity < requested, "realized_pnl": round(realized, 4) if realized is not None else None,
                                       "position_closed": side == "sell" and shares[sym] == 0})
                        if quantity < requested:
                            reject(day, sym, side, "partial_fill_capacity_cash_or_lot", requested - quantity)
            for sym, bar in bars.items():
                if bar and (_num(bar.get("execution_c")) or _num(bar.get("c"))) > 0:
                    last_prices[sym] = _num(bar.get("execution_c")) or _num(bar.get("c"))
                elif shares[sym]:
                    issues.add("stale_holding_valuation")
            equity = cash + sum(shares[sym] * last_prices[sym] for sym in symbols)
            curve.append({"t": _day_timestamp(day), "trade_date": day, "equity": round(equity, 6),
                          "cash": round(cash, 6), "holding_count": sum(v > 0 for v in shares.values()),
                          "shares": sum(shares.values())})
            positions.append({"t": _day_timestamp(day), "target_weights": dict(prev_weights),
                              "actual_weight": round((equity - cash) / equity, 6) if equity else 0.0,
                              "holdings": dict(shares), "holding_count": sum(v > 0 for v in shares.values())})
            last_day = day
        for sym, bar in bars.items():
            if bar:
                previous[sym] = bar
                if not in_evaluation and not state.get("last_trade_date"):
                    last_prices[sym] = _num(bar.get("execution_c")) or _num(bar.get("c")) or last_prices[sym]
    warnings.add("日线开盘撮合和前日量容量为保守近似，未模拟真实盘口排队")
    warnings.add("交易日历来自输入数据并集；数据完整性须由数据快照质量检查保证")
    if not t_plus_one:
        issues.add("a_share_t_plus_one_disabled")
    performance = _performance(curve, trades, opening_equity)
    performance["gross_dividend_income"] = round(sum(row["gross_cash_credit"] for row in action_ledger), 6)
    result = {"engine_version": ENGINE_VERSION, "rules_version": trading_rules.RULES_VERSION,
              "performance": performance, "trades": trades, "equity_curve": curve,
              "position_curve": positions, "rejected_orders": rejected, "bar_count": len(curve),
              "corporate_action_ledger": action_ledger,
              "initial_cash": initial_cash, "initial_equity": opening_equity, "cost_model": cost,
              "max_position_pct": max_position_pct, "max_symbol_weight": max_symbol_weight,
              "t_plus_one": bool(t_plus_one),
              "quality": {"eligible": not issues and bool(curve), "issues": sorted(issues), "warnings": sorted(warnings)},
              "final_state": {"cash": cash, "positions": shares, "available": available,
                              "cost_basis": basis, "last_prices": last_prices, "last_trade_date": last_day,
                              "applied_corporate_actions": sorted(applied_actions),
                              "unresolved_corporate_actions": sorted(action_blocked),
                              "cash_distributions": cash_distributions},
              "engine_notes": ["收盘目标仅在下一交易日开盘执行一次，非调仓日不交易。", "预热期不成交、不计绩效；未知交易元数据会禁止自动晋级。", "已公告分红和拆股于除权日开盘前按旧持仓入账；未知行动、迟到公告和未明确零碎股结算仍拒绝交易。"]}
    result.update(performance)
    return result


def run_backtest(
    *, candles: list[dict[str, Any]], strategy_id: str, params: dict[str, Any] | None,
    initial_cash: float, commission_rate: float | None = None,
    cost_model: dict[str, Any] | None = None, max_position_pct: float = 1.0,
    t_plus_one: bool = True, symbol: str = "", evaluation_start: int | None = None,
    evaluation_end: int | None = None,
) -> dict[str, Any]:
    bars = list(_daily_rows(candles).values())
    if len(bars) < 20:
        raise ValueError("K 线数据不足，至少需要 20 根")
    sid = (strategy_id or "buy_and_hold").strip().lower()
    clamped = _clamp_params(sid, params)
    cost = dict(cost_model or {})
    if commission_rate is not None:
        cost["commission_rate"] = commission_rate
    signals = _signal_weights(bars, sid, clamped)
    sym = symbol.strip().upper() or "600000.SS"
    targets: dict[int, dict[str, float]] = {}
    previous_weight: float | None = None
    evaluation_day = trading_rules.trade_date(evaluation_start) if evaluation_start is not None else None
    for index, (bar, signal) in enumerate(zip(bars, signals)):
        weight = signal["target_weight"]
        # Seed the evaluation portfolio from the immediately preceding close,
        # even if a persistent position signal started during indicator warmup.
        seed = evaluation_day is not None and index + 1 < len(bars) and bar["trade_date"] < evaluation_day <= bars[index + 1]["trade_date"]
        if weight != previous_weight or seed:
            targets[bar["t"]] = {sym: weight}
        previous_weight = weight
    result = run_target_backtest(bars_by_symbol={sym: bars}, targets_by_t=targets,
                                 initial_cash=initial_cash, cost_model=cost,
                                 max_position_pct=max_position_pct, t_plus_one=t_plus_one,
                                 evaluation_start=evaluation_start, evaluation_end=evaluation_end)
    # Benchmark uses the same fees, calendar and execution constraints.
    benchmark_targets = {bars[0]["t"]: {sym: 1.0}}
    if evaluation_day:
        earlier = [b for b in bars if b["trade_date"] < evaluation_day]
        if earlier:
            benchmark_targets = {earlier[-1]["t"]: {sym: 1.0}}
    if sid == "buy_and_hold" and max_position_pct == 1.0:
        benchmark = result["performance"].get("total_return_pct", 0.0)
    else:
        baseline = run_target_backtest(bars_by_symbol={sym: bars}, targets_by_t=benchmark_targets,
                                      initial_cash=initial_cash, cost_model=cost, t_plus_one=t_plus_one,
                                      evaluation_start=evaluation_start, evaluation_end=evaluation_end)
        benchmark = baseline["performance"].get("total_return_pct", 0.0)
    result["performance"]["benchmark_buy_hold_return_pct"] = benchmark
    result["performance"]["excess_return_pct"] = round(result["performance"].get("total_return_pct", 0.0) - benchmark, 4)
    result.update(result["performance"])
    result.update({"strategy_id": sid, "strategy_name": next((s["name"] for s in STRATEGY_CATALOG if s["id"] == sid), sid), "params": clamped})
    for row in result["position_curve"]:
        row["target_weight"] = row["target_weights"].get(sym, 0.0)
    return result


def _require_daily(interval: str) -> None:
    if interval != "1d":
        raise ValueError("可信回测当前仅支持1d日线，分钟/周/月线不能沿用日线年化及T+1规则")


async def _market_bundle(symbol: str, range_param: str, interval: str) -> dict[str, Any]:
    from app.security.user_context import current_user_id
    from app.services import research_data
    _require_daily(interval)
    return await research_data.fetch_bundle(current_user_id.get() or 0, symbol, range_param, interval)


def _merge_market_quality(result: dict, bundle: dict) -> None:
    """Execution eligibility never overrides acquisition/provenance blockers."""
    quality = result["quality"]
    source_quality = bundle.get("research_quality") or {}
    issues = set(quality.get("issues", [])) | set(source_quality.get("issues", []))
    if source_quality.get("eligible") is False and not source_quality.get("issues"):
        issues.add("market_data_quality_unverified")
    if bundle.get("cache_stale"):
        issues.add("stale_market_cache")
    result["quality"] = {"eligible": bool(quality.get("eligible")) and not issues,
                         "issues": sorted(issues),
                         "warnings": sorted(set(quality.get("warnings", [])) | set(source_quality.get("warnings", [])))}


async def backtest_from_market(
    symbol: str,
    range_param: str,
    interval: str,
    strategy_id: str,
    params: dict[str, Any] | None,
    initial_cash: float,
    commission_rate: float | None = None,
    cost_model: dict[str, Any] | None = None,
    max_position_pct: float = 1.0,
    t_plus_one: bool = True,
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    bundle = await _market_bundle(sym, range_param, interval)
    result = run_backtest(
        candles=bundle.get("candles") or [],
        strategy_id=strategy_id,
        params=params,
        initial_cash=initial_cash,
        commission_rate=commission_rate,
        cost_model=cost_model,
        max_position_pct=max_position_pct,
        t_plus_one=t_plus_one,
        symbol=sym,
    )
    result.update(
        {
            "symbol": sym,
            "range": range_param,
            "interval": interval,
            "kline_source": bundle.get("kline_source"),
            "data_as_of": bundle.get("data_as_of"),
            "research_batch_ids": bundle.get("research_batch_ids", []),
        }
    )
    _merge_market_quality(result, bundle)
    return result


def _grid_values(definition: dict[str, Any], raw: Any) -> list[Any]:
    """Normalize a UI supplied grid while enforcing the strategy catalog bounds."""
    values = raw if isinstance(raw, list) else [raw]
    out: list[Any] = []
    for value in values:
        try:
            parsed = int(value) if definition.get("type") == "int" else float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(parsed):
            continue
        parsed = max(definition.get("min", parsed), min(definition.get("max", parsed), parsed))
        if parsed not in out:
            out.append(parsed)
    return out[:12]


def optimize_backtest(
    *, candles: list[dict[str, Any]], strategy_id: str, param_grid: dict[str, Any] | None,
    initial_cash: float, cost_model: dict[str, Any] | None = None,
    max_position_pct: float = 1.0, t_plus_one: bool = True, symbol: str = "",
    train_ratio: float = 0.7, objective: str = "sharpe",
    include_holdout: bool = True,
) -> dict[str, Any]:
    """Train shortlist -> validation selection/stress -> ONE untouched holdout.

    The holdout is not exposed to proposal generation or candidate selection.
    This pure function has no persistence. HTTP/worker callers must use the
    recorded wrapper, which reserves the user-wide holdout before execution.
    """
    catalog = next((s for s in STRATEGY_CATALOG if s["id"] == strategy_id), None)
    if not catalog or not catalog.get("params"):
        raise ValueError("该策略没有可优化参数")
    bars = list(_daily_rows(candles).values())
    if len(bars) < 80:
        raise ValueError("稳健性验证至少需要 80 根 K 线")
    if objective not in {"sharpe", "return"}:
        raise ValueError("objective 须为 sharpe 或 return")
    if not math.isfinite(float(train_ratio)):
        raise ValueError("train_ratio 须为有限数")
    ratio = max(0.55, min(0.85, float(train_ratio)))
    train_end = max(40, min(len(bars) - 40, int(len(bars) * ratio)))
    validation_end = train_end + (len(bars) - train_end) // 2
    definitions = {p["key"]: p for p in catalog["params"]}
    keys = list(definitions)
    axes = [_grid_values(definition, (param_grid or {}).get(key, definition["default"])) or [definition["default"]] for key, definition in definitions.items()]
    combinations = list(itertools.islice(itertools.product(*axes), 60))
    metric_key = "total_return_pct" if objective == "return" else "sharpe"
    common = {"strategy_id": strategy_id, "initial_cash": initial_cash, "cost_model": cost_model,
              "max_position_pct": max_position_pct, "t_plus_one": t_plus_one, "symbol": symbol}
    rows: list[dict[str, Any]] = []
    for values in combinations:
        train = run_backtest(candles=bars[:train_end], params=dict(zip(keys, values, strict=True)), **common)
        rows.append({"params": train["params"], "train": train["performance"]})
    rows.sort(key=lambda row: float(row["train"].get(metric_key) or 0), reverse=True)
    finalists = rows[: min(10, len(rows))]
    for row in finalists:
        # All pre-split bars are indicator context only; no arbitrary 30-bar limit.
        validation = run_backtest(candles=bars[:validation_end], params=row["params"],
                                  evaluation_start=bars[train_end]["t"], **common)
        row["validation"] = validation["performance"]
        train_score = float(row["train"].get(metric_key) or 0)
        score = float(row["validation"].get(metric_key) or 0)
        row["stability_score"] = round(score - abs(train_score - score) * 0.25, 4)
    finalists.sort(key=lambda row: row["stability_score"], reverse=True)
    selected = finalists[0]
    frozen_params = dict(selected["params"])
    stress = []
    base_cost = _default_cost_model(cost_model)
    for multiple in (0.0, 1.0, 2.0):
        stressed = {**base_cost, "fee_multiplier": base_cost["fee_multiplier"] * multiple}
        stressed["slippage"] = {**base_cost["slippage"], "ratio": float(base_cost["slippage"]["ratio"]) * multiple,
                               "tick_count": float(base_cost["slippage"]["tick_count"]) * multiple}
        result = run_backtest(candles=bars[:validation_end], params=frozen_params,
                              evaluation_start=bars[train_end]["t"], **{**common, "cost_model": stressed})
        stress.append({"cost_multiple": multiple, "partition": "validation", "performance": result["performance"]})
        if multiple == 1.0:
            quality = result["quality"]
    best = dict(selected)
    if include_holdout:
        holdout = run_backtest(candles=bars, params=frozen_params,
                              evaluation_start=bars[validation_end]["t"], **common)
        best.update(holdout=holdout["performance"], test=holdout["performance"])
        quality = holdout["quality"]
    return {
        "mode": "robustness_lab", "engine_version": ENGINE_VERSION,
        "strategy_id": strategy_id, "symbol": symbol, "objective": objective,
        "train_ratio": train_end / len(bars), "split_timestamp": bars[train_end]["t"],
        "holdout_start": bars[validation_end]["t"], "holdout_evaluation_count": int(include_holdout),
        "holdout_status": "evaluated" if include_holdout else "not_evaluated",
        "partitions": {"train": {"start": bars[0]["t"], "end": bars[train_end - 1]["t"], "bars": train_end},
                       "validation": {"start": bars[train_end]["t"], "end": bars[validation_end - 1]["t"], "bars": validation_end - train_end},
                       "holdout": {"start": bars[validation_end]["t"], "end": bars[-1]["t"], "bars": len(bars) - validation_end}},
        "combination_count": len(combinations), "best_params": frozen_params, "best": best,
        "candidates": finalists, "cost_stress": stress, "quality": quality,
        "notes": ["训练期只选入围候选；验证期选优并做成本压力测试；参数冻结后仅评估一次独立留出期。",
                  "预热使用完整此前历史，预热期不成交且不计入收益。",
                  "历史测试会消耗独立测试集；HTTP入口与自主研究共享每账户一次的持久访问门禁。"],
    }


def _recorded_market_optimization(user_id: int, bundle: dict, sym: str, range_param: str,
                                  interval: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Freeze validation-selected parameters before atomic, durable access.

    The shared gate is deliberately user-wide, including changed windows and
    symbols: a previously seen test interval cannot become 'independent' by
    changing a request. A crash after reservation consumes access fail-closed.
    """
    from app.storage.research_store import ResearchStore, content_hash
    ledger = ResearchStore()
    bars = bundle.get("candles") or []
    # Acquisition timestamps and batch provenance do not change prices or execution inputs. Keep
    # nested action announcements, which *do* affect executability.
    stable_bars = [{key: value for key, value in bar.items()
                    if key not in {"fetched_at", "source_time", "available_at", "batch_id"}} for bar in bars]
    request = {"engine_version": ENGINE_VERSION, "symbol": sym, "range": range_param,
               "interval": interval, "data_hash": content_hash(stable_bars), "parameters": kwargs}
    request_id = content_hash(request)
    ledger.put_entity(user_id, "quant_optimization_request", request, request_id)
    cached = ledger.get_entity(user_id, "quant_optimization_result", request_id)
    if cached:
        ledger.put_entity(user_id, "quant_optimization_access", {"request_id": request_id, "status": "cached_result"})
        result = {**cached["payload"], "reused_result": True, "holdout_evaluation_count": 0}
        _merge_market_quality(result, bundle)
        return result
    result = optimize_backtest(candles=bars, symbol=sym, include_holdout=False, **kwargs)
    result.update({"range": range_param, "interval": interval, "kline_source": bundle.get("kline_source"),
                   "data_as_of": bundle.get("data_as_of"), "request_id": request_id, "reused_result": False,
                   "research_batch_ids": bundle.get("research_batch_ids", [])})
    freeze = {"kind": "legacy_quant", "request_id": request_id, "data_hash": request["data_hash"],
              "engine_version": ENGINE_VERSION, "params": result["best_params"], "partitions": result["partitions"]}
    ledger.put_entity(user_id, "quant_optimization_freeze", freeze, request_id)
    permitted = ledger.compare_and_set_state(user_id, "holdout_access", None, freeze)
    if permitted:
        ledger.put_entity(user_id, "quant_optimization_access", {"request_id": request_id, "status": "reserved"})
        try:
            holdout = run_backtest(candles=bars, strategy_id=kwargs["strategy_id"], params=result["best_params"],
                                  initial_cash=kwargs["initial_cash"], cost_model=kwargs.get("cost_model"),
                                  max_position_pct=kwargs.get("max_position_pct", 1.0),
                                  t_plus_one=kwargs.get("t_plus_one", True), symbol=sym,
                                  evaluation_start=result["holdout_start"])
        except Exception as exc:
            ledger.put_entity(user_id, "quant_optimization_access", {"request_id": request_id, "status": "failed_consumed", "error_type": type(exc).__name__})
            raise
        result["best"].update(holdout=holdout["performance"], test=holdout["performance"])
        result.update(quality=holdout["quality"], holdout_status="evaluated", holdout_evaluation_count=1)
    else:
        result["holdout_status"] = "already_consumed"
        result["notes"].append("本账户的独立留出访问已消耗；本次只报告验证期选优，不再次查看测试期。")
    _merge_market_quality(result, bundle)
    ledger.put_entity(user_id, "quant_optimization_access", {"request_id": request_id, "status": result["holdout_status"]})
    if permitted:
        ledger.put_entity(user_id, "quant_optimization_result", result, request_id)
    else:
        # Concurrent requests may finish validation while the winning request
        # is still evaluating. They must never overwrite its immutable result.
        ledger.put_entity(user_id, "quant_validation_result", result)
    return result


async def optimize_backtest_from_market(**kwargs: Any) -> dict[str, Any]:
    from app.security.user_context import current_user_id
    sym = str(kwargs.pop("symbol", "")).strip().upper()
    range_param = str(kwargs.pop("range_param", "2y"))
    interval = str(kwargs.pop("interval", "1d"))
    bundle = await _market_bundle(sym, range_param, interval)
    return await asyncio.to_thread(_recorded_market_optimization, current_user_id.get() or 0,
                                   bundle, sym, range_param, interval, kwargs)


def _std(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    avg = sum(vals) / len(vals)
    return math.sqrt(sum((x - avg) ** 2 for x in vals) / (len(vals) - 1))


def _factor_score(
    symbol: str,
    idx: int,
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    params: dict[str, Any],
) -> dict[str, Any] | None:
    bars = bars_by_symbol[symbol]
    mom_n = int(params["momentum_lookback"])
    trend_n = int(params["trend_ma"])
    vol_n = int(params["volatility_lookback"])
    need = max(mom_n, trend_n, vol_n, 20) + 1
    if idx < need:
        return None
    closes = [_num(b.get("c")) for b in bars[: idx + 1]]
    vols = [_num(b.get("v")) for b in bars[: idx + 1]]
    c = closes[idx]
    if c <= 0:
        return None
    mom = c / closes[idx - mom_n] - 1.0 if closes[idx - mom_n] > 0 else 0.0
    ma_vals = closes[idx - trend_n + 1 : idx + 1]
    ma = sum(ma_vals) / len(ma_vals) if ma_vals else c
    trend = c / ma - 1.0 if ma > 0 else 0.0
    rets = [(closes[j] / closes[j - 1] - 1.0) for j in range(idx - vol_n + 1, idx + 1) if closes[j - 1] > 0]
    vol = _std(rets)
    vol5 = sum(vols[max(0, idx - 4) : idx + 1]) / min(5, idx + 1)
    vol20 = sum(vols[max(0, idx - 19) : idx + 1]) / min(20, idx + 1)
    volume = (vol5 / vol20 - 1.0) if vol20 > 0 else 0.0
    score = (
        params["w_momentum"] * mom
        + params["w_trend"] * trend
        - params["w_low_vol"] * vol
        + params["w_volume"] * volume
    )
    return {
        "symbol": symbol,
        "score": round(score, 6),
        "momentum": round(mom, 6),
        "trend": round(trend, 6),
        "volatility": round(vol, 6),
        "volume": round(volume, 6),
        "close": round(c, 4),
    }


async def _fetch_symbol_bundle(symbol: str, range_param: str, interval: str) -> tuple[str, dict[str, Any] | None, str | None]:
    sym = symbol.strip().upper()
    try:
        bundle = await _market_bundle(sym, range_param, interval)
        return sym, bundle, None
    except Exception as exc:  # noqa: BLE001
        return sym, None, str(exc)


def run_portfolio_factor_backtest(
    *, bars_by_symbol: dict[str, list[dict[str, Any]]], params: dict[str, Any] | None,
    initial_cash: float, cost_model: dict[str, Any] | None = None,
    max_position_pct: float = 1.0, t_plus_one: bool = True,
) -> dict[str, Any]:
    """Factor signals use each symbol's observed history and the union calendar."""
    from bisect import bisect_right
    daily = {sym: _daily_rows(rows) for sym, rows in bars_by_symbol.items()}
    if len(daily) < 2:
        raise ValueError("组合因子回测至少需要2只标的")
    calendar = sorted({day for rows in daily.values() for day in rows})
    parameters = _clamp_portfolio_params(params, len(daily))
    warmup = max(parameters["momentum_lookback"], parameters["trend_ma"], parameters["volatility_lookback"], 20) + 1
    if len(calendar) <= warmup + 1:
        raise ValueError("股票池日线不足以完成指标预热和下一日撮合")
    rows_by_symbol = {sym: list(rows.values()) for sym, rows in daily.items()}
    days_by_symbol = {sym: list(rows) for sym, rows in daily.items()}
    targets = {}
    log = []
    for index, day in enumerate(calendar):
        if index < warmup or (index - warmup) % parameters["rebalance_days"]:
            continue
        scored = []
        for sym in sorted(daily):
            if day not in daily[sym]:
                continue
            idx = bisect_right(days_by_symbol[sym], day) - 1
            score = _factor_score(sym, idx, rows_by_symbol, parameters)
            if score:
                scored.append(score)
        scored.sort(key=lambda row: (-row["score"], row["symbol"]))
        selected = scored[:parameters["top_n"]]
        weight = 1 / len(selected) if selected else 0.0
        targets[_day_timestamp(day)] = {row["symbol"]: weight for row in selected}
        log.append({"t": _day_timestamp(day), "signal_date": day, "selected": selected,
                    "target_weight": weight * max_position_pct, "candidate_count": len(scored)})
    start = _day_timestamp(calendar[warmup + 1])
    result = run_target_backtest(bars_by_symbol=rows_by_symbol, targets_by_t=targets,
                                 initial_cash=initial_cash, cost_model=cost_model,
                                 max_position_pct=max_position_pct, t_plus_one=t_plus_one,
                                 evaluation_start=start)
    baseline = run_target_backtest(bars_by_symbol=rows_by_symbol,
                                   targets_by_t={_day_timestamp(calendar[warmup]): {sym: 1 / len(daily) for sym in daily}},
                                   initial_cash=initial_cash, cost_model=cost_model,
                                   t_plus_one=t_plus_one, evaluation_start=start)
    benchmark = baseline["performance"].get("total_return_pct", 0.0)
    result["performance"].update({"benchmark_equal_weight_return_pct": benchmark,
                                  "excess_return_pct": round(result["performance"].get("total_return_pct", 0.0) - benchmark, 4)})
    result.update(result["performance"])
    result.update({"mode": "portfolio_factor", "strategy_id": "factor_rotation_topn",
                   "strategy_name": "多因子 TopN 轮动", "symbols": sorted(daily),
                   "params": parameters, "rebalance_log": log})
    return result


async def portfolio_factor_backtest_from_market(
    *, symbols: list[str], range_param: str, interval: str, params: dict[str, Any] | None,
    initial_cash: float, cost_model: dict[str, Any] | None = None,
    max_position_pct: float = 1.0, t_plus_one: bool = True,
) -> dict[str, Any]:
    _require_daily(interval)
    cleaned = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    if any(not (len(sym) == 9 and sym[:6].isdigit() and sym[6:] in (".SS", ".SH", ".SZ", ".BJ")) for sym in cleaned):
        raise ValueError("仅支持6位代码加.SS/.SH/.SZ/.BJ的A股")
    if not 2 <= len(cleaned) <= 50:
        raise ValueError("组合股票池须包含2至50只标的")
    sem = asyncio.Semaphore(5)
    async def limited(sym: str):
        async with sem:
            return await _fetch_symbol_bundle(sym, range_param, interval)
    fetched = await asyncio.gather(*(limited(sym) for sym in cleaned))
    errors = {sym: err or "空行情" for sym, bundle, err in fetched if err or not bundle or not bundle.get("candles")}
    # Never silently shrink the submitted universe after seeing which series fail.
    if errors:
        raise ValueError(f"股票池数据不完整，请先修复数据源或显式调整股票池: {errors}")
    bundles = {sym: bundle for sym, bundle, _ in fetched}
    result = run_portfolio_factor_backtest(bars_by_symbol={sym: bundle.get("candles") or [] for sym, bundle in bundles.items()},
                                           params=params, initial_cash=initial_cash, cost_model=cost_model,
                                           max_position_pct=max_position_pct, t_plus_one=t_plus_one)
    result.update({"range": range_param, "interval": interval, "unavailable_symbols": {},
                   "kline_sources": {sym: bundle.get("kline_source") for sym, bundle in bundles.items()}})
    for bundle in bundles.values():
        _merge_market_quality(result, bundle)
    return result


def _fallback_strategy_draft(goal: str, symbol: str) -> dict[str, Any]:
    goal_l = (goal or "").lower()
    if "rsi" in goal_l or "超卖" in goal:
        strategy_id, params = "rsi_reversal", {"period": 14, "lower": 30, "upper": 70}
    elif "回归" in goal or "atr" in goal_l:
        strategy_id, params = "ma_reversion_atr_stop", {"ma": 10, "atr": 14, "atr_multiplier": 2.0}
    else:
        strategy_id, params = "ma_cross", {"fast": 5, "slow": 20}
    return {
        "title": "模板化量化策略草案",
        "symbol": symbol,
        "strategy_id": strategy_id,
        "params": params,
        "hypothesis": goal or "用中低频技术信号验证趋势与风控效果。",
        "rules": [
            "使用日线收盘后可见信息生成目标仓位。",
            "下一交易日开盘按目标仓位调仓，启用 A 股 T+1 与交易成本。",
            "先用较长区间回测，再做参数敏感性和样本外验证。",
        ],
        "risk_controls": ["单标的最大仓位不超过 100%", "纳入滑点与最低佣金", "关注最大回撤与交易次数"],
        "joinquant_mapping": JOINQUANT_MODEL,
        "llm_used": False,
    }


def generate_strategy_draft(goal: str, symbol: str = "") -> dict[str, Any]:
    """让大模型给出策略草案；失败时回退到本地模板。"""
    fallback = _fallback_strategy_draft(goal, symbol)
    if not settings.any_llm_key_configured:
        return {**fallback, "llm_warning": "未配置大模型 Key，已使用本地模板草案。"}
    catalog = [{"id": s["id"], "name": s["name"], "params": s["params"], "description": s["description"]} for s in STRATEGY_CATALOG]
    messages = [
        {
            "role": "system",
            "content": (
                "你是A股量化研究助手。请只输出JSON对象，不要输出Markdown。"
                "不得给确定收益承诺；必须强调回测局限、成本、滑点、T+1和样本外验证。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "goal": goal,
                    "symbol": symbol,
                    "available_strategy_templates": catalog,
                    "joinquant_model": JOINQUANT_MODEL,
                    "required_keys": ["title", "strategy_id", "params", "hypothesis", "rules", "risk_controls", "validation_plan"],
                },
                ensure_ascii=False,
            ),
        },
    ]
    raw, err = llm_client.chat_completion_sync(messages, temperature=0.25, timeout=90.0)
    if err or not raw:
        return {**fallback, "llm_warning": err or "大模型空响应"}
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {**fallback, "llm_warning": "大模型返回非 JSON，已使用本地模板草案。", "raw_llm_text": raw[:2000]}
    if not isinstance(data, dict):
        return fallback
    sid = str(data.get("strategy_id") or fallback["strategy_id"])
    data["strategy_id"] = sid if any(s["id"] == sid for s in STRATEGY_CATALOG) else fallback["strategy_id"]
    data["params"] = _clamp_params(data["strategy_id"], data.get("params") if isinstance(data.get("params"), dict) else {})
    data["symbol"] = symbol
    data["joinquant_mapping"] = JOINQUANT_MODEL
    data["llm_used"] = True
    return data
