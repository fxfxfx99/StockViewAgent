"""基于 K 线 OHLCV 调用 MyTT 指标，输出与 candles 等长的 JSON 友好序列。"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from app.services import mytt_lib as mt

MYTT_MAX_SERIES_PER_REQUEST = 16

MYTT_CATALOG: list[dict[str, Any]] = [
    {"id": "ma5", "title": "MA5"},
    {"id": "ma10", "title": "MA10"},
    {"id": "ma20", "title": "MA20"},
    {"id": "ma60", "title": "MA60"},
    {"id": "macd", "title": "MACD"},
    {"id": "kdj", "title": "KDJ"},
    {"id": "rsi", "title": "RSI(24)"},
    {"id": "boll", "title": "BOLL(20,2)"},
    {"id": "bias", "title": "BIAS"},
    {"id": "atr", "title": "ATR(20)"},
    {"id": "wr", "title": "W&R"},
    {"id": "psy", "title": "PSY"},
    {"id": "cci", "title": "CCI(14)"},
    {"id": "bbi", "title": "BBI"},
    {"id": "dmi", "title": "DMI"},
    {"id": "trix", "title": "TRIX"},
    {"id": "dma", "title": "DMA"},
    {"id": "mtm", "title": "MTM"},
    {"id": "roc", "title": "ROC"},
    {"id": "brar", "title": "BRAR"},
    {"id": "vr", "title": "VR(26)"},
    {"id": "emv", "title": "EMV"},
    {"id": "dpo", "title": "DPO"},
    {"id": "taq", "title": "唐安奇通道(20)"},
]


def _to_json_list(a: Any) -> list[float | None]:
    if a is None:
        return []
    out: list[float | None] = []
    for x in np.asarray(a, dtype=float).ravel().tolist():
        if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
            out.append(None)
        elif isinstance(x, (float, int)):
            out.append(round(float(x), 6))
        else:
            out.append(None)
    return out


def candles_to_ohlcv(candles: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    n = len(candles)
    o = np.zeros(n, dtype=float)
    h = np.zeros(n, dtype=float)
    low = np.zeros(n, dtype=float)
    c = np.zeros(n, dtype=float)
    v = np.zeros(n, dtype=float)
    for i, row in enumerate(candles):
        o[i] = float(row.get("o") or 0)
        h[i] = float(row.get("h") or 0)
        low[i] = float(row.get("l") or 0)
        c[i] = float(row.get("c") or 0)
        v[i] = float(row.get("v") or 0)
    return o, h, low, c, v


def compute_series(candles: list[dict[str, Any]], names: list[str]) -> tuple[dict[str, Any], list[str]]:
    """
    names：指标 id（小写）；返回 (series_dict, unknown_ids)。
    每个指标值为 dict[str, list]，子键为曲线名。
    """
    unknown: list[str] = []
    if len(candles) < 5:
        return {}, list(names)

    O, H, L, C, V = candles_to_ohlcv(candles)
    allowed_ids = {x["id"] for x in MYTT_CATALOG}
    series: dict[str, Any] = {}

    for raw in names:
        key = (raw or "").strip().lower()
        if not key or key not in allowed_ids:
            if raw and raw.strip():
                unknown.append(raw.strip())
            continue
        try:
            if key == "ma5":
                series[key] = {"ma5": _to_json_list(mt.MA(C, 5))}
            elif key == "ma10":
                series[key] = {"ma10": _to_json_list(mt.MA(C, 10))}
            elif key == "ma20":
                series[key] = {"ma20": _to_json_list(mt.MA(C, 20))}
            elif key == "ma60":
                series[key] = {"ma60": _to_json_list(mt.MA(C, 60))}
            elif key == "macd":
                dif, dea, hist = mt.MACD(C)
                series[key] = {
                    "dif": _to_json_list(dif),
                    "dea": _to_json_list(dea),
                    "histogram": _to_json_list(hist),
                }
            elif key == "kdj":
                k, d, j = mt.KDJ(C, H, L)
                series[key] = {"k": _to_json_list(k), "d": _to_json_list(d), "j": _to_json_list(j)}
            elif key == "rsi":
                series[key] = {"rsi": _to_json_list(mt.RSI(C, 24))}
            elif key == "boll":
                up, mid, lo = mt.BOLL(C, 20, 2)
                series[key] = {
                    "upper": _to_json_list(up),
                    "mid": _to_json_list(mid),
                    "lower": _to_json_list(lo),
                }
            elif key == "bias":
                b1, b2, b3 = mt.BIAS(C, 6, 12, 24)
                series[key] = {
                    "bias6": _to_json_list(b1),
                    "bias12": _to_json_list(b2),
                    "bias24": _to_json_list(b3),
                }
            elif key == "atr":
                series[key] = {"atr": _to_json_list(mt.ATR(C, H, L, 20))}
            elif key == "wr":
                w1, w2 = mt.WR(C, H, L, 10, 6)
                series[key] = {"wr10": _to_json_list(w1), "wr6": _to_json_list(w2)}
            elif key == "psy":
                p, pm = mt.PSY(C, 12, 6)
                series[key] = {"psy": _to_json_list(p), "psyma": _to_json_list(pm)}
            elif key == "cci":
                series[key] = {"cci": _to_json_list(mt.CCI(C, H, L, 14))}
            elif key == "bbi":
                series[key] = {"bbi": _to_json_list(mt.BBI(C))}
            elif key == "dmi":
                pdi, mdi, adx, adxr = mt.DMI(C, H, L, 14, 6)
                series[key] = {
                    "pdi": _to_json_list(pdi),
                    "mdi": _to_json_list(mdi),
                    "adx": _to_json_list(adx),
                    "adxr": _to_json_list(adxr),
                }
            elif key == "trix":
                tx, tma = mt.TRIX(C, 12, 20)
                series[key] = {"trix": _to_json_list(tx), "trma": _to_json_list(tma)}
            elif key == "dma":
                dif, dma = mt.DMA(C, 10, 50, 10)
                series[key] = {"dif": _to_json_list(dif), "difma": _to_json_list(dma)}
            elif key == "mtm":
                m, mm = mt.MTM(C, 12, 6)
                series[key] = {"mtm": _to_json_list(m), "mtmma": _to_json_list(mm)}
            elif key == "roc":
                r, rm = mt.ROC(C, 12, 6)
                series[key] = {"roc": _to_json_list(r), "maroc": _to_json_list(rm)}
            elif key == "brar":
                ar, br = mt.BRAR(O, C, H, L, 26)
                series[key] = {"ar": _to_json_list(ar), "br": _to_json_list(br)}
            elif key == "vr":
                series[key] = {"vr": _to_json_list(mt.VR(C, V, 26))}
            elif key == "emv":
                e, ema = mt.EMV(H, L, V, 14, 9)
                series[key] = {"emv": _to_json_list(e), "maemv": _to_json_list(ema)}
            elif key == "dpo":
                d, md = mt.DPO(C, 20, 10, 6)
                series[key] = {"dpo": _to_json_list(d), "madpo": _to_json_list(md)}
            elif key == "taq":
                up, mid, dn = mt.TAQ(H, L, 20)
                series[key] = {
                    "upper": _to_json_list(up),
                    "mid": _to_json_list(mid),
                    "lower": _to_json_list(dn),
                }
        except Exception as e:
            series[key] = {"_error": str(e)[:240]}

    return series, unknown


def parse_series_param(series_csv: str) -> list[str]:
    parts = [x.strip().lower() for x in (series_csv or "").split(",") if x.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
        if len(out) >= MYTT_MAX_SERIES_PER_REQUEST:
            break
    return out
