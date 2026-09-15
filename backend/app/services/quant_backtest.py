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
from collections import deque
from typing import Any

from app.config import settings
from app.services import kline_pipeline, llm_client

TRADING_DAYS = 252

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
        for i in range(1, len(bars)):
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
    decimals = int(cost.get("price_decimals") or 2)
    if stype == "tick":
        delta = _num(slippage.get("tick_size"), 0.01) * _num(slippage.get("tick_count"), 2)
        return round(price + delta if side == "buy" else price - delta, decimals)
    ratio = _num(slippage.get("ratio"), 0.001) / 2.0
    return round(price * (1 + ratio) if side == "buy" else price * (1 - ratio), decimals)


def _fees(symbol: str, price: float, shares: int, side: str, cost: dict[str, Any]) -> dict[str, float]:
    notional = price * shares
    commission = max(_num(cost.get("min_commission"), 5.0), notional * _num(cost.get("commission_rate"), 0.0003))
    stamp = notional * _num(cost.get("stamp_tax_rate"), 0.001) if side == "sell" else 0.0
    transfer = notional * _num(cost.get("transfer_fee_rate"), 0.00001) if symbol.endswith((".SS", ".SH")) else 0.0
    flow = _num(cost.get("flow_fee"), 0.0)
    return {
        "commission": round(commission, 4),
        "stamp_tax": round(stamp, 4),
        "transfer_fee": round(transfer, 4),
        "flow_fee": round(flow, 4),
        "total": round(commission + stamp + transfer + flow, 4),
    }


def _default_cost_model(cost_model: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "commission_rate": 0.0003,
        "min_commission": 5.0,
        "stamp_tax_rate": 0.001,
        "transfer_fee_rate": 0.00001,
        "flow_fee": 0.0,
        "price_decimals": 2,
        "slippage": {"type": "ratio", "ratio": 0.001, "tick_size": 0.01, "tick_count": 2},
    }
    for k, v in (cost_model or {}).items():
        if k == "slippage" and isinstance(v, dict):
            out["slippage"] = {**out["slippage"], **v}
        else:
            out[k] = v
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
    return base ** (TRADING_DAYS / n) - 1.0


def _performance(equity_curve: list[dict[str, Any]], trades: list[dict[str, Any]], initial_cash: float) -> dict[str, Any]:
    equities = [float(x["equity"]) for x in equity_curve]
    if not equities:
        return {}
    final = equities[-1]
    total_ret = final / initial_cash - 1.0
    returns = [(equities[i] / equities[i - 1] - 1.0) for i in range(1, len(equities)) if equities[i - 1] > 0]
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
    }


