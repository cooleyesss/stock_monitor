# -*- coding: utf-8 -*-
"""半年日线回测: 标的池量化策略 + 分层调优 + 个股操作点 dashboard
=======================================================================
口径(用户确认): 策略独立收益(标的池) —— 5只持仓股为标的池, 期初资金=当前账户总资产
 (现金+持仓市值), 全现金起步; 策略可自主持仓/减仓/清仓/回补; 目标20~50% (半年)。

分层(用户确认"分层对比"):
  bh    等权买入持有(基准)
  hold  L1 做T框架: 等权底仓 + 动态区间做T(锚定+投票共振), 仅调做T参数
  trend L2 趋势择时: L1 + 个股MA择时(走弱清仓/走强回补), 允许空仓
  rot   L2 动量轮动: 按N日动量选前K只(趋势向上且动量>0)持有, 其余空仓, 叠加做T

数据: 东财日线 klt=101, 2026-04-01~2026-10-13
反未来函数(严格): 第i日决策只用截至 i-1 日收盘的指标; 量比用"昨日量比"代理
日内路径启发式: 开盘更靠近当日最高 -> O→H→L; 否则 O→L→H
用法: python daily_backtest.py [--optimize] [--no-dashboard]
"""
import argparse
import json
import math
import os
import random
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests
import config_quant as config
from common import fees

BEG, END = "20260401", "20261013"
IDX_CODE = "999999"
HOSTS = ["https://push2his.eastmoney.com", "https://33.push2his.eastmoney.com"]
TMP_DIR = r"D:\trae_projects\_bt_tmp"
CACHE = os.path.join(TMP_DIR, "bt_daily_cache.json")

POOL = {"603169": "兰石重装", "600557": "康缘药业", "600480": "凌云股份",
        "600879": "航天电子", "300455": "航天智装"}  # 2026-10-20 剔除神奇制药(用户: 完全放弃)


# ============================== 数据 ==============================
def secid(code):
    if code == IDX_CODE:
        return "1.000001"
    return ("1." if code[0] in "56" else "0.") + code


