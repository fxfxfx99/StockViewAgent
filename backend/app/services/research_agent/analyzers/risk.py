"""多维风险清单（规则 + 分析器输出汇总）。"""
from __future__ import annotations

from typing import Any


def analyze_risks(
    bundle: dict[str, Any],
    price: dict[str, Any],
    valuation: dict[str, Any],
    news: dict[str, Any],
) -> dict[str, Any]:
    risks: list[str] = []

    if not (bundle.get("kline") or {}).get("candles"):
        risks.append("行情数据缺失或滞后，价格与波动判断不可靠。")

    v = price.get("volatility_annual_approx")
    if v is not None and v > 45:
        risks.append(f"估算波动率较高（约 {v}%），短期价格噪声与止损压力需重点关注。")

    if not (bundle.get("news") or []):
        risks.append("新闻与事件面信息不足，可能存在未入库的重大公开信息。")

    if not (bundle.get("issuer_chunks") or []):
        risks.append("缺乏上传财报/公告原文支撑，对 MD&A、风险因素、指引变化追踪不足。")

    pe = valuation.get("pe_ttm")
    if pe is not None and pe < 0:
        risks.append("市盈率为负或异常，可能反映亏损或一次性项目，估值锚不稳定。")
    if pe is not None and pe > 60:
        risks.append("估值倍数处于高位区间，对增长兑现度敏感度高（估值风险）。")

    neg_news = [e for e in (news.get("top_events") or []) if e.get("sentiment") == "负面"]
    if len(neg_news) >= 3:
        risks.append("近期负面归类新闻条数偏多，需核查是否反映持续性基本面问题。")

    risks.append("政策与监管、竞争格局、流动性与资金面等需结合行业与宏观另行跟踪（当前自动化覆盖有限）。")

    narrative = "主要风险维度（自动化草稿）:\n" + "\n".join(f"- {r}" for r in risks)
    return {"items": risks, "narrative": narrative}