def run_backtest(
    *,
    candles: list[dict[str, Any]],
    strategy_id: str,
    params: dict[str, Any] | None,
    initial_cash: float,
    commission_rate: float | None = None,
    cost_model: dict[str, Any] | None = None,
    max_position_pct: float = 1.0,
    t_plus_one: bool = True,
    symbol: str = "",
) -> dict[str, Any]:
    if initial_cash <= 0:
        raise ValueError("initial_cash 须为正数")
    bars = sorted([b for b in candles if b.get("o") and b.get("c")], key=lambda x: float(x.get("t") or 0))
    if len(bars) < 20:
        raise ValueError("K 线数据不足，至少需要 20 根")
    sid = (strategy_id or "buy_and_hold").strip().lower()
    params = _clamp_params(sid, params)
    cost = _default_cost_model(cost_model)
    if commission_rate is not None:
        cost["commission_rate"] = commission_rate
    max_position_pct = max(0.0, min(1.0, float(max_position_pct or 1.0)))
    signals = _signal_weights(bars, sid, params)

    cash = float(initial_cash)
    shares = 0
    available_shares = 0
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    position_curve: list[dict[str, Any]] = []

    for i, bar in enumerate(bars):
        open_px = _num(bar.get("o"))
        close_px = _num(bar.get("c"))
        target_weight = min(max(_num(signals[i].get("target_weight"), 0.0), 0.0), max_position_pct)
        equity_before = cash + shares * open_px
        target_value = equity_before * target_weight
        target_shares = _lot_shares(target_value, open_px)
        delta = target_shares - shares
        reason = signals[i].get("reason") or "目标仓位调整"

        if delta > 0:
            px = _slip_price(open_px, "buy", cost)
            max_affordable = _lot_shares(cash - _num(cost.get("min_commission"), 5.0), px)
            vol = min(delta // 100 * 100, max_affordable)
            if vol >= 100:
                fee = _fees(symbol, px, vol, "buy", cost)
                gross = px * vol
                cash -= gross + fee["total"]
                shares += vol
                if not t_plus_one:
                    available_shares += vol
                trades.append({"t": int(bar["t"]), "side": "buy", "price": px, "volume": vol, "amount": round(gross, 2), "fee": fee, "reason": reason})
        elif delta < 0:
            sellable = available_shares if t_plus_one else shares
            vol = min((-delta) // 100 * 100, sellable // 100 * 100)
            if vol >= 100:
                px = _slip_price(open_px, "sell", cost)
                fee = _fees(symbol, px, vol, "sell", cost)
                gross = px * vol
                cash += gross - fee["total"]
                shares -= vol
                available_shares -= vol if t_plus_one else 0
                trades.append({"t": int(bar["t"]), "side": "sell", "price": px, "volume": vol, "amount": round(gross, 2), "fee": fee, "reason": reason})

        equity = cash + shares * close_px
        equity_curve.append({"t": int(bar["t"]), "equity": round(equity, 2), "cash": round(cash, 2), "shares": shares})
        position_curve.append({"t": int(bar["t"]), "target_weight": target_weight, "actual_weight": round((shares * close_px / equity) if equity > 0 else 0.0, 4), "reason": reason})
        if t_plus_one:
            available_shares = shares

    perf = _performance(equity_curve, trades, initial_cash)
    first_open = _num(bars[1].get("o"), _num(bars[0].get("o")))
    last_close = _num(bars[-1].get("c"))
    benchmark_ret = (last_close / first_open - 1.0) if first_open > 0 else 0.0
    perf["benchmark_buy_hold_return_pct"] = round(benchmark_ret * 100, 4)
    perf["excess_return_pct"] = round(perf.get("total_return_pct", 0.0) - perf["benchmark_buy_hold_return_pct"], 4)
    result = {
        "strategy_id": sid,
        "strategy_name": next((s["name"] for s in STRATEGY_CATALOG if s["id"] == sid), sid),
        "params": params,
        "initial_cash": round(initial_cash, 2),
        "cost_model": cost,
        "t_plus_one": bool(t_plus_one),
        "max_position_pct": max_position_pct,
        "performance": perf,
        "trades": trades,
        "equity_curve": equity_curve,
        "position_curve": position_curve,
        "bar_count": len(bars),
        "engine_notes": [
            "信号按日线收盘后可见信息生成，下一根 bar 开盘调仓。",
            "A 股按 100 股整手撮合；默认启用 T+1，卖出印花税、最低佣金、过户费和滑点纳入成本。",
            "当前版本为研究型单标的/单仓位回测，不执行任意用户 Python，不连接实盘账户。",
        ],
    }
    result.update(perf)
    return result


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
    bundle = await kline_pipeline.fetch_a_share_kline_with_fallbacks(sym, range_param, interval)
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
        }
    )
    return result


def _grid_values(definition: dict[str, Any], raw: Any) -> list[Any]:
    """Normalize a UI supplied grid while enforcing the strategy catalog bounds."""
    values = raw if isinstance(raw, list) else [raw]
    out: list[Any] = []
    for value in values:
        try:
            parsed = int(value) if definition.get("type") == "int" else float(value)
        except (TypeError, ValueError):
            continue
        parsed = max(definition.get("min", parsed), min(definition.get("max", parsed), parsed))
        if parsed not in out:
            out.append(parsed)
    return out[:12]


