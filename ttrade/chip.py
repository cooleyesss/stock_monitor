# -*- coding: utf-8 -*-
"""筹码密集区(换手衰减筹码分布) + 滞涨形态: v3做T新因子(2026-11-11用户需求)
供 ttrade_v3(实盘) 与 bt_ttrade(回测) 镜像调用; 全局统一规则, 不逐股调参。

筹码密集区: 近N日日K -> 换手衰减筹码分布(真实流通盘未知, 以
  FLOAT_MULT×中位日成交量 近似流通盘, 中位换手≈1/FLOAT_MULT)
  -> 累计筹码≥PCT的最窄连续价格带[lo, hi]。
含义: 上沿=套牢盘强阻力(滞涨即反T卖点), 下沿=承接盘强支撑(放宽正T接回),
      内部=多空胶着无方向(不开新腿, 减少区间内反复小亏)。

滞涨: 最近n根5分钟K高点不再抬高 + 最新收阴(冲高乏力转弱, 反T最佳卖点,
  优于"等大乖离收阳"——后者常错过拐点)。
"""
from ttrade import config_ttrade as config


def chip_zone(bars, pct=None, days=None, buckets=240, float_mult=None):
    """bars: 升序日K(调用方保证不含当日); 返回{lo, hi}或None(数据不足/区间退化)
    最窄连续价格带(累计筹码≥pct): 双指针O(buckets), 日级缓存由调用方负责"""
    pct = config.V3_CHIP_PCT if pct is None else pct
    days = config.V3_CHIP_DAYS if days is None else days
    float_mult = (config.V3_CHIP_FLOAT_MULT if float_mult is None
                  else float_mult)
    bars = [b for b in bars if b.get("vol")]
    if len(bars) < max(days // 2, 20):        # 至少半窗口, 太短无统计意义
        return None
    bars = bars[-days:]
    lo_all = min(b["low"] for b in bars)
    hi_all = max(b["high"] for b in bars)
    if hi_all <= lo_all:
        return None
    step = (hi_all - lo_all) / buckets
    if step <= 0:
        return None
    vols = sorted(b["vol"] for b in bars)
    med = vols[len(vols) // 2] or 1.0
    cap = med * float_mult                     # 近似流通盘(单位=手, 比例尺无关)
    chips = [0.0] * buckets
    for b in bars:
        t = min(b["vol"] / cap, 0.50)          # 单日换手(50%上限防巨量日打穿)
        decay = 1.0 - t
        for i in range(buckets):
            chips[i] *= decay
        i0 = max(int((b["low"] - lo_all) / step), 0)
        i1 = min(int((b["high"] - lo_all) / step), buckets - 1)
        span = i1 - i0 + 1
        if span <= 0:
            continue
        per = b["vol"] / span                  # 当日筹码均匀分布到当日价格区间
        for i in range(i0, i1 + 1):
            chips[i] += per
    total = sum(chips)
    if total <= 0:
        return None
    need = total * pct
    acc, j = 0.0, 0
    bi, bj = 0, buckets - 1
    for i in range(buckets):
        while j < buckets and acc < need:
            acc += chips[j]
            j += 1
        if acc < need:
            break
        if (j - 1) - i < bj - bi:
            bi, bj = i, j - 1
        acc -= chips[i]
    return {"lo": lo_all + bi * step,
            "hi": lo_all + (bj + 1) * step}


def stall_shape(bars5, n=None):
    """滞涨形态: 最近n根5分钟高点均不高于前n根(不再创新高)且最新收阴
    bars5: 今日5分钟K(升序); 返回bool"""
    n = config.V3_STALL_N if n is None else n
    if len(bars5) < 2 * n:
        return False
    recent = bars5[-n:]
    prior = bars5[-2 * n:-n]
    if max(b["high"] for b in recent) >= max(b["high"] for b in prior):
        return False
    last = bars5[-1]
    return last["close"] < last["open"]