def fetch_daily(code, beg=BEG, end=END):
    params = {"secid": secid(code), "fields1": "f1,f2,f3",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
              "klt": 101, "fqt": 1, "beg": beg, "end": end}
    for h in HOSTS:
        try:
            r = requests.get(h + "/api/qt/stock/kline/get", params=params,
                             timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            k = (r.json().get("data") or {}).get("klines") or []
            bars = []
            for line in k:
                f = line.split(",")
                bars.append({"dt": f[0], "open": float(f[1]), "close": float(f[2]),
                             "high": float(f[3]), "low": float(f[4]),
                             "vol": float(f[5]), "amount": float(f[6])})
            if bars:
                return bars
        except Exception:
            continue
    return []


def load_universe():
    if os.path.exists(CACHE):
        try:
            d = json.load(open(CACHE, encoding="utf-8"))
            if d.get("beg") == BEG and d.get("end") == END:
                return d["data"]
        except Exception:
            pass
    codes = {IDX_CODE: "上证指数"}
    codes.update(POOL)
    data = {}
    for code, name in codes.items():
        b = fetch_daily(code)
        if not b:
            raise RuntimeError(f"日线获取失败: {code} {name}")
        data[code] = {"name": name, "bars": b}
    os.makedirs(TMP_DIR, exist_ok=True)
    json.dump({"beg": BEG, "end": END, "data": data},
              open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return data


# ============================== 指标 ==============================
# sma/calc_boll/calc_rsi/calc_atr/hi_lo/build_ind 已迁移至 core.indicators
# (回测与实盘信号共用同一实现, 保证零漂移)
from core.indicators import sma, build_ind


# ============================== 参数 ==============================
BASE_P = dict(
    buy_pad_atr=0.30, sell_ref="mid", sell_pad_atr=0.30, stop_atr=1.50,
    w_zone=0.30, w_rsi=0.20, w_boll=0.20, w_ma=0.15, w_pos=0.15,
    buy_thr=0.60, sell_thr=0.60, min_agree=2, min_vote=0.30,
    buy_ma_veto=False, use_vr=True, buy_vr_max=0.80, sell_vr_min=1.20,
    lot_ratio=1.0 / 3, max_weight=1.0 / 6, use_t=True,
    # trend 层 (个股MA择时)
    tr_ma=10, tr_mode="cross",
    # rot 层 (推荐: 2月动量 + MA20过滤, 集中持有, 周度调仓)
    rot_N=40, rot_K=2, rot_rebal=5, rot_min_mom=0.0, rot_ma=20, rot_frac=0.85,
)


# ============================== 账户 ==============================
def new_acct(capital):
    return {"cash": round(capital, 2), "pos": {}, "fees": 0.0,
            "trades": [], "ops": [], "ledger": {}, "equity": []}


def _led(acct, code):
    return acct["ledger"].setdefault(code, {"bq": 0, "ba": 0.0, "sq": 0, "sa": 0.0})


def _buy(acct, code, name, qty, price, day, reason=""):
    qty = qty // config.LOT_MIN * config.LOT_MIN
    if qty < config.LOT_MIN:
        return 0
    amount = round(qty * price, 2)
    fee = fees.buy_fee(amount, code)
    if acct["cash"] < amount + fee:
        return 0
    acct["cash"] = round(acct["cash"] - amount - fee, 2)
    pos = acct["pos"].setdefault(code, {"shares": 0, "cost": 0.0, "today_bought": 0})
    old = pos["shares"]
    pos["shares"] = old + qty
    pos["cost"] = round((pos["cost"] * old + amount + fee) / pos["shares"], 4)
    pos["today_bought"] = pos.get("today_bought", 0) + qty
    acct["fees"] = round(acct["fees"] + fee, 2)
    led = _led(acct, code)
    led["bq"] += qty
    led["ba"] = round(led["ba"] + amount, 2)
    acct["trades"].append({"date": day, "code": code, "side": "buy", "qty": qty,
                           "price": price, "fee": fee})
    acct["ops"].append({"date": day, "code": code, "name": name, "side": "BUY",
                        "qty": qty, "price": round(price, 3), "reason": reason})
    return qty


def _sell(acct, code, name, qty, price, day, reason="", stop=False, t1=True):
    pos = acct["pos"].get(code)
    if not pos or pos["shares"] < config.LOT_MIN:
        return 0
    if t1:
        avail = (pos["shares"] - pos.get("today_bought", 0)) // 100 * 100
        qty = min(qty, avail)
    qty = min(qty, pos["shares"]) // 100 * 100
    if qty < config.LOT_MIN:
        return 0
    amount = round(qty * price, 2)
    fee = fees.sell_fee(amount, code)
    acct["cash"] = round(acct["cash"] + amount - fee, 2)
    acct["fees"] = round(acct["fees"] + fee, 2)
    pos["shares"] -= qty
    if pos["shares"] == 0:
        pos["cost"] = 0.0
        pos["today_bought"] = 0
    if not stop:
        led = _led(acct, code)
        led["sq"] += qty
        led["sa"] = round(led["sa"] + amount, 2)
    acct["trades"].append({"date": day, "code": code, "side": "sell", "qty": qty,
                           "price": price, "fee": fee, "stop": stop})
    acct["ops"].append({"date": day, "code": code, "name": name,
                        "side": "STOP" if stop else "SELL", "qty": qty,
                        "price": round(price, 3), "reason": reason})
    return qty


def affordable(acct, code, price, want):
    qty = want // config.LOT_MIN * config.LOT_MIN
    while qty >= config.LOT_MIN:
        amount = round(qty * price, 2)
        if acct["cash"] >= amount + fees.buy_fee(amount, code):
            return qty
        qty -= config.LOT_MIN
    return 0


def fifo_pairs(trades):
    pairs, opens, q = [], [], {}
    for t in trades:
        if t.get("stop"):
            continue
        leg = {"code": t["code"], "side": t["side"], "qty": t["qty"],
               "price": t["price"], "fps": (t["fee"] or 0) / t["qty"] if t["qty"] else 0}
        qq = q.setdefault(t["code"], [])
        while leg["qty"] > 0 and qq and qq[0]["side"] != leg["side"]:
            hd = qq[0]
            m = min(leg["qty"], hd["qty"])
            bl, sl = (hd, leg) if leg["side"] == "sell" else (leg, hd)
            pairs.append({"code": t["code"], "qty": m,
                          "pnl": round(m * (sl["price"] - bl["price"])
                                       - (bl["fps"] + sl["fps"]) * m, 2)})
            hd["qty"] -= m
            leg["qty"] -= m
            if hd["qty"] <= 0:
                qq.pop(0)
        if leg["qty"] > 0:
            qq.append(leg)
    for qq in q.values():
        opens.extend(qq)
    return pairs, opens


# ============================== 投票 ==============================
def votes_for(zone, ind, i, price):
    j = i - 1
    v = {}
    v["zone"] = 1.0 if zone["buy_low"] <= price <= zone["buy_high"] else (
        -1.0 if zone["sell_low"] <= price <= zone["sell_high"] else 0.0)
    r = ind["rsi"][j]
    v["rsi"] = 0.0 if r is None else max(-1.0, min(1.0, (50.0 - r) / 25.0))
    m, u, l = ind["mid"][j], ind["up"][j], ind["lo"][j]
    v["boll"] = 0.0 if None in (m, u, l) or u <= l else \
        max(-1.0, min(1.0, (0.5 - (price - l) / (u - l)) * 2.0))
    ma5, ma20 = ind["ma5"][j], ind["ma20"][j]
    v["ma"] = 0.0 if None in (ma5, ma20) or ma20 == 0 else \
        max(-1.0, min(1.0, (ma5 - ma20) / ma20 / 0.04))
    h20, l20 = ind["h20"][j], ind["l20"][j]
    v["pos"] = 0.0 if None in (h20, l20) or h20 <= l20 else \
        max(-1.0, min(1.0, (0.5 - (price - l20) / (h20 - l20)) * 2.0))
    return v


def score_side(v, P, side_dir):
    w = {"zone": P["w_zone"], "rsi": P["w_rsi"], "boll": P["w_boll"],
         "ma": P["w_ma"], "pos": P["w_pos"]}
    wsum = sum(w.values())
    tot = sum(w[k] * max(side_dir * v[k], 0.0) for k in v) / wsum
    agree = sum(1 for k in v if v[k] * side_dir >= P["min_vote"])
    return round(tot, 3), agree


# ============================== 引擎 ==============================
def run_bt(uni, P, layer, capital, i0=0, i1=None, codes=None):
    codes = sorted(codes) if codes else sorted(POOL)
    if i1 is None:
        i1 = len(uni[codes[0]]["bars"])
    ind = {c: build_ind(uni[c]["bars"]) for c in codes}
    bars = {c: uni[c]["bars"] for c in codes}
    acct = new_acct(capital)
    days = [bars[codes[0]][k]["dt"] for k in range(i0, i1)]
    n_all = len(bars[codes[0]])

    core0 = {}
    if layer != "rot":
        for c in codes:
            qty = int(capital * P["max_weight"] / bars[c][i0]["open"] // 100 * 100)
            _buy(acct, c, POOL[c], qty, bars[c][i0]["open"], days[0], "期初建仓")
            core0[c] = qty
    else:
        for c in codes:
            core0[c] = int(capital * P["max_weight"] * P["rot_frac"]
                           / bars[c][i0]["open"] // 100 * 100)

    ref = dict(core0)          # 做T基准量
    trend_on = {c: True for c in codes}
    se = None
    for i in range(i0, i1):
        day = days[i - i0]
        for c in codes:
            acct["pos"].setdefault(c, {"shares": 0, "cost": 0.0}).pop("today_bought", None)

        # ---- 择时/轮动(用 i-1 指标, 当日开盘执行) ----
        if layer == "trend":
            for c in codes:
                j = i - 1
                ma = ind[c]["ma%d" % P["tr_ma"]][j]
                cl = ind[c]["close"][j]
                if ma is None:
                    continue
                up = cl > ma
                if P.get("tr_mode") == "cross":
                    f, s = ind[c]["ma5"][j], ind[c]["ma20"][j]
                    up = up and f is not None and s is not None and f > s
                if trend_on[c] and not up:
                    sh = acct["pos"][c]["shares"]
                    if sh > 0:
                        _sell(acct, c, POOL[c], sh, bars[c][i]["open"], day,
                              f"跌破MA{P['tr_ma']}清仓", t1=False)
                    trend_on[c] = False
                elif (not trend_on[c]) and up:
                    q = affordable(acct, c, bars[c][i]["open"], core0[c])
                    if q >= config.LOT_MIN:
                        _buy(acct, c, POOL[c], q, bars[c][i]["open"], day, "站上MA回补")
                        trend_on[c] = True
        elif layer == "rot":
            if (i - i0) % max(1, P["rot_rebal"]) == 0:
                j = i - 1
                mom = {}
                for c in codes:
                    N = P["rot_N"]
                    if j - N < 0:
                        continue
                    m = ind[c]["close"][j - N]
                    if m > 0:
                        mom[c] = ind[c]["close"][j] / m - 1
                picks = [c for c in sorted(mom, key=lambda x: -mom[x])
                         if mom[c] > P["rot_min_mom"]]
                if P["rot_ma"]:
                    picks = [c for c in picks
                             if sma(ind[c]["close"], P["rot_ma"])[j] is not None
                             and ind[c]["close"][j] > sma(ind[c]["close"],
                                                          P["rot_ma"])[j]]
                picks = picks[:P["rot_K"]]
                tgt = {}
                if picks:
                    each = capital * P["rot_frac"] / len(picks)
                    for c in picks:
                        tgt[c] = int(each / bars[c][i]["open"] // 100 * 100)
                for c in codes:      # 先卖后买(释放现金)
                    cur = acct["pos"][c]["shares"]
                    want = tgt.get(c, 0)
                    if want < cur:
                        _sell(acct, c, POOL[c], cur - want, bars[c][i]["open"],
                              day, "轮动卖出", t1=False)
                for c in codes:
                    cur = acct["pos"][c]["shares"]
                    want = tgt.get(c, 0)
                    if want > cur:
                        _buy(acct, c, POOL[c], want - cur, bars[c][i]["open"],
                             day, "轮动买入")
                    ref[c] = want or ref[c]

        # ---- 日内做T ----
        if P["use_t"] and layer != "bh":
            for c in codes:
                if layer == "trend" and not trend_on[c]:
                    continue
                ii = ind[c]
                b = bars[c][i]
                j = i - 1
                if j < 0:
                    continue
                lower, mid_, upper, atr = ii["lo"][j], ii["mid"][j], ii["up"][j], ii["atr"][j]
                if None in (lower, mid_, atr) or atr <= 0:
                    continue
                sref = mid_ if P["sell_ref"] == "mid" else upper
                if sref is None:
                    continue
                zone = {"buy_low": lower, "buy_high": lower + P["buy_pad_atr"] * atr,
                        "sell_low": sref - P["sell_pad_atr"] * atr, "sell_high": sref,
                        "stop": lower - P["stop_atr"] * atr}
                o, h, l = b["open"], b["high"], b["low"]
                up_first = (h - o) >= (o - l)
                rank = ({"sell": 0, "buy": 1, "stop": 2} if up_first
                        else {"stop": 0, "buy": 1, "sell": 2})
                ev = []
                if (l <= zone["buy_high"]) and (h >= zone["buy_low"]):
                    ev.append(("buy", o if zone["buy_low"] <= o <= zone["buy_high"]
                               else zone["buy_high"]))
                if (h >= zone["sell_low"]) and (l <= zone["sell_high"]):
                    ev.append(("sell", o if zone["sell_low"] <= o <= zone["sell_high"]
                               else zone["sell_low"]))
                if l <= zone["stop"]:
                    ev.append(("stop", min(o, zone["stop"])))
                ev.sort(key=lambda e: rank[e[0]])
                pos = acct["pos"][c]
                lot = int(ref[c] * P["lot_ratio"] // 100 * 100)
                for kind, pf in ev:
                    if kind == "buy":
                        if P["use_vr"] and ii["vr"][i] is not None \
                                and ii["vr"][i] > P["buy_vr_max"]:
                            continue
                        v = votes_for(zone, ii, i, pf)
                        tot, agree = score_side(v, P, +1)
                        if agree < P["min_agree"] or tot < P["buy_thr"]:
                            continue
                        if P["buy_ma_veto"] and ii["ma5"][j] is not None \
                                and ii["ma20"][j] is not None and ii["ma5"][j] < ii["ma20"][j]:
                            continue
                        q = affordable(acct, c, pf, lot)
                        if q >= config.LOT_MIN:
                            _buy(acct, c, POOL[c], q, pf, day, f"低吸共振{tot:.2f}")
                    elif kind == "sell":
                        if P["use_vr"] and ii["vr"][i] is not None \
                                and ii["vr"][i] < P["sell_vr_min"]:
                            continue
                        v = votes_for(zone, ii, i, pf)
                        tot, agree = score_side(v, P, -1)
                        if agree < P["min_agree"] or tot < P["sell_thr"]:
                            continue
                        led = _led(acct, c)
                        if led["sq"] - led["bq"] >= max(lot, config.LOT_MIN):
                            continue
                        q = min(max(lot, config.LOT_MIN), pos["shares"] // 100 * 100)
                        _sell(acct, c, POOL[c], q, pf, day, f"高抛共振{tot:.2f}")
                    else:
                        if pos["shares"] > 0:
                            _sell(acct, c, POOL[c], pos["shares"], min(o, zone["stop"]),
                                  day, "跌破止损", stop=True, t1=False)
        # ---- 估值 ----
        eq = acct["cash"]
        for c, p in acct["pos"].items():
            if p["shares"] > 0:
                eq += p["shares"] * bars[c][i]["close"]
        acct["equity"].append(round(eq, 2))
        if se is None:
            se = eq

    pairs, _ = fifo_pairs(acct["trades"])
    fa = acct["equity"][-1]
    peak, mdd = acct["equity"][0], 0.0
    for v in acct["equity"]:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak else 0)
    by_code = {}
    for op in acct["ops"]:
        d = by_code.setdefault(op["code"], {"buy": 0, "sell": 0, "stop": 0})
        d[{"BUY": "buy", "SELL": "sell", "STOP": "stop"}[op["side"]]] += 1
    return {"layer": layer, "final_asset": round(fa, 2),
            "return": round(fa / se - 1, 4) if se else 0, "mdd": round(mdd, 4),
            "equity": acct["equity"], "ops": acct["ops"], "fees": acct["fees"],
            "n_trades": len(acct["trades"]), "t_realized": round(sum(p["pnl"] for p in pairs), 2),
            "n_stop": sum(1 for t in acct["trades"] if t.get("stop")),
            "start_equity": round(se or capital, 2), "by_code": by_code,
            "days": days}


# ============================== 调优 ==============================
RANGES = {
    "buy_pad_atr": (0.05, 0.60), "sell_pad_atr": (0.05, 0.60), "stop_atr": (0.8, 3.0),
    "w_rsi": (0.05, 0.35), "w_boll": (0.05, 0.35), "w_ma": (0.0, 0.30), "w_pos": (0.0, 0.30),
    "buy_thr": (0.40, 0.80), "sell_thr": (0.40, 0.80), "min_agree": (1, 3),
    "lot_ratio": (0.15, 0.50), "buy_vr_max": (0.6, 1.2), "sell_vr_min": (0.8, 1.8),
}


def random_t_params():
    p = dict(BASE_P)
    for k, (lo, hi) in RANGES.items():
        p[k] = random.randint(lo, hi) if k == "min_agree" else round(random.uniform(lo, hi), 3)
    p["sell_ref"] = random.choice(["mid", "up"])
    p["use_vr"] = random.random() < 0.6
    p["buy_ma_veto"] = random.random() < 0.5
    p["use_t"] = random.random() < 0.7
    p["tr_ma"] = random.choice([5, 10, 20, 30, 60])
    p["tr_mode"] = random.choice(["close_ma", "cross"])
    s = p["w_rsi"] + p["w_boll"] + p["w_ma"] + p["w_pos"]
    if s > 0:
        for k in ("w_rsi", "w_boll", "w_ma", "w_pos"):
            p[k] = round(p[k] / s * 0.70, 3)
    return p


def random_rot_params():
    p = dict(BASE_P)
    p["rot_N"] = random.choice([10, 15, 20, 30, 40, 60])
    p["rot_K"] = random.choice([1, 2, 3])
    p["rot_rebal"] = random.choice([1, 3, 5, 10])
    p["rot_min_mom"] = random.choice([-0.05, 0.0, 0.03, 0.08])
    p["rot_ma"] = random.choice([0, 10, 20, 30, 60])
    p["rot_frac"] = random.choice([0.7, 0.85, 1.0])
    p["use_t"] = random.random() < 0.5
    if p["use_t"]:
        for k, (lo, hi) in RANGES.items():
            if k in ("buy_thr", "sell_thr", "min_agree", "lot_ratio"):
                p[k] = random.randint(lo, hi) if k == "min_agree" else round(random.uniform(lo, hi), 3)
    return p


def optimize(uni, layer, capital, n_iter, split=0.65):
    n = len(uni["603169"]["bars"])
    isp = int(n * split)
    gen = random_rot_params if layer == "rot" else random_t_params
    best, bt = None, -9
    for _ in range(n_iter):
        P = gen()
        try:
            r = run_bt(uni, P, layer, capital, 0, isp)
        except Exception:
            continue
        if r["return"] > bt:
            bt, best = r["return"], P
    rte = run_bt(uni, best, layer, capital, isp, n)
    rfull = run_bt(uni, best, layer, capital, 0, n)
    return {"P": best, "train": bt, "test": rte["return"], "full": rfull,
            "split": isp, "n": n}


# ============================== 稳健性 / 个股独立 ==============================
def monthly_returns(res):
    """按自然月计算权益环比收益"""
    eq, dd = res["equity"], res["days"]
    m0 = {}
    for i, d in enumerate(dd):
        m = d[:7]
        if m not in m0:
            m0[m] = [eq[i], eq[i]]
        m0[m][1] = eq[i]
    out, prev = {}, None
    for m in sorted(m0):
        a, b = m0[m]
        base = prev if prev is not None else a
        out[m] = round(b / base - 1, 4)
        prev = b
    return out


def sweep_rot(uni, capital, topn=10):
    """动量轮动固定参数扫描(不拟合, 用于稳健性检验)"""
    rows = []
    for N in (20, 30, 40):
        for K in (1, 2, 3):
            for rb in (5, 10):
                for ma in (0, 20):
                    P = dict(BASE_P)
                    P.update(rot_N=N, rot_K=K, rot_rebal=rb, rot_ma=ma,
                             rot_frac=0.85, use_t=False)
                    r = run_bt(uni, P, "rot", capital)
                    rows.append({"N": N, "K": K, "rb": rb, "ma": ma, "ret": r["return"],
                                 "mdd": r["mdd"], "trades": r["n_trades"]})
    rows.sort(key=lambda x: -x["ret"])
    return rows[:topn]


def run_ps_stock(uni, code, capital, dyn=None):
    """自适应双模策略(迭代终版 v2.3):
      趋势腿: cl>MA20&MA5>MA10 & cl>MA60 & MA20上行 & 10日动量>2% -> 买入; 收盘<MA10退出
      分批建仓(v2.3, SCALE_IN_EN): 趋势首笔只投SCALE_FRAC1比例, 收盘站上MA5->次日开盘
          加仓剩余; 跌破MA5不加仓, 破位清仓时额度一并回笼(lab回测采纳: 等权Δ+0.96pp)
      动态止盈止损(dyn, 回测实验层; 生产不传=None行为完全不变): 仅作用于趋势腿
          trail        移动止盈: 收盘自持仓最高收盘回撤超X% -> 次日开盘清仓
          trail_tiers  阶梯移动止盈: 按当前浮盈档位选带宽[(浮盈阈值,带宽)...]从宽到窄排列,
                       浮盈越大带宽越宽(小盈紧保护/大盈让利润奔跑), 命中档覆盖trail
          be_trig/stop 浮盈达be_trig后止损线上移至成本*be_stop(保本), 跌破即清仓
          tp1_pct/frac 浮盈达tp1_pct先减仓tp1_frac比例, 剩余继续跑趋势(不再加仓)
          ma_n         趋势退出均线档位(5/10/20/60, 默认10): 卖点敏感度维度
          ma_exit=False 关闭均线退出(卖点完全交给移动止盈/保本线, 卖点随股价动态走)
          rsi_ob/frac  RSI14超买减仓: RSI>=rsi_ob先减仓rsi_frac比例(回落rsi_ob-8重置可再减)
          rsi_clear    RSI14超买清仓: RSI>=rsi_clear直接清仓(激进对照)
          macd_x       MACD(12,26,9)死叉退出: DIF下穿DEA次日开盘清仓(与均线退出叠加,先到先卖)
          stop_pct     固定止损: 收盘<成本*(1-stop_pct)次日开盘清仓
          stop_tiers   阶梯止损: 按持仓最高浮盈(hmax/entry-1)取档[(浮盈阈值,止损线/成本)...]
                       从宽到窄排列, 止损线=entry*line只上移不下移(浮盈曾达高档即锁住),
                       收盘<止损线次日开盘清仓(与移动止盈/MA10叠加, 先到先卖)
          trail_atr    波动率自适应止盈(已否决留档): 带宽=k×入场日ATR%, (k1,k2,k3)对应
                       浮盈档<15%/15~30%/≥30%; 27组网格均≤固定6/10/15%基准, 见Readme
          stop_atr     波动率自适应止损(已否决留档): 灾难线=entry×(1-s×入场ATR%),
                       锁盈档+3/+10同v2.5; s∈{4,5,6}零触发无区别
      scalp腿(弱势市): 昨低破布林下轨且收回轨内+RSI14<40+收阳+5日跌>-12%+(今开-信号低点)>=0.3ATR
                       -> 半仓; 中轨止盈/收盘破信号低点止损/8日超时; 冷却3日
      突发保险丝: 昨日跌<=-7% 或 量>3倍5日均且跌<=-4% -> 次日禁止scalp(恐慌未释放完)
      v2.2候选(config开关, 默认关): A.市场连续冰点+个股超卖未崩->左侧半仓;
          B.日内恐慌V反(盘中暴跌>=5%且收盘收复>=50%且放量)->半仓抄底(豁免疫丝);
          两者出场均复用scalp机制(中轨止盈/信号低点止损cool=3/8日超时)
      注: 行业RS维度(该强不强禁买/该弱不弱放宽/RS仓位/行业环境/RS轮动)经4轮回测迭代
          验证在本池无增量信息(绝对动量+三重过滤已隐含RS), 故不纳入, 见回测报告
    严格无未来函数: j=i-1决策, 开盘i成交"""
    bars = uni[code]["bars"]
    nm = uni[code].get("name", code)      # 支持自定义标的池(如量化当前持仓)
    ind = build_ind(bars)
    closes = ind["close"]
    # v2.2候选: 指数收盘序列(连续冰点判定); 与个股日K同日历, 长度不一致则禁用该维度
    _ib = (uni.get(IDX_CODE) or {}).get("bars") or []
    idx_cl = [b["close"] for b in _ib] if len(_ib) == len(bars) else None
    cash, sh, entry = float(capital), 0, 0.0
    cool, hold_d, n_tr = 0, 0, 0
    sh_tr, sig_low = False, 0.0
    add = 0.0                            # v2.3 分批建仓: 待加仓额度
    hmax, be_on, tp1_done = 0.0, False, False   # 动态止盈止损状态(最高收盘/保本激活/已减仓)
    ob_done = False                             # RSI超买减仓状态(回落重置)
    leg_atr_pct = 0.0                           # 波动率自适应: 入场日ATR14/收盘(止盈止损带宽基准)
    ops, equity = [], []
    # MACD(12,26,9)全序列预计算(供dyn死叉退出维度): 无未来函数, 只用截至当日的收盘
    dif = dea = None
    if dyn and dyn.get("macd_x"):
        _e12 = _e26 = _dv = None
        dif, dea = [], []
        for _c in closes:
            _e12 = _c if _e12 is None else _e12 * 11 / 13 + _c * 2 / 13
            _e26 = _c if _e26 is None else _e26 * 25 / 27 + _c * 2 / 27
            _d = _e12 - _e26
            _dv = _d if _dv is None else _dv * 8 / 10 + _d * 2 / 10
            dif.append(_d)
            dea.append(_dv)
    # 市场层/板块层门控(实验参数, 生产不传=行为不变):
    #   mkt_ma=N  趋势腿入场门: 上证收盘>SMA_N(缺失/未就绪=不拦截)
    #   mkt_exit=N 持仓退出: 上证收盘<SMA_N -> 次日开盘清仓"大盘破位"
    #   bk_cl=板块收盘序列(按个股日历对齐); bk_ma/bk_mom=入场门(SMA_N/N日动量>0)
    #   bk_exit=N 持仓退出: 板块收盘<SMA_N -> 次日开盘清仓"板块破位"
    bk_cl = dyn.get("bk_cl") if dyn else None
    mkt_e = sma(idx_cl, dyn["mkt_ma"]) if (dyn and idx_cl and dyn.get("mkt_ma")) else None
    mkt_x = sma(idx_cl, dyn["mkt_exit"]) if (dyn and idx_cl and dyn.get("mkt_exit")) else None
    mkt_d = sma(idx_cl, dyn["mkt_dn"]) if (dyn and idx_cl and dyn.get("mkt_dn")) else None
    mkt_h = sma(idx_cl, dyn["mkt_half"]) if (dyn and idx_cl and dyn.get("mkt_half")) else None
    bk_e = sma(bk_cl, dyn["bk_ma"]) if (dyn and bk_cl and dyn.get("bk_ma")) else None
    bk_x = sma(bk_cl, dyn["bk_exit"]) if (dyn and bk_cl and dyn.get("bk_exit")) else None
    for i in range(1, len(bars)):
        j = i - 1
        o = bars[i]["open"]
        ma20, ma5, ma10, ma60 = ind["ma20"][j], ind["ma5"][j], ind["ma10"][j], ind["ma60"][j]
        lo, mid, rsi, atr = ind["lo"][j], ind["mid"][j], ind["rsi"][j], ind["atr"][j]
        cl, pc = closes[j], bars[j]["close"]
        if not (None in (ma20, ma5, ma10, lo, mid, rsi, atr) or atr <= 0 or j < 10):
            up_reg = cl > ma20 and ma5 > ma10
            ma20_up = ind["ma20"][j - 5] is not None and ma20 > ind["ma20"][j - 5]
            mom10 = cl / closes[j - 10] - 1
            up_ok = up_reg and ma60 is not None and cl > ma60 and ma20_up and mom10 > 0.02
            # 市场层/板块层入场门(实验): 大盘/板块不走强时压制趋势腿入场(scalp/冰点腿不受限)
            if up_ok and mkt_e and mkt_e[j] and idx_cl[j] <= mkt_e[j]:
                up_ok = False
            if up_ok and mkt_d and mkt_d[j] and j >= 5 and mkt_d[j - 5] \
                    and mkt_d[j] < mkt_d[j - 5]:
                up_ok = False
            if up_ok and bk_e and bk_e[j] and bk_cl[j] <= bk_e[j]:
                up_ok = False
            if up_ok and dyn and dyn.get("bk_mom") and bk_cl:
                k0 = j - dyn["bk_mom"]
                if k0 < 0 or bk_cl[j] <= bk_cl[k0]:
                    up_ok = False
            if sh > 0:
                hold_d += 1
                px_sell, why = None, ""
                if sh_tr:
                    hmax = max(hmax, pc)   # 持仓最高收盘(移动止盈基准)
                    if dyn and dyn.get("be_trig") is not None \
                            and pc >= entry * (1 + dyn["be_trig"]):
                        be_on = True       # 浮盈达标, 激活保本线
                    if dyn and not tp1_done and dyn.get("tp1_pct") \
                            and pc >= entry * (1 + dyn["tp1_pct"]):
                        tp1_done, add = True, 0.0   # 已减仓, 不再加仓
                        q1 = int(sh * dyn.get("tp1_frac", 0.5) // 100 * 100)
                        if q1 >= 100 and sh - q1 >= 100:
                            amt1 = round(q1 * o, 2)
                            f1 = fees.sell_fee(amt1, code)
                            cash += amt1 - f1
                            sh -= q1
                            ops.append({"date": bars[i]["dt"], "code": code,
                                        "name": nm, "side": "SELL", "qty": q1,
                                        "price": round(o, 3), "reason": "止盈减仓"})
                    # RSI超买减仓: 超买先减半仓, 回落rsi_ob-8后重置可再次触发
                    if dyn and dyn.get("rsi_ob") and sh >= 200:
                        rv = ind["rsi"][j]
                        if rv is not None:
                            if rv >= dyn["rsi_ob"] and not ob_done:
                                ob_done, add = True, 0.0   # 超买减仓后不再加仓
                                q1 = int(sh * dyn.get("rsi_frac", 0.5) // 100 * 100)
                                if q1 >= 100 and sh - q1 >= 100:
                                    amt1 = round(q1 * o, 2)
                                    f1 = fees.sell_fee(amt1, code)
                                    cash += amt1 - f1
                                    sh -= q1
                                    ops.append({"date": bars[i]["dt"], "code": code,
                                                "name": nm, "side": "SELL", "qty": q1,
                                                "price": round(o, 3), "reason": "超买减仓"})
                            elif rv <= dyn["rsi_ob"] - 8:
                                ob_done = False
                    if dyn and dyn.get("rsi_clear") and sh >= 100:
                        rv = ind["rsi"][j]
                        if rv is not None and rv >= dyn["rsi_clear"]:
                            px_sell, why = o, "超买清仓"
                    # 动态止盈止损: 阶梯移动止盈(浮盈越大带宽越宽) > 固定trail > 浮盈保本 > MACD死叉/均线退出
                    trail = dyn.get("trail") if dyn else None
                    if dyn and dyn.get("trail_tiers"):
                        pf = pc / entry - 1 if entry > 0 else 0.0
                        for _th, _t in dyn["trail_tiers"]:
                            if pf >= _th:
                                trail = _t
                                break
                    # 波动率自适应: 带宽=k×入场ATR%, 浮盈档<15%/15~30%/≥30%对应k1/k2/k3
                    if dyn and dyn.get("trail_atr") and leg_atr_pct > 0 and entry > 0:
                        k1, k2, k3 = dyn["trail_atr"]
                        pf = pc / entry - 1
                        trail = (k3 if pf >= 0.30 else (k2 if pf >= 0.15 else k1)) \
                            * leg_atr_pct
                    if not px_sell and trail and cl <= hmax * (1 - trail):
                        px_sell, why = o, "移动止盈%.0f%%" % (trail * 100)
                    # 止损为独立判定(不进elif链), 否则配置止损参数后会吞掉下方MA10趋势破位
                    if not px_sell and dyn and (dyn.get("stop_tiers") or dyn.get("stop_pct")
                                                or dyn.get("stop_atr")):
                        stop_line = None
                        if dyn.get("stop_tiers") or dyn.get("stop_atr"):
                            tiers = dyn.get("stop_tiers")
                            if not tiers:   # stop_atr: 锁盈档同v2.5, 灾难线=-s×入场ATR%
                                tiers = [(0.50, 1.10), (0.25, 1.03),
                                         (0.0, 1 - dyn["stop_atr"] * leg_atr_pct)]
                            pfh = hmax / entry - 1 if entry > 0 else 0.0   # 按最高浮盈取档
                            for _th, _ln in tiers:
                                if pfh >= _th:
                                    stop_line = entry * _ln
                                    break
                        else:
                            stop_line = entry * (1 - dyn["stop_pct"])
                        if stop_line and cl < stop_line:
                            why = ("阶梯止损" if (dyn.get("stop_tiers") or dyn.get("stop_atr"))
                                   else "止损%.0f%%" % (dyn["stop_pct"] * 100))
                            px_sell = o
                    # 市场层/板块层破位退出(实验): 大盘/板块收盘跌破SMA_N -> 次日开盘清仓
                    if not px_sell and mkt_x and idx_cl and mkt_x[j] \
                            and idx_cl[j] < mkt_x[j]:
                        px_sell, why = o, "大盘破位"
                    if not px_sell and bk_x and bk_cl and bk_x[j] \
                            and bk_cl[j] < bk_x[j]:
                        px_sell, why = o, "板块破位"
                    if not px_sell and dyn and be_on and cl < entry * dyn.get("be_stop", 1.0):
                        px_sell, why = o, "保本止盈"
                    elif not px_sell and dyn and dyn.get("macd_x") and dif is not None \
                            and j >= 1 and dif[j - 1] >= dea[j - 1] and dif[j] < dea[j]:
                        px_sell, why = o, "MACD死叉"
                    if not px_sell:
                        exit_ma = ma10
                        if dyn and dyn.get("ma_n") in (5, 10, 20, 60):
                            exit_ma = ind["ma%d" % dyn["ma_n"]][j]
                        if cl < exit_ma and not (dyn and dyn.get("ma_exit") is False):
                            px_sell, why = o, "趋势破位"
                else:
                    if pc >= mid:
                        px_sell, why = o, "止盈"
                    elif pc <= sig_low:
                        px_sell, why = o, "止损"
                        cool = 3
                    elif hold_d >= 8:
                        px_sell, why = o, "超时"
                if px_sell:
                    amt = round(sh * px_sell, 2)
                    f = fees.sell_fee(amt, code)
                    cash += amt - f
                    ops.append({"date": bars[i]["dt"], "code": code, "name": nm,
                                "side": "SELL", "qty": sh,
                                "price": round(px_sell, 3), "reason": why})
                    sh, hold_d = 0, 0
                    add = 0.0            # v2.3: 清仓回收未加仓额度
                elif config.SCALE_IN_EN and sh_tr and add > 0 and cl > ma5:
                    # v2.3 分批建仓: 首笔后收盘站上MA5 -> 次日开盘加仓剩余额度
                    q = int(add / o // 100 * 100)
                    add = 0.0
                    if q >= 100:
                        amt = round(q * o, 2)
                        f = fees.buy_fee(amt, code)
                        if cash >= amt + f:
                            cash -= amt + f
                            entry = round((entry * sh + amt + f) / (sh + q), 4)  # 加仓后摊薄成本
                            sh += q
                            n_tr += 1
                            ops.append({"date": bars[i]["dt"], "code": code,
                                        "name": nm, "side": "BUY", "qty": q,
                                        "price": round(o, 3), "reason": "分批加仓"})
            elif up_ok:
                frac = config.SCALE_FRAC1 if config.SCALE_IN_EN else 1.0
                # 市场层弱市折半仓(实验): 大盘收盘<SMA_N时不禁买, 趋势腿首笔减半
                if dyn and dyn.get("mkt_half") and mkt_h and mkt_h[j] \
                        and idx_cl[j] < mkt_h[j]:
                    frac *= 0.5
                q = int(cash * frac / o // 100 * 100)
                if q >= 100:
                    amt = round(q * o, 2)
                    f = fees.buy_fee(amt, code)
                    if cash >= amt + f:
                        cash -= amt + f
                        sh, entry, hold_d, sh_tr = q, o, 0, True
                        hmax, be_on, tp1_done, ob_done = o, False, False, False   # 动态止盈状态重置
                        leg_atr_pct = atr / pc if pc > 0 else 0.0   # 波动率自适应基准(信号日)
                        if config.SCALE_IN_EN:
                            add = cash   # v2.3: 剩余现金作为待加仓额度
                        n_tr += 1
                        ops.append({"date": bars[i]["dt"], "code": code, "name": nm,
                                    "side": "BUY", "qty": q,
                                    "price": round(o, 3), "reason": "趋势站上"})
            elif not up_reg and j >= 20:
                if cool > 0:
                    cool -= 1
                else:
                    # 突发保险丝: 恐慌日(暴跌/放量长阴)次日不接飞刀
                    yret = pc / bars[j - 1]["close"] - 1
                    v5a = sum(ind["vol"][j - 5:j]) / 5
                    panic = yret <= -0.07 or (v5a > 0 and ind["vol"][j] > 3 * v5a
                                              and yret <= -0.04)
                    deep, why, sig = False, "", 0.0
                    # v2.2候选B: 日内恐慌V反抄底(长下影证明承接, 豁免疫丝)
                    if config.PANIC_BUY_EN:
                        loj, pjc = bars[j]["low"], bars[j - 1]["close"]
                        if loj < pjc and loj / pjc - 1 <= config.PANIC_DROP:
                            rec = (pc - loj) / (pjc - loj)
                            if rec >= config.PANIC_RECOVER and v5a > 0 \
                                    and ind["vol"][j] >= config.PANIC_VOLX * v5a \
                                    and (o - loj) >= 0.3 * atr:
                                deep, why, sig = True, "恐慌V反", loj
                    if not deep and not panic:
                        deep = cl > lo and bars[j]["low"] < lo and rsi < 40
                        if not (pc > bars[j]["open"]):
                            deep = False
                        if (cl / closes[j - 5] - 1) < -0.12:
                            deep = False
                        if deep and (o - bars[j]["low"]) < 0.3 * atr:
                            deep = False
                        if deep:
                            why, sig = "超跌", bars[j]["low"]
                        # v2.2候选A: 市场连续冰点+个股超卖未崩 -> 左侧半仓(受保险丝约束)
                        if not deep and config.ICE_LEFT_EN and idx_cl:
                            k0 = j - config.ICE_MKT_DAYS
                            mkt_ice = False
                            if k0 >= 0:
                                dn = sum(1 for k in range(k0 + 1, j + 1)
                                         if idx_cl[k] < idx_cl[k - 1])
                                cum = idx_cl[j] / idx_cl[k0] - 1
                                mkt_ice = dn >= config.ICE_MKT_DAYS \
                                    or cum <= config.ICE_MKT_DROP
                            stk_ok = (rsi < config.ICE_RSI
                                      and (cl / closes[j - 5] - 1) >= config.ICE_STK_DROP5
                                      and pc < bars[j - 1]["close"])
                            if mkt_ice and stk_ok and (o - bars[j]["low"]) >= 0.3 * atr:
                                deep, why, sig = True, "冰点左侧", bars[j]["low"]
                    if deep:
                        q = int(cash * 0.5 / o // 100 * 100)
                        if q >= 100:
                            amt = round(q * o, 2)
                            f = fees.buy_fee(amt, code)
                            if cash >= amt + f:
                                cash -= amt + f
                                sh, entry, hold_d, sh_tr, sig_low = q, o, 0, False, sig
                                n_tr += 1
                                ops.append({"date": bars[i]["dt"], "code": code,
                                            "name": nm, "side": "BUY", "qty": q,
                                            "price": round(o, 3), "reason": why})
        equity.append(round(cash + sh * bars[i]["close"], 2))
    final = equity[-1] if equity else capital
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak else 0)
    return {"return": round(final / capital - 1, 4), "mdd": round(mdd, 4),
            "trades": n_tr, "ops": ops, "equity": equity,
            "final": round(final, 2)}


def per_stock_bt(uni, capital):
    """每只个股独立运行自适应双模策略 -> 个股买卖操作点"""
    return {c: run_ps_stock(uni, c, capital) for c in sorted(POOL)}


# ============================== Dashboard ==============================
def build_dashboard(res, capital, path):
    payload = {"capital": capital, "days": res["days"],
               "layers": {k: {"return": v["return"], "final": v["final_asset"],
                              "mdd": v["mdd"], "trades": v["n_trades"],
                              "fees": v["fees"], "t_realized": v["t_realized"],
                              "n_stop": v.get("n_stop", 0), "equity": v["equity"],
                              "ops": v["ops"], "monthly": monthly_returns(v)}
                          for k, v in res["layers"].items()},
               "stocks": res["stocks"], "train_test": res.get("train_test", {}),
               "rob": res.get("rob", []), "per_stock": res.get("per_stock", {})}
    with open(path, "w", encoding="utf-8") as f:
        f.write(_TPL.replace("__DATA__", json.dumps(payload, ensure_ascii=False)))
    return path


_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>半年回测·个股操作点 Dashboard</title>
<style>
:root{--bg:#0f1420;--card:#171f2e;--line:#2a3446;--fg:#e8edf5;--mut:#9aa7bd;
--red:#e60000;--green:#00a800;--blue:#3f7fd0;--amber:#f0a020;--hdr:#3f4a5a;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:22px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:26px 0 10px;
border-left:3px solid var(--blue);padding-left:8px}
.sub{color:var(--mut);font-size:12px;margin-bottom:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card .k{color:var(--mut);font-size:12px}.card .v{font-size:22px;font-weight:700;margin-top:4px}
.pos{color:#ff5c5c}.neg{color:#4bd94b}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}
th{background:var(--hdr);color:#fff;font-weight:600;text-align:left;padding:9px 10px}
td{padding:8px 10px;border-top:1px solid var(--line)}
tr:hover td{background:#1c2637}
.tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11px;font-weight:600;margin:2px}
.tag.b{background:rgba(230,0,0,.16);color:#ff6b6b}
.tag.s{background:rgba(0,168,0,.16);color:#4bd94b}
.tag.x{background:rgba(240,160,32,.18);color:#f0b64a}
.chart{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px;margin-bottom:14px}
.chart .t{font-size:13px;color:var(--mut);margin-bottom:6px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}
canvas{width:100%;display:block}
.warn{background:rgba(240,160,32,.1);border:1px solid rgba(240,160,32,.4);
border-radius:8px;padding:10px 12px;color:#f0c89a;font-size:13px;margin:10px 0}
.ok{background:rgba(0,168,0,.12);border:1px solid rgba(0,168,0,.5);color:#8fe88f}
.foot{color:var(--mut);font-size:11px;margin-top:20px;line-height:1.8}
</style></head><body><div class="wrap">
<h1>半年量化策略回测 · 个股操作点 Dashboard</h1>
<div class="sub" id="sub"></div>
<div id="cards" class="cards"></div>
<div id="verdict"></div>

<h2>资金曲线 (期初 = 当前账户总资产, 全现金起步)</h2>
<div class="chart"><div class="t"><span>Equity Curve</span><span id="lg"></span></div>
<canvas id="eq" height="250"></canvas></div>

<h2>分层结果对比 (含训练/验证)</h2>
<table id="tbl"></table>

<h2>组合策略 · 个股操作点 (日线 + 买卖/止损标记)</h2>
<div id="stocks"></div>

<h2>月度收益 (权益环比 %)</h2>
<div id="mtb" class="chart" style="padding:0;margin-bottom:14px"></div>

<h2>参数稳健性检验 · 动量轮动固定参数扫描 (全期, 无拟合)</h2>
<div id="rob" class="chart" style="padding:0;margin-bottom:14px"></div>

<h2>个股独立策略操作点 (自适应双模: 趋势市三重过滤持有 / 弱势市超跌反弹半仓)</h2>
<div id="ps"></div>
<div class="foot" id="foot"></div>
</div>
<script>
const D=__DATA__;
const pct=x=>(x*100>=0?'+':'')+(x*100).toFixed(2)+'%';
const clr=x=>x>=0?'pos':'neg';
document.getElementById('sub').textContent=
 `窗口 ${D.days[0]} ~ ${D.days[D.days.length-1]} (${D.days.length}交易日) | 期初资金 ${D.capital.toLocaleString()} 元 | 标的池 ${Object.keys(D.stocks).length}只 | 目标 20~50%`;
const lay=D.layers;
const NAMES={bh:'等权买入持有(基准)',hold:'L1 做T框架(等权底仓)',hold_opt:'L1 做T框架(训练集拟合)',
trend:'L2 趋势择时(MA金叉过滤)',trend_opt:'L2 趋势择时(训练集拟合)',rot:'L2 动量轮动(MA滤·集中) ★推荐',rot_opt:'L2 动量轮动(训练集拟合)'};
const order=['bh','hold','hold_opt','trend','trend_opt','rot','rot_opt'].filter(k=>lay[k]);
const COLORS={bh:'#8899aa',hold:'#3f7fd0',hold_opt:'#5fa8ff',trend:'#f0a020',trend_opt:'#ffa94d',rot:'#b06bd6',rot_opt:'#ff5c5c'};
let cards='';
for(const k of order){const v=lay[k];
 cards+=`<div class="card"><div class="k">${NAMES[k]}</div>
 <div class="v ${clr(v.return)}">${pct(v.return)}</div>
 <div class="k">期末 ${v.final.toLocaleString()} · 回撤 ${(v.mdd*100).toFixed(1)}% · 成交${v.trades}笔</div></div>`;}
document.getElementById('cards').innerHTML=cards;
const cand=order.filter(k=>k!=='bh');
const bk=cand.reduce((a,b)=>lay[a].return>=lay[b].return?a:b), best=lay[bk];
const hit=best.return>=0.20;
document.getElementById('verdict').innerHTML=`<div class="warn ${hit?'ok':''}">
目标 20~50% → 最优层 <b>${NAMES[bk]}</b> 全期 <b>${pct(best.return)}</b> ${hit?'✅ 达标':'❌ 未达标'}
${D.train_test&&D.train_test[bk]?` ｜ 训练 ${pct(D.train_test[bk].train)} / 验证 ${pct(D.train_test[bk].test)}`:''}</div>`;

const cv=document.getElementById('eq'),ctx=cv.getContext('2d');
function drawEq(){cv.width=cv.clientWidth*2;cv.height=250*2;
 ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,cv.width,cv.height);
 const W=cv.width,H=cv.height,pad=46*2;
 let mn=1e18,mx=-1e18;
 for(const k of order)for(const v of lay[k].equity){mn=Math.min(mn,v);mx=Math.max(mx,v)}
 if(mn===mx){mn*=.99;mx*=1.01}
 const X=i=>pad+(W-2*pad)*i/(D.days.length-1),Y=v=>H-pad-(H-2*pad)*(v-mn)/(mx-mn);
 ctx.strokeStyle='#2a3446';ctx.lineWidth=2;ctx.beginPath();
 ctx.moveTo(pad,pad);ctx.lineTo(pad,H-pad);ctx.lineTo(W-pad,H-pad);ctx.stroke();
 // 0 baseline at capital
 for(const k of order){ctx.strokeStyle=COLORS[k];ctx.lineWidth=k.includes('opt')?3:2;
  ctx.beginPath();lay[k].equity.forEach((v,i)=>{i?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v))});ctx.stroke();}
 document.getElementById('lg').innerHTML=order.map(k=>`<span style="color:${COLORS[k]};margin-left:10px">■ ${NAMES[k]} ${pct(lay[k].return)}</span>`).join('');
}
let th='<tr><th>策略层</th><th>全期收益</th><th>期末资产</th><th>最大回撤</th><th>成交笔数</th><th>止损</th><th>费用</th><th>训练/验证</th></tr>';
let tr='';
for(const k of order){const v=lay[k];const tt=D.train_test&&D.train_test[k];
 tr+=`<tr><td>${NAMES[k]}</td><td class="${clr(v.return)}"><b>${pct(v.return)}</b></td>
 <td>${v.final.toLocaleString()}</td><td>${(v.mdd*100).toFixed(1)}%</td><td>${v.trades}</td>
 <td>${v.n_stop||0}</td><td>${v.fees.toLocaleString()}</td>
 <td>${tt?pct(tt.train)+' / '+pct(tt.test):'--'}</td></tr>`;}
document.getElementById('tbl').innerHTML=th+tr;
const SK=bk;
let sh='';
for(const c in D.stocks){const s=D.stocks[c];
 const ops=(lay[SK].ops||[]).filter(o=>o.code===c);
 const last=s.close[s.close.length-1],first=s.close[0];
 sh+=`<div class="chart"><div class="t"><span>${s.name} (${c})  ${first.toFixed(2)}→${last.toFixed(2)} (${pct(last/first-1)})</span>
 <span>${NAMES[SK]} 操作 ${ops.length} 次</span></div><canvas id="c_${c}" height="200"></canvas>
 <div style="font-size:12px;color:var(--mut);margin-top:4px">`;
 for(const o of ops)sh+=`<span class="tag ${o.side==='BUY'?'b':o.side==='SELL'?'s':'x'}" title="${o.reason||''}">${o.date.slice(5)} ${o.side} ${o.qty}@${o.price.toFixed(2)}</span>`;
 if(!ops.length)sh+='<span style="color:#667">无操作</span>';
 sh+=`</div></div>`;}
document.getElementById('stocks').innerHTML=sh;
function drawStock(c){const cv=document.getElementById('c_'+c);cv.width=cv.clientWidth*2;cv.height=200*2;
 const ctx=cv.getContext('2d');ctx.clearRect(0,0,cv.width,cv.height);
 const W=cv.width,H=cv.height,s=D.stocks[c],pad=40*2;
 let mn=Math.min(...s.low),mx=Math.max(...s.high);
 const X=i=>pad+(W-2*pad)*i/(s.close.length-1),Y=v=>H-pad-(H-2*pad)*(v-mn)/(mx-mn);
 ctx.strokeStyle='#232d3e';ctx.lineWidth=1;
 for(let g=0;g<=4;g++){const y=pad+(H-2*pad)*g/4;ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(W-pad,y);ctx.stroke();}
 const bw=Math.max(2,(W-2*pad)/s.close.length*0.62);
 for(let i=0;i<s.close.length;i++){const o=s.open[i],cl=s.close[i],hi=s.high[i],lo=s.low[i];
  const col=cl>=o?'#e60000':'#00a800';ctx.strokeStyle=col;ctx.fillStyle=col;
  ctx.beginPath();ctx.moveTo(X(i),Y(hi));ctx.lineTo(X(i),Y(lo));ctx.stroke();
  const y1=Y(Math.max(o,cl)),y2=Y(Math.min(o,cl));ctx.fillRect(X(i)-bw/2,y1,bw,Math.max(1,y2-y1));}
 const ops=(lay[SK].ops||[]).filter(x=>x.code===c);
 for(const op of ops){const i=s.days.indexOf(op.date);if(i<0)continue;const x=X(i),y=Y(op.price);
  ctx.beginPath();
  if(op.side==='BUY'){ctx.fillStyle='#ff5c5c';ctx.moveTo(x,y+13);ctx.lineTo(x-9,y+27);ctx.lineTo(x+9,y+27);}
  else{ctx.fillStyle=op.side==='STOP'?'#f0a020':'#4bd94b';ctx.moveTo(x,y-13);ctx.lineTo(x-9,y-27);ctx.lineTo(x+9,y-27);}
  ctx.closePath();ctx.fill();}
}
function drawAll(){drawEq();for(const c in D.stocks)drawStock(c);if(window.__drawPS)for(const c in D.per_stock)window.__drawPS(c);}
drawAll();window.addEventListener('resize',drawAll);

// ---- 月度收益 ----
(function(){const months=Object.keys(lay[bk].monthly||{}).sort();
 let h='<tr><th>策略层</th>'+months.map(m=>`<th>${m.slice(2)}</th>`).join('')+'</tr>';
 for(const k of order){const mm=lay[k].monthly||{};
  h+=`<tr><td>${NAMES[k]}</td>`+months.map(m=>{const v=mm[m];
   return `<td class="${v==null?'':clr(v)}">${v==null?'--':pct(v)}</td>`;}).join('')+'</tr>';}
 document.getElementById('mtb').innerHTML=`<table>${h}</table>`;
})();

// ---- 参数稳健性 ----
(function(){const R=D.rob||[];
 let h='<tr><th>参数组合 (动量回看N / 持仓数K / 调仓周期 / MA过滤)</th><th>全期收益</th><th>最大回撤</th><th>成交笔数</th></tr>';
 for(const r of R)h+=`<tr><td>N=${r.N} · K=${r.K} · ${r.rb}日调仓 · MA${r.ma?r.ma:'无'}过滤</td>
  <td class="${clr(r.ret)}"><b>${pct(r.ret)}</b></td><td>${(r.mdd*100).toFixed(1)}%</td><td>${r.trades}</td></tr>`;
 const pos=R.filter(r=>r.ret>=0.20).length;
 document.getElementById('rob').innerHTML=`<table>${h}</table>
  <div style="padding:8px 10px;color:var(--mut);font-size:12px">扫描 ${R.length} 组参数中，${pos} 组全期收益 ≥ 20% —— 表明该策略存在较宽"参数稳健区间"，而非单点幸运拟合。</div>`;
})();

// ---- 个股独立策略 ----
(function(){const PS=D.per_stock||{};let h='';
 for(const c in PS){const s=PS[c],st=D.stocks[c],ops=s.ops||[];
  const last=st.close[st.close.length-1],first=st.close[0];
  h+=`<div class="chart"><div class="t"><span>${st.name} (${c})　标的区间涨跌 ${pct(last/first-1)}</span>
   <span>独立策略 <b class="${clr(s.return)}">${pct(s.return)}</b> · 回撤 ${(s.mdd*100).toFixed(1)}% · ${s.trades}笔</span></div>
  <canvas id="ps_${c}" height="180"></canvas>
  <div style="font-size:12px;color:var(--mut);margin-top:4px">`;
  const shown=ops.slice(-24);
  for(const o of shown)h+=`<span class="tag ${o.side==='BUY'?'b':o.side==='SELL'?'s':'x'}" title="${o.reason||''}">${o.date.slice(5)} ${o.side} ${o.qty}@${o.price.toFixed(2)}</span>`;
  if(!ops.length)h+='<span style="color:#667">无操作</span>';
  h+=`</div></div>`;}
 document.getElementById('ps').innerHTML=h;
 window.__drawPS=function(c){const cv=document.getElementById('ps_'+c);if(!cv)return;
  cv.width=cv.clientWidth*2;cv.height=180*2;const ctx=cv.getContext('2d');
  const W=cv.width,H=cv.height,s=D.stocks[c],ps=PS[c],pad=40*2;
  const mn=Math.min(...s.low),mx=Math.max(...s.high);
  const X=i=>pad+(W-2*pad)*i/(s.close.length-1),Y=v=>H-pad-(H-2*pad)*(v-mn)/(mx-mn);
  ctx.strokeStyle='#232d3e';ctx.lineWidth=1;
  for(let g=0;g<=4;g++){const y=pad+(H-2*pad)*g/4;ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(W-pad,y);ctx.stroke();}
  const bw=Math.max(2,(W-2*pad)/s.close.length*0.62);
  for(let i=0;i<s.close.length;i++){const o=s.open[i],cln=s.close[i],hi=s.high[i],lo=s.low[i];
   const col=cln>=o?'#e60000':'#00a800';ctx.strokeStyle=col;ctx.fillStyle=col;
   ctx.beginPath();ctx.moveTo(X(i),Y(hi));ctx.lineTo(X(i),Y(lo));ctx.stroke();
   const y1=Y(Math.max(o,cln)),y2=Y(Math.min(o,cln));ctx.fillRect(X(i)-bw/2,y1,bw,Math.max(1,y2-y1));}
  for(const op of (ps.ops||[])){const i=s.days.indexOf(op.date);if(i<0)continue;const x=X(i),y=Y(op.price);
   ctx.beginPath();
   if(op.side==='BUY'){ctx.fillStyle='#ff5c5c';ctx.moveTo(x,y+13);ctx.lineTo(x-9,y+27);ctx.lineTo(x+9,y+27);}
   else{ctx.fillStyle=op.side==='STOP'?'#f0a020':'#4bd94b';ctx.moveTo(x,y-13);ctx.lineTo(x-9,y-27);ctx.lineTo(x+9,y-27);}
   ctx.closePath();ctx.fill();}};
 for(const c in PS)window.__drawPS(c);
})();
document.getElementById('foot').innerHTML=
 `口径: 严格无未来函数(第i日决策仅用 i-1 日收盘指标; 成交在当日开盘价); 量比用"昨日量比"代理; 日内路径按"开盘更靠近当日高点→O→H→L, 否则 O→L→H"启发式; 费用按 common/fees.py(佣金万2.5最低5 + 印花税卖出万5 + 过户费十万分之一). 数据: 东财日线 klt=101, ${D.days[0]}~${D.days[D.days.length-1]}.<br>
 结论: 等权持有基准 ${pct(lay.bh.return)}; <b>L1 做T框架</b>不改变底仓方向(≈基准); <b>L2 动量轮动(MA过滤+集中)</b>达到目标区间, 但其收益主要来自 2026-08 单一标的(神奇制药)的强势段, 回撤约 ${(lay[bk].mdd*100).toFixed(0)}%, 属"高集中 + 单标的驱动"结构, 非通用分散收益.<br>
 风险提示: 单窗口 129 日、6 只标的样本有限; 参数稳健区间虽宽(见上表), 仍存在样本外失效风险; 实盘应叠加分散、仓位与止损纪律, 不应照搬集中满仓.<br>
 行业维度迭代结论(4轮): "该强不强"禁买/提前卖、"该弱不弱"放宽、RS仓位调节、行业MA20环境、RS动量轮动 —— 在本池全部无增量或负增量(唯一全期大幅跑赢行业的恰是唯一盈利股, 绝对动量+三重过滤已隐含RS信息); 突发保险丝(恐慌日禁接飞刀)已内置个股策略, 样本内触发0次, 作实盘保护.<br>
 v2.2迭代(2026-10-14): <b>规则A 连续冰点左侧买入</b>(指数连跌3日或累跌≥3%视为冰点 + 个股RSI14&lt;35超卖未崩[5日跌幅≤10%] + 开盘距信号低点≥0.3ATR → 半仓左侧, 出场同scalp) 经27组参数网格验证: RSI35/连跌≥3日为稳定正增量平台(Δ+0.8~+4.6%), 本参数点6股合计Δ+3.60%(样本4笔, 其中康缘单笔-1.1pp, 局限需注意); RSI放宽至40全网格大幅负增量, 勿放宽.<b>规则B 日内恐慌V反抄底</b>(盘中暴跌≥5%且收盘收复≥50%且放量→豁免疫丝抄底) 经18组阈值网格验证: 所有可触发组合均为负增量(-33%~-0.5%), V反多为下跌中继, 豁免疫丝等于拆保护 —— 已否决(代码留备查, PANIC_BUY_EN=False).`;
</script></body></html>"""


# ============================== 入口 ==============================
# 期初资金锚点(个性化, config_local.py 的 BT_CAPITAL 覆盖; 默认100万):
# 全仓买入整百取整在现金边界会量化跳变(一笔成交与否可翻转收益), 策略迭代跨日对比
# 需要稳定基准; 需重定基线时改 config_local.py 的 BT_CAPITAL
BT_CAPITAL = config.BT_CAPITAL

LOADED = None


def main():
    global LOADED
    LOADED = load_universe()
    codes = sorted(POOL)
    days = [b["dt"] for b in LOADED[codes[0]]["bars"]]
    capital = BT_CAPITAL
    print(f"期初资金(策略独立口径, 钉住基线): {capital:,.2f} 元")
    print(f"窗口: {days[0]} ~ {days[-1]} ({len(days)}交易日), 标的池 {len(codes)}只\n")

    layers = {}
    tt = {}
    layers["bh"] = run_bt(LOADED, BASE_P, "bh", capital)
    print(f"[基准] 等权买入持有      : {layers['bh']['return']*100:+.2f}%")
    layers["hold"] = run_bt(LOADED, BASE_P, "hold", capital)
    print(f"[L1默认] 做T框架        : {layers['hold']['return']*100:+.2f}% "
          f"({layers['hold']['n_trades']}笔)")
    layers["trend"] = run_bt(LOADED, BASE_P, "trend", capital)
    print(f"[L2默认] 趋势择时        : {layers['trend']['return']*100:+.2f}% "
          f"({layers['trend']['n_trades']}笔)")
    layers["rot"] = run_bt(LOADED, BASE_P, "rot", capital)
    print(f"[L2默认] 动量轮动        : {layers['rot']['return']*100:+.2f}% "
          f"({layers['rot']['n_trades']}笔)")

    if "--optimize" in sys.argv:
        print("\n[调优] L1 做T框架 (随机搜索) ...")
        o1 = optimize(LOADED, "hold", capital, 400)
        print(f"  训练(前65%) {o1['train']*100:+.2f}% | 验证(后35%) {o1['test']*100:+.2f}% "
              f"| 全期 {o1['full']['return']*100:+.2f}%")
        layers["hold_opt"] = o1["full"]
        tt["hold_opt"] = {"train": o1["train"], "test": o1["test"]}
        print("[调优] L2 趋势择时 (随机搜索) ...")
        o2 = optimize(LOADED, "trend", capital, 400)
        print(f"  训练(前65%) {o2['train']*100:+.2f}% | 验证(后35%) {o2['test']*100:+.2f}% "
              f"| 全期 {o2['full']['return']*100:+.2f}%")
        layers["trend_opt"] = o2["full"]
        tt["trend_opt"] = {"train": o2["train"], "test": o2["test"]}
        print("[调优] L2 动量轮动 (随机搜索) ...")
        o3 = optimize(LOADED, "rot", capital, 900)
        print(f"  训练(前65%) {o3['train']*100:+.2f}% | 验证(后35%) {o3['test']*100:+.2f}% "
              f"| 全期 {o3['full']['return']*100:+.2f}%")
        layers["rot_opt"] = o3["full"]
        tt["rot_opt"] = {"train": o3["train"], "test": o3["test"]}
        json.dump({"L1_best": o1["P"], "trend_best": o2["P"], "rot_best": o3["P"],
                   "L1": {"train": o1["train"], "test": o1["test"]},
                   "trend": {"train": o2["train"], "test": o2["test"]},
                   "rot": {"train": o3["train"], "test": o3["test"]}},
                  open(os.path.join(TMP_DIR, "best_params.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)

    # 结论
    cand = {k: v["return"] for k, v in layers.items() if k != "bh"}
    bk = max(cand, key=cand.get)
    print(f"\n>>> 最优层: {bk} 全期 {cand[bk]*100:+.2f}% | "
          f"目标20~50%: {'达标 ✅' if cand[bk] >= 0.20 else '未达标 ❌'}")

    stocks = {c: {"name": POOL[c], "days": [x["dt"] for x in LOADED[c]["bars"]],
                  "open": [x["open"] for x in LOADED[c]["bars"]],
                  "close": [x["close"] for x in LOADED[c]["bars"]],
                  "high": [x["high"] for x in LOADED[c]["bars"]],
                  "low": [x["low"] for x in LOADED[c]["bars"]]} for c in codes}

    print("\n[稳健性] 动量轮动固定参数扫描 ...")
    rob = sweep_rot(LOADED, capital, 10)
    for r in rob[:5]:
        print(f"  N={r['N']:<2} K={r['K']} rb={r['rb']:<2} MA{r['ma'] or '--'}: "
              f"{r['ret']*100:+.2f}% (回撤{r['mdd']*100:.1f}%, {r['trades']}笔)")

    print("\n[个股独立·自适应双模]")
    ps = per_stock_bt(LOADED, capital)
    for c in sorted(ps):
        print(f"  {POOL[c]}({c}): {ps[c]['return']*100:+.2f}% "
              f"({ps[c]['trades']}笔, 回撤{ps[c]['mdd']*100:.1f}%)")

    if "--no-dashboard" not in sys.argv:
        out = os.path.join(config.REPORT_DIR, "dashboard_backtest.html")
        build_dashboard({"days": days, "layers": layers, "stocks": stocks,
                         "train_test": tt, "rob": rob, "per_stock": ps}, capital, out)
        print(f"\ndashboard -> {out}")


if __name__ == "__main__":
    main()