def optimize_backtest(
    *,
    candles: list[dict[str, Any]],
    strategy_id: str,
    param_grid: dict[str, Any] | None,
    initial_cash: float,
    cost_model: dict[str, Any] | None = None,
    max_position_pct: float = 1.0,
    t_plus_one: bool = True,
    symbol: str = "",
    train_ratio: float = 0.7,
    objective: str = "sharpe",
) -> dict[str, Any]:
    """Bounded grid search with chronological out-of-sample and cost stress tests."""
    catalog = next((s for s in STRATEGY_CATALOG if s["id"] == strategy_id), None)
    if not catalog or not catalog.get("params"):
        raise ValueError("该策略没有可优化参数")
    bars = sorted([b for b in candles if b.get("o") and b.get("c")], key=lambda x: float(x.get("t") or 0))
    if len(bars) < 80:
        raise ValueError("稳健性验证至少需要 80 根 K 线")
    train_ratio = max(0.55, min(0.85, float(train_ratio)))
    split = max(40, min(len(bars) - 20, int(len(bars) * train_ratio)))
    definitions = {p["key"]: p for p in catalog["params"]}
    keys: list[str] = []
    axes: list[list[Any]] = []
    for key, definition in definitions.items():
        values = _grid_values(definition, (param_grid or {}).get(key, definition["default"]))
        keys.append(key)
        axes.append(values or [definition["default"]])
    combinations = list(itertools.islice(itertools.product(*axes), 60))
    metric_key = "total_return_pct" if objective == "return" else "sharpe"
    rows: list[dict[str, Any]] = []
    for values in combinations:
        params = dict(zip(keys, values, strict=True))
        train = run_backtest(
            candles=bars[:split], strategy_id=strategy_id, params=params, initial_cash=initial_cash,
            cost_model=cost_model, max_position_pct=max_position_pct, t_plus_one=t_plus_one, symbol=symbol,
        )
        rows.append({"params": train["params"], "train": train["performance"]})
    rows.sort(key=lambda row: float(row["train"].get(metric_key) or 0), reverse=True)
    finalists = rows[: min(10, len(rows))]
    test_bars = bars[max(0, split - 30):]
    for row in finalists:
        test = run_backtest(
            candles=test_bars, strategy_id=strategy_id, params=row["params"], initial_cash=initial_cash,
            cost_model=cost_model, max_position_pct=max_position_pct, t_plus_one=t_plus_one, symbol=symbol,
        )
        row["test"] = test["performance"]
        train_score = float(row["train"].get(metric_key) or 0)
        test_score = float(row["test"].get(metric_key) or 0)
        row["stability_score"] = round(test_score - abs(train_score - test_score) * 0.25, 4)
    finalists.sort(key=lambda row: row["stability_score"], reverse=True)
    best = finalists[0]
    stress: list[dict[str, Any]] = []
    base_cost = _default_cost_model(cost_model)
    for multiple in (0.0, 1.0, 2.0):
        stressed = {**base_cost, "commission_rate": base_cost["commission_rate"] * multiple}
        stressed["slippage"] = {**base_cost["slippage"], "ratio": base_cost["slippage"]["ratio"] * multiple}
        result = run_backtest(
            candles=test_bars, strategy_id=strategy_id, params=best["params"], initial_cash=initial_cash,
            cost_model=stressed, max_position_pct=max_position_pct, t_plus_one=t_plus_one, symbol=symbol,
        )
        stress.append({"cost_multiple": multiple, "performance": result["performance"]})
    return {
        "mode": "robustness_lab", "strategy_id": strategy_id, "symbol": symbol,
        "objective": objective, "train_ratio": train_ratio, "split_timestamp": int(bars[split]["t"]),
        "combination_count": len(combinations), "best_params": best["params"], "best": best,
        "candidates": finalists, "cost_stress": stress,
        "notes": [
            "参数仅在较早的样本内区间排序，较新的区间用于样本外复核。",
            "样本外测试额外保留 30 根预热 K 线供指标计算，绩效仍应结合交易明细判断。",
            "成本压力测试覆盖零成本、基准成本和双倍成本；稳定不等于未来盈利。",
        ],
    }


async def optimize_backtest_from_market(**kwargs: Any) -> dict[str, Any]:
    sym = str(kwargs.pop("symbol", "")).strip().upper()
    range_param = str(kwargs.pop("range_param", "2y"))
    interval = str(kwargs.pop("interval", "1d"))
    bundle = await kline_pipeline.fetch_a_share_kline_with_fallbacks(sym, range_param, interval)
    result = optimize_backtest(candles=bundle.get("candles") or [], symbol=sym, **kwargs)
    result.update({"range": range_param, "interval": interval, "kline_source": bundle.get("kline_source")})
    return result


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
        bundle = await kline_pipeline.fetch_a_share_kline_with_fallbacks(sym, range_param, interval)
        return sym, bundle, None
    except Exception as exc:  # noqa: BLE001
        return sym, None, str(exc)


