"""回测引擎单元测试（不依赖网络）。"""
from app.services.quant_backtest import optimize_backtest, run_backtest


def _candles(n: int, base: float = 10.0):
    out = []
    t = 1700000000
    for i in range(n):
        c = base + i * 0.05
        out.append({"t": t + i * 86400, "o": c, "h": c + 0.1, "l": c - 0.1, "c": c, "v": 1e6})
    return out


def test_buy_and_hold():
    c = _candles(30, 100)
    r = run_backtest(
        candles=c,
        strategy_id="buy_and_hold",
        params={},
        initial_cash=100_000,
        commission_rate=0.0003,
    )
    assert r["bar_count"] == 30
    assert len(r["trades"]) == 1
    assert r["trades"][0]["side"] == "buy"
    assert r["final_equity"] > 0


def test_ma_cross_runs():
    c = _candles(120, 50)
    r = run_backtest(
        candles=c,
        strategy_id="ma_cross",
        params={"fast": 5, "slow": 20},
        initial_cash=500_000,
        commission_rate=0.0003,
    )
    assert r["bar_count"] == 120
    assert "max_drawdown_pct" in r
    assert len(r["equity_curve"]) >= 100


def test_optimize_has_out_of_sample_and_cost_stress():
    candles = _candles(180, 30)
    result = optimize_backtest(
        candles=candles,
        strategy_id="ma_cross",
        param_grid={"fast": [3, 5], "slow": [15, 20]},
        initial_cash=100_000,
        symbol="600000.SS",
    )
    assert result["combination_count"] == 4
    assert result["best_params"]
    assert "test" in result["best"]
    assert [row["cost_multiple"] for row in result["cost_stress"]] == [0.0, 1.0, 2.0]
