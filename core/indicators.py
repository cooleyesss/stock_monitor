# -*- coding: utf-8 -*-
"""策略指标库: MA/布林/RSI(Wilder)/ATR/高低点 + build_ind统一打包
回测(backtest.daily)与实盘信号(core.adaptive)共用同一实现, 保证零漂移
bars格式: list[dict] 含 close/high/low/vol 键(与东财K线输出一致)
"""
import math


def sma(vals, n):
    out, s = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def calc_boll(closes, n=20, k=2.0):
    mid, up, lo = [None] * len(closes), [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        seg = closes[i - n + 1:i + 1]
        m = sum(seg) / n
        sd = math.sqrt(sum((c - m) ** 2 for c in seg) / n)
        mid[i], up[i], lo[i] = m, m + k * sd, m - k * sd
    return mid, up, lo


def calc_rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    k = 1.0 / n
    ag = al = None
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        ag = g if ag is None else g * k + ag * (1 - k)
        al = l if al is None else l * k + al * (1 - k)
        if i >= n:
            out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def calc_atr(bars, n=14):
    out = [None] * len(bars)
    trs, k, atr = [], 1.0 / n, None
    for i, b in enumerate(bars):
        pc = bars[i - 1]["close"] if i > 0 else b["close"]
        tr = max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc))
        trs.append(tr)
        if i == n:
            atr = sum(trs[1:n + 1]) / n
        elif i > n:
            atr = tr * k + atr * (1 - k)
        out[i] = atr
    return out


def hi_lo(closes, n):
    hi, lo = [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        seg = closes[i - n + 1:i + 1]
        hi[i], lo[i] = max(seg), min(seg)
    return hi, lo


def build_ind(bars):
    """打包全部指标: 下标与bars对齐, 前n-1根为None(未就绪)"""
    closes = [b["close"] for b in bars]
    vols = [b["vol"] for b in bars]
    mid, up, lo = calc_boll(closes, 20, 2.0)
    h20, l20 = hi_lo(closes, 20)
    vma5 = sma(vols, 5)
    vr = [None] * len(bars)
    for i in range(6, len(bars)):
        if vma5[i - 1]:
            vr[i] = vols[i - 1] / vma5[i - 1]
    return {"close": closes, "ma5": sma(closes, 5), "ma10": sma(closes, 10),
            "ma20": sma(closes, 20), "ma30": sma(closes, 30), "ma60": sma(closes, 60),
            "mid": mid, "up": up, "lo": lo,
            "rsi": calc_rsi(closes, 14), "atr": calc_atr(bars, 14),
            "h20": h20, "l20": l20, "vr": vr, "vol": vols}