async def portfolio_factor_backtest_from_market(
    *,
    symbols: list[str],
    range_param: str,
    interval: str,
    params: dict[str, Any] | None,
    initial_cash: float,
    cost_model: dict[str, Any] | None = None,
    max_position_pct: float = 1.0,
    t_plus_one: bool = True,
) -> dict[str, Any]:
    if initial_cash <= 0:
        raise ValueError("initial_cash 须为正数")
    cleaned: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        sym = str(s or "").strip().upper()
        if not sym or sym in seen:
            continue
        if not (len(sym) == 9 and sym[:6].isdigit() and sym[6:] in (".SS", ".SH", ".SZ", ".BJ")):
            continue
        seen.add(sym)
        cleaned.append(sym)
    if len(cleaned) < 2:
        raise ValueError("组合因子回测至少需要 2 只 A 股标的")
    cleaned = cleaned[:50]
    params2 = _clamp_portfolio_params(params, len(cleaned))
    cost = _default_cost_model(cost_model)
    max_position_pct = max(0.0, min(1.0, float(max_position_pct or 1.0)))

    sem = asyncio.Semaphore(5)

    async def limited(sym: str):
        async with sem:
            return await _fetch_symbol_bundle(sym, range_param, interval)

    fetched = await asyncio.gather(*(limited(sym) for sym in cleaned))
    bars_by_t: dict[str, dict[int, dict[str, Any]]] = {}
    kline_sources: dict[str, str] = {}
    errors: dict[str, str] = {}
    for sym, bundle, err in fetched:
        if err or not bundle:
            errors[sym] = err or "空数据"
            continue
        rows = sorted(bundle.get("candles") or [], key=lambda x: int(x.get("t") or 0))
        rows = [r for r in rows if r.get("o") and r.get("c")]
        if len(rows) < 80:
            errors[sym] = f"K 线不足：{len(rows)}"
            continue
        bars_by_t[sym] = {int(r["t"]): r for r in rows}
        kline_sources[sym] = str(bundle.get("kline_source") or "")
    if len(bars_by_t) < 2:
        raise ValueError(f"可用标的不足：{errors}")
    common_ts: set[int] | None = None
    for rows in bars_by_t.values():
        keys = set(rows.keys())
        common_ts = keys if common_ts is None else common_ts & keys
    ts = sorted(common_ts or [])
    if len(ts) < 80:
        raise ValueError("股票池共同交易日不足，无法做组合因子回测")
    bars_by_symbol: dict[str, list[dict[str, Any]]] = {
        sym: [rows[t] for t in ts] for sym, rows in bars_by_t.items()
    }
    symbols_ok = sorted(bars_by_symbol)

    cash = float(initial_cash)
    shares: dict[str, int] = {sym: 0 for sym in symbols_ok}
    available: dict[str, int] = {sym: 0 for sym in symbols_ok}
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    position_curve: list[dict[str, Any]] = []
    rebalance_log: list[dict[str, Any]] = []
    prev_targets: dict[str, float] = {sym: 0.0 for sym in symbols_ok}
    warmup = max(params2["momentum_lookback"], params2["trend_ma"], params2["volatility_lookback"], 20) + 1

    for i, t in enumerate(ts):
        close_prices = {sym: _num(bars_by_symbol[sym][i].get("c")) for sym in symbols_ok}
        open_prices = {sym: _num(bars_by_symbol[sym][i].get("o")) for sym in symbols_ok}
        is_rebalance = i >= warmup and ((i - warmup) % int(params2["rebalance_days"]) == 0)
        targets = dict(prev_targets)
        if is_rebalance:
            scored = [
                sc
                for sym in symbols_ok
                if (sc := _factor_score(sym, i - 1, bars_by_symbol, params2)) is not None
            ]
            scored.sort(key=lambda x: (-x["score"], x["symbol"]))
            selected = scored[: int(params2["top_n"])]
            weight = max_position_pct / len(selected) if selected else 0.0
            targets = {sym: 0.0 for sym in symbols_ok}
            for row in selected:
                targets[row["symbol"]] = weight
            rebalance_log.append(
                {
                    "t": int(t),
                    "selected": selected,
                    "target_weight": round(weight, 4),
                    "candidate_count": len(scored),
                }
            )
            prev_targets = dict(targets)

        equity_open = cash + sum(shares[sym] * open_prices[sym] for sym in symbols_ok)
        # 先卖后买，释放现金。
        for side in ("sell", "buy"):
            for sym in symbols_ok:
                px0 = open_prices[sym]
                if px0 <= 0:
                    continue
                target_shares = _lot_shares(equity_open * targets.get(sym, 0.0), px0)
                delta = target_shares - shares[sym]
                if side == "sell" and delta < 0:
                    sellable = available[sym] if t_plus_one else shares[sym]
                    vol = min((-delta) // 100 * 100, sellable // 100 * 100)
                    if vol >= 100:
                        px = _slip_price(px0, "sell", cost)
                        fee = _fees(sym, px, vol, "sell", cost)
                        gross = px * vol
                        cash += gross - fee["total"]
                        shares[sym] -= vol
                        available[sym] -= vol if t_plus_one else 0
                        trades.append({"t": int(t), "symbol": sym, "side": "sell", "price": px, "volume": vol, "amount": round(gross, 2), "fee": fee, "reason": "因子调仓"})
                elif side == "buy" and delta > 0:
                    px = _slip_price(px0, "buy", cost)
                    max_affordable = _lot_shares(cash - _num(cost.get("min_commission"), 5.0), px)
                    vol = min(delta // 100 * 100, max_affordable)
                    if vol >= 100:
                        fee = _fees(sym, px, vol, "buy", cost)
                        gross = px * vol
                        cash -= gross + fee["total"]
                        shares[sym] += vol
                        if not t_plus_one:
                            available[sym] += vol
                        trades.append({"t": int(t), "symbol": sym, "side": "buy", "price": px, "volume": vol, "amount": round(gross, 2), "fee": fee, "reason": "因子调仓"})

        equity = cash + sum(shares[sym] * close_prices[sym] for sym in symbols_ok)
        equity_curve.append({"t": int(t), "equity": round(equity, 2), "cash": round(cash, 2), "holding_count": sum(1 for v in shares.values() if v > 0)})
        position_curve.append(
            {
                "t": int(t),
                "actual_weight": round(sum(shares[sym] * close_prices[sym] for sym in symbols_ok) / equity, 4) if equity > 0 else 0,
                "holding_count": sum(1 for v in shares.values() if v > 0),
            }
        )
        if t_plus_one:
            available = dict(shares)

    perf = _performance(equity_curve, trades, initial_cash)
    # 等权买入持有基准：共同交易日起点到终点的平均收益。
    bench_rets = []
    for sym in symbols_ok:
        first = _num(bars_by_symbol[sym][1].get("o"), _num(bars_by_symbol[sym][0].get("o")))
        last = _num(bars_by_symbol[sym][-1].get("c"))
        if first > 0:
            bench_rets.append(last / first - 1.0)
    benchmark = sum(bench_rets) / len(bench_rets) if bench_rets else 0.0
    perf["benchmark_equal_weight_return_pct"] = round(benchmark * 100, 4)
    perf["excess_return_pct"] = round(perf.get("total_return_pct", 0.0) - perf["benchmark_equal_weight_return_pct"], 4)
    result = {
        "mode": "portfolio_factor",
        "strategy_id": "factor_rotation_topn",
        "strategy_name": "多因子 TopN 轮动",
        "symbols": symbols_ok,
        "unavailable_symbols": errors,
        "range": range_param,
        "interval": interval,
        "params": params2,
        "initial_cash": round(initial_cash, 2),
        "cost_model": cost,
        "t_plus_one": bool(t_plus_one),
        "max_position_pct": max_position_pct,
        "performance": perf,
        "trades": trades,
        "equity_curve": equity_curve,
        "position_curve": position_curve,
        "rebalance_log": rebalance_log,
        "kline_sources": kline_sources,
        "bar_count": len(ts),
        "engine_notes": [
            "股票池内用上一交易日收盘前可见数据计算多因子分，下一交易日开盘调仓，避免未来函数。",
            "默认因子：中期动量、价格相对均线趋势、低波动、量能变化；按综合分选择 TopN 等权持有。",
            "当前为研究型多标的组合回测，适合验证因子方向、调仓频率与成本敏感性，不连接实盘账户。",
        ],
    }
    result.update(perf)
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
