# -*- coding: utf-8 -*-
"""T侧做T策略回测器: 5分钟K线重放, 旧策略(现行区间+止损) vs 新策略(反T编排+网格回补)
用法: python bt_ttrade.py [--codes 603169,...] [--shares 603169:14700,...]
                        [--m5 2400] [--chip] [--sweep]
数据: 5分钟K(尽量拉长, 2400根≈32+交易日) + 日K(前一日BOLL20/ATR14/MA20基准)
口径: T+1约束(当日买入不可卖)与实盘一致; 配对=当日FIFO(反T先卖后买/正T跨日出场)
     正T腿T+1当日不可卖 -> 次日开盘出场(隔日T, 如实计提隔夜风险)
新增(2026-11-11): --chip 启用筹码密集区+滞涨因子(与实盘ttrade_v3镜像),
     --sweep 扫参网格(密集区累计阈值/滞涨根数/滞涨乖离/下沿支撑乖离/内部禁做),
     以"当前v3(chip off)"为基线, 输出净盈亏/胜率/夏普对比, 参数达标后写回config。
新增(2026-11-13): --ml 启用SVM周频方向闸门(lab机器学习实验移植):
     每股每周用当周之前的2年日线重训SVM, 预测"5个交易日后收盘更高?";
     预测跌(0)时禁正T买入腿(不接飞刀) / 预测涨(1)时可选禁反T卖出腿(保上行);
     --ml-sweep 扫闸门组合(禁正T/禁回补/禁反T), 基线仍为当前v3, 追求净盈亏最大化。
"""
import argparse
import datetime
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, r"d:\trae_projects\stock_monitor")
import numpy as np
from sklearn import svm as _svm

from common import quotes, fees
from ttrade import config_ttrade as config
from ttrade import chip as chip_mod

FEE_BUY, FEE_SELL = fees.buy_fee, fees.sell_fee
DAILY_CACHE = {}
ZONE_CACHE = {}          # (code, date, pct, days, float_mult) -> 密集区{lo,hi}


# ---------------- 日线技术基准(BOLL20/ATR14/MA20, 与tplan_gen四因子同源) ----------------
def daily_base(code, end_date):
    key = (code, end_date)
    if key in DAILY_CACHE:
        return DAILY_CACHE[key]
    bars = quotes.fetch_kline_bars(code, 101, 200)   # 200根: 筹码密集区需60+历史
    bars = [b for b in bars if b["dt"][:10] < end_date]
    if len(bars) < 21:
        DAILY_CACHE[key] = None
        return None
    closes = [b["close"] for b in bars[-20:]]
    ma20 = sum(closes) / 20
    sd = (sum((c - ma20) ** 2 for c in closes) / 20) ** 0.5
    trs = []
    for i in range(-14, 0):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / 14
    base = {"boll_lo": ma20 - 2 * sd, "boll_mid": ma20, "atr": atr,
            "ma20": ma20, "prev_close": bars[-1]["close"], "past": bars}
    DAILY_CACHE[key] = base
    return base


def daily_zone(code, end_date, pct, days, float_mult):
    """当日筹码密集区(缓存key含参数, 供扫参)"""
    key = (code, end_date, pct, days, float_mult)
    if key in ZONE_CACHE:
        return ZONE_CACHE[key]
    base = daily_base(code, end_date)
    z = chip_mod.chip_zone(base["past"], pct=pct, days=days,
                           float_mult=float_mult) if base else None
    ZONE_CACHE[key] = z
    return z


def plan_prices(base):
    """按tplan_gen四因子公式算当日低吸/高抛/止损/价差(与实盘t_plan同源)"""
    atr = base["atr"]
    return {"lo2": base["boll_lo"] + config.TP_LO_A2 * atr,   # 低吸触发上限
            "hi1": base["boll_mid"] + config.TP_HI_A1 * atr,  # 高抛触发下限
            "stop": base["boll_lo"] + config.TP_LO_A1 * atr
                    - config.TP_STOP_A * atr,
            "sprd": max(0.15, config.TP_SPRD_A * atr)}


def lot_of(shares):
    return max(int(shares * config.LOT_RATIO // 100) * 100, 0)


def std_lot(qty):
    return max(int(qty // 100) * 100, 0)


# ---------------- SVM周频方向闸门(2026-11-13, lab机器学习实验移植) ----------------
# 数据: D:\trae_projects\_bt_tmp\ml_daily_cache.json(2年日线); 只用回测日之前的数据,
# 严格无未来函数; 每股每ISO周重训一次(扩张窗口walk-forward), 全局参数不逐股调参。
# 两种取预测方式:
#   ml_pred      每日预测(模型每周重训, 特征滚动到昨日) —— 旧"闸门"档用
#   ml_week_pred 每周一次固定预测(周一开市前用截至上周五的数据预测,
#                预测区间=本周5个交易日, 全周固定) —— "方向指导"档用(2026-11-13)
ML_DAILY = r"D:\trae_projects\_bt_tmp\ml_daily_cache.json"
_ML = {"daily": None, "models": {}, "week_pred": {}}


def _ml_feats(bars, i, n=15):
    """截至第i根(含)的n根日K -> 7因子(与用户原脚本一致)"""
    w = bars[i - n + 1:i + 1]
    close = [b["close"] for b in w]
    high = [b["high"] for b in w]
    low = [b["low"] for b in w]
    vol = [b["vol"] for b in w]
    mean = lambda xs: sum(xs) / len(xs)
    return [close[-1] / mean(close),
            vol[-1] / (mean(vol) or 1.0),
            high[-1] / mean(high),
            low[-1] / mean(low),
            float(vol[-1]),
            close[-1] / close[0],
            float(np.std(close))]


def _ml_train(hist):
    """在hist(严格历史)上训练SVM; 样本i取[14, len-6], 标签close[i+5]>close[i]"""
    if len(hist) < 30:
        return None
    xs, ys = [], []
    for i in range(14, len(hist) - 5):
        xs.append(_ml_feats(hist, i))
        ys.append(1 if hist[i + 5]["close"] > hist[i]["close"] else 0)
    if len(xs) < 10 or len(set(ys)) < 2:
        return None
    try:                      # 全局参数(扫优结论): C=0.5, rbf, gamma=auto, 不标准化
        return _svm.SVC(C=0.5, kernel="rbf", gamma="auto").fit(
            np.array(xs), ys)
    except Exception:
        return None


def _ml_predict(clf, hist):
    if clf is None or len(hist) < 15:
        return None
    x = np.array([_ml_feats(hist, len(hist) - 1)])
    try:
        return int(clf.predict(x)[0])
    except Exception:
        return None


def _ml_daily(code):
    if _ML["daily"] is None:
        if not os.path.exists(ML_DAILY):
            return []
        _ML["daily"] = json.load(open(ML_DAILY, encoding="utf-8"))["data"]
    return (_ML["daily"].get(code) or {}).get("bars") or []


def ml_pred(code, d):
    """(code, d)的每日SVM预测: 1=预测5个交易日后涨 / 0=预测跌 / None=数据不足
    模型每周重训(缓存), 特征每日滚动到昨日收盘 —— 只用历史"""
    hist = [b for b in _ml_daily(code) if b["dt"] < d]
    wk = datetime.date.fromisoformat(d).isocalendar()[:2]
    key = (code, wk)
    clf = _ML["models"].get(key)
    if clf is None:
        clf = _ML["models"][key] = _ml_train(hist)
    return _ml_predict(clf, hist)


def ml_week_pred(code, d):
    """(code, d)所在ISO周的方向指导(预测区间=本周5个交易日):
    周一开市前用"截至上周五收盘"的日线训练并预测一次, 全周固定使用同一预测;
    预测涨=本周以正T为主(先买后卖) / 预测跌=本周以反T为主(先卖后买)"""
    iso = datetime.date.fromisoformat(d).isocalendar()
    monday = str(datetime.date.fromisocalendar(iso[0], iso[1], 1))  # 本周一
    key = (code, iso[:2])
    if key in _ML["week_pred"]:
        return _ML["week_pred"][key]
    hist = [b for b in _ml_daily(code) if b["dt"] < monday]   # 严格只用周一之前
    clf = _ml_train(hist)
    p = _ml_predict(clf, hist)
    _ML["week_pred"][key] = p
    return p


class BtAcct:
    """回测子账户(单股): T+1口径与实盘一致"""

    def __init__(self, code, name, shares, cash):
        self.code, self.name = code, name
        self.shares = shares
        self.yd = shares                  # 昨仓(T+1可卖)
        self.today_buy = 0
        self.cash = cash
        self.trades = []
        self.stop_cleared = False

    def buy(self, dt, qty, price, tag=""):
        qty = std_lot(qty)
        while qty > 0 and self.cash < qty * price + FEE_BUY(qty * price, self.code):
            qty -= 100
        if qty <= 0:
            return None
        amt = qty * price
        fee = FEE_BUY(amt, self.code)
        self.cash -= amt + fee
        self.shares += qty
        self.today_buy += qty
        self.trades.append({"dt": dt, "side": "buy", "qty": qty,
                            "price": price, "fee": fee, "tag": tag})
        return qty

    def sell(self, dt, qty, price, tag="", t1=True):
        qty = std_lot(qty)
        avail = self.shares - self.today_buy if t1 else self.shares
        qty = min(qty, std_lot(avail))
        if qty <= 0:
            return None
        amt = qty * price
        fee = FEE_SELL(amt, self.code)
        self.cash += amt - fee
        self.shares -= qty
        self.trades.append({"dt": dt, "side": "sell", "qty": qty,
                            "price": price, "fee": fee, "tag": tag})
        return qty

    def clear_stop(self, dt, price):
        qty = std_lot(self.shares - self.today_buy)   # 止损豁免T+1(卖昨仓)
        if qty <= 0:
            return
        self.sell(dt, qty, price, "stop", t1=True)
        self.stop_cleared = True

    def eod(self):
        self.yd = self.shares
        self.today_buy = 0


def fifo_pairs(trades):
    """当日FIFO配对(同实盘account.fifo_pairs语义, 止损腿不入配对)"""
    queues, pairs = {}, []
    for t in trades:
        if t["side"] == "stop":
            continue
        fps = t["fee"] / t["qty"] if t["qty"] else 0
        leg = {"qty": t["qty"], "price": t["price"], "fps": fps,
               "tag": t["tag"], "dt": t["dt"]}
        opp = "sell" if t["side"] == "buy" else "buy"
        oq = queues.setdefault(opp, [])
        q = queues.setdefault(t["side"], [])
        left = t["qty"]
        while left > 0 and oq:
            h = oq[0]
            m = min(left, h["qty"])
            b, s = (h, leg) if t["side"] == "sell" else (leg, h)
            pnl = m * (s["price"] - b["price"]) - (b["fps"] + s["fps"]) * m
            pairs.append({"pnl": pnl, "tag": t["tag"] or h["tag"], "dt": t["dt"]})
            h["qty"] -= m
            left -= m
            if h["qty"] <= 0:
                oq.pop(0)
        if left > 0:
            leg["qty"] = left
            q.append(leg)
    return pairs


def hhmm(bar):
    return bar["dt"][11:16]


def minutes(t):
    return int(t[:2]) * 60 + int(t[3:])


# ---------------- 旧策略: 区间触发+止损清仓(现行规则忠实复刻) ----------------
def run_old(acct, bars, plan):
    n_buy = n_sell = 0
    last = {"buy": None, "sell": None}
    vols = []
    gap_deep = None
    for i, b in enumerate(bars):
        t = hhmm(b)
        vols.append(b["vol"])
        ref = sum(vols[-9:-1]) / max(len(vols[-9:-1]), 1)
        # 止损: 跌破止损线清仓(跳空深穿≥1%挂到14:50收盘确认)
        if not acct.stop_cleared:
            if gap_deep is None:
                gap_deep = bars[0]["open"] <= plan["stop"] * (1 - config.ST_GAP_PCT / 100)
            if b["low"] <= plan["stop"] and (not gap_deep or t >= config.ST_GAP_CONFIRM):
                acct.clear_stop(b["dt"], min(b["open"], plan["stop"]))
                continue
        if acct.stop_cleared:
            continue
        cool = lambda side: (last[side] is None
                             or minutes(t) - last[side] >= config.T_COOLDOWN_MIN)
        # 低吸: 触低吸上限即限价买入 + 缩量或停留豁免
        if (n_buy < config.T_MAX_PER_DAY and b["low"] <= plan["lo2"]
                and cool("buy")):
            vr = b["vol"] / ref if ref else 9
            if vr <= (config.T_BUY_VR_MAX or 1.2) or i >= 8:
                q = acct.buy(b["dt"], lot_of(acct.shares), plan["lo2"], "旧低吸")
                if q:
                    n_buy += 1
                    last["buy"] = minutes(t)
        # 高抛: 触高抛下限即限价卖出 + 放量, T+1只卖昨仓
        if (n_sell < config.T_MAX_PER_DAY and b["high"] >= plan["hi1"]
                and cool("sell")):
            vr = b["vol"] / ref if ref else 0
            if vr >= 1.2:
                q = acct.sell(b["dt"], min(lot_of(acct.shares), acct.yd),
                              plan["hi1"], "旧高抛")
                if q:
                    n_sell += 1
                    last["sell"] = minutes(t)


# ---------------- 新策略: 反T编排+网格回补+环境过滤+收盘兜底 ----------------
GRID_K = (0.5, 1.0, 1.5)      # 回补网格: 卖价下方0.5/1.0/1.5×间距, 各接1/3
VWAP_DEV = 0.02               # 高抛VWAP乖离阈值 / 正T深乖离阈值(取负)
GAP_FLAT = 0.01               # 平开区间(±1%内当日不做T)


def run_new(acct, bars, plan, yd_close, ma20, chip_en=False, zone=None,
            stall_n=2, stall_dev=0.012, support_dev=0.015, edge=0.006,
            inside_block=False, ml=None):
    """返回当日正T腿(隔日出场, 由外层跨日处理)
    chip_en=True时镜像实盘ttrade_v3筹码密集区+滞涨三接入点:
      ①上沿/滞涨开反T ②下沿支撑放宽正T ③密集区内部不开新腿(inside_block可关)
    ml=SVM周频方向闸门(2026-11-13): {"pred": 1涨/0跌/None,
      "block_buy": None/"pt"(只禁正T)/"all"(正T+回补都禁), "block_sell": bool};
      预测跌->禁买入类开腿(不接飞刀); 预测涨->可选禁反T卖出腿(保上行); 兜底永不闸"""
    if ml and ml.get("mode") == "guide":      # 方向指导模式: 周频预测定本周T主方向
        p = ml.get("pred")
        ml = {"pred": p,
              "block_buy": "all" if p == 0 else None,   # 跌周: 禁买入类开腿(反T为主)
              "block_sell": bool(p == 1),               # 涨周: 禁反T(正T为主)
              "boost_buy": bool(ml.get("boost") and p == 1)}  # 涨周正T腿上限1->2
    gap = bars[0]["open"] / yd_close - 1
    weak = yd_close < ma20                       # 前一日收MA20下=弱势股
    one_way_dn = gap <= -0.02 or (weak and gap <= 0)     # 单边下跌日: 禁正T
    flat = abs(gap) < GAP_FLAT                   # 平开日: 不开新腿(研究: 平开放弃)
    sprd = max(plan["sprd"] * 0.75, yd_close * 0.012)    # 网格间距≥1.2%
    z_hi = zone["hi"] if (chip_en and zone) else None
    z_lo = zone["lo"] if (chip_en and zone) else None
    sold = []                                    # 未回补卖出腿{price,qty,fills}
    bought = []                                  # 正T腿(隔日出)
    n_sell = n_buy = 0
    vw_pv = vw_v = 0.0
    lows = []
    for i, b in enumerate(bars):
        t = hhmm(b)
        px = b["close"]
        vw_pv += px * b["vol"]; vw_v += b["vol"]
        vwap = vw_pv / vw_v if vw_v else px
        late = t >= "14:40"                      # 14:40后不开新腿(只兜底平仓)
        dev = px / vwap - 1 if vw_v else 0.0
        upper = bool(z_hi and px >= z_hi * (1 - edge))    # 触密集区上沿
        lower = bool(z_lo and px <= z_lo * (1 + edge))    # 触密集区下沿
        inside = bool(z_lo and z_hi
                      and z_lo * (1 + edge) < px < z_hi * (1 - edge))
        # ①高抛腿(反T卖出, 任何环境可卖): 高抛区 或 VWAP上乖离≥2%收阳
        #   新增: 滞涨形态(近n根5分钟高点不再抬高+最新收阴)且(触上沿 或 乖离≥stall_dev)
        #   ML闸门: 预测5日后涨(1)且block_sell -> 不开新卖出腿(持有等涨)
        if (not late and not flat and n_sell < 2 and acct.yd >= 100
                and not (chip_en and inside_block and inside)
                and not (ml and ml.get("block_sell")
                         and ml.get("pred") == 1)):
            hot = (b["high"] >= plan["hi1"]
                   or (dev >= VWAP_DEV and b["close"] > b["open"]))
            if not hot and chip_en and i >= 2 * stall_n - 1:
                rec_bars = bars[i - stall_n + 1:i + 1]
                pri_bars = bars[i - 2 * stall_n + 1:i - stall_n + 1]
                if (max(x["high"] for x in rec_bars)
                        < max(x["high"] for x in pri_bars)
                        and b["close"] < b["open"]):
                    hot = bool(upper or dev >= stall_dev)
            if hot:
                price = max(plan["hi1"], px)
                q = acct.sell(b["dt"], min(lot_of(acct.shares), acct.yd),
                              price, "新反T卖")
                if q:
                    n_sell += 1
                    sold.append({"price": price, "qty": q,
                                 "fills": [False] * 3})
        # ②网格回补(核心动作): 未回补卖出腿分3档接回
        #   ML闸门: 预测跌(0)且block_buy="all" -> 回补也暂停(腿交由收盘兜底闭合)
        if not (ml and ml.get("pred") == 0 and ml.get("block_buy") == "all"):
            for leg in sold:
                if leg["qty"] <= 0:
                    continue
                per = max(std_lot(leg["qty"] // 3), 100) \
                    if leg["qty"] >= 300 else leg["qty"]
                for gi, k in enumerate(GRID_K):
                    if leg["fills"][gi] or leg["qty"] <= 0:
                        continue
                    target = leg["price"] - k * sprd
                    if b["low"] <= target:
                        q = acct.buy(b["dt"], min(per, leg["qty"]), target,
                                     f"新回补{gi + 1}")
                        if q:
                            leg["qty"] -= q
                            leg["fills"][gi] = True
        # ③正T(超跌反弹确认): 非单边下跌/平开日, VWAP深乖离+企稳收阳
        #   新增: 触密集区下沿=强支撑, 乖离门槛放宽(VWAP_DEV -> support_dev)
        #   ML闸门: 预测跌(0)且block_buy -> 禁正T买入腿(不接飞刀)
        lows.append(b["low"])
        dev_need = support_dev if (chip_en and lower) else VWAP_DEV
        n_buy_max = 2 if (ml and ml.get("boost_buy")) else 1
        if (not late and not flat and not one_way_dn and n_buy < n_buy_max
                and not (chip_en and inside_block and inside)
                and not (ml and ml.get("pred") == 0 and ml.get("block_buy"))
                and vwap and px / vwap - 1 <= -dev_need
                and (len(lows) < 6 or min(lows[-3:]) >= min(lows[-6:-3]))
                and b["close"] > b["open"]):
            q = acct.buy(b["dt"], lot_of(acct.shares), px, "新正T")
            if q:
                n_buy += 1
                bought.append({"price": px, "qty": q})
        # ④收盘兜底(反T闭合纪律): 14:45未回补卖出腿按现价买回
        if t >= "14:45":
            for leg in sold:
                if leg["qty"] > 0:
                    q = acct.buy(b["dt"], leg["qty"], px, "新兜底")
                    if q:
                        leg["qty"] = 0
    return [pos for pos in bought if pos["qty"] > 0]


def load_data(codes, m5_lmt=2400):
    """拉5分钟K(尽量长)并切出完整交易日; 各策略共享同份数据"""
    data = {}
    for c in codes:
        m5 = quotes.fetch_kline_bars(c, 5, m5_lmt) or []
        cnt = defaultdict(int)
        for b in m5:
            cnt[b["dt"][:10]] += 1
        days = sorted(d for d, n in cnt.items() if n >= 20)[1:]   # 首日不全丢弃
        data[c] = {"m5": m5, "days": days}
    return data


def run_backtest(codes, shares_map, cash0=57010.07, m5_lmt=2400,
                 chip_cfg=None, data=None, run_old_too=True, ml_cfg=None):
    """chip_cfg: {pct, days, float_mult, stall_n, stall_dev, support_dev,
    edge, inside_block}; None=当前v3基线(不启用筹码/滞涨)
    ml_cfg(2026-11-13): {block_buy: None/"pt"/"all", block_sell: bool};
    None=不启用SVM方向闸门"""
    data = data or load_data(codes, m5_lmt)
    all_days = sorted(set().union(*[set(d["days"]) for d in data.values()]))
    names = {"603169": "兰石重装", "600557": "康缘药业", "300207": "欣旺达",
             "600480": "凌云股份", "300455": "航天智装", "600879": "航天电子",
             "002009": "天奇股份", "600803": "新奥股份"}
    res = {"OLD": defaultdict(list), "NEW": defaultdict(list)}
    detail = {"OLD": [], "NEW": []}
    carry = {}                                     # 新策略隔日正T腿
    stopped = {"OLD": set(), "NEW": set()}
    for d in all_days:
        for c in codes:
            cd = data[c]
            if d not in cd["days"] or shares_map[c] <= 0:
                continue
            bars = [b for b in cd["m5"] if b["dt"][:10] == d]
            base = daily_base(c, d)
            if not base:
                continue
            plan = plan_prices(base)
            sh = shares_map[c]
            nm = names.get(c, c)
            # ---- OLD ----
            if run_old_too and c not in stopped["OLD"]:
                a1 = BtAcct(c, nm, sh, cash0)
                run_old(a1, bars, plan)
                for p in fifo_pairs(a1.trades):
                    res["OLD"][d].append(p)
                detail["OLD"] += [(d, c, nm, a1.trades, a1.stop_cleared)]
                if a1.stop_cleared:
                    stopped["OLD"].add(c)
            # ---- NEW ----
            if c not in stopped["NEW"]:
                a2 = BtAcct(c, nm, sh, cash0)
                pos = carry.pop(c, None)
                if pos:                            # 昨日正T腿: 今日开盘出场
                    o = bars[0]["open"]
                    tag = "新隔日T" + ("盈" if o > pos["price"] else "亏")
                    a2.sell(bars[0]["dt"], pos["qty"], o, tag, t1=False)
                zone = None
                if chip_cfg:
                    zone = daily_zone(c, d, chip_cfg["pct"], chip_cfg["days"],
                                      chip_cfg["float_mult"])
                nb = run_new(a2, bars, plan, base["prev_close"], base["ma20"],
                              chip_en=bool(chip_cfg), zone=zone,
                              stall_n=chip_cfg["stall_n"] if chip_cfg else 2,
                              stall_dev=chip_cfg["stall_dev"] if chip_cfg else 0.012,
                              support_dev=chip_cfg["support_dev"] if chip_cfg else 0.015,
                              edge=chip_cfg["edge"] if chip_cfg else 0.006,
                              inside_block=chip_cfg["inside_block"] if chip_cfg else False,
                              ml=({"pred": (ml_week_pred(c, d)
                                            if ml_cfg.get("mode") == "guide"
                                            else ml_pred(c, d)), **ml_cfg}
                                   if ml_cfg else None))
                for p in fifo_pairs(a2.trades):
                    res["NEW"][d].append(p)
                detail["NEW"] += [(d, c, nm, a2.trades, a2.stop_cleared)]
                if nb:
                    carry[c] = nb[0]
    return res, detail, all_days


def metrics(res_one, detail_one, days):
    """单策略指标: 配对数/胜率/净盈亏/费用/腿数/日盈亏夏普(含零交易日)"""
    ps = [p for d in days for p in res_one.get(d, [])]
    n, win = len(ps), sum(1 for p in ps if p["pnl"] > 0)
    pnl = sum(p["pnl"] for p in ps)
    fee = sum(t["fee"] for _, _, _, ts, _ in detail_one for t in ts)
    n_leg = sum(1 for _, _, _, ts, _ in detail_one
                for t in ts if t["side"] in ("buy", "sell"))
    by_day = {d: sum(p["pnl"] for p in res_one.get(d, [])) for d in days}
    vals = [by_day.get(d, 0.0) for d in days]
    mu = sum(vals) / len(vals) if vals else 0.0
    sd = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5 if vals else 0.0
    sharpe = mu / sd if sd > 0 else 0.0
    return {"n": n, "win": win, "win_rate": win / n * 100 if n else 0.0,
            "pnl": pnl, "fee": fee, "n_leg": n_leg, "sharpe": sharpe}


def report(res, detail, days, cash0=57010.07, chip_cfg=None):
    print(f"回测区间: {days[0]} ~ {days[-1]} ({len(days)}个交易日) "
          f"底仓&现金: 统一初始快照"
          + (f" | 筹码密集区+滞涨: ON {chip_cfg}" if chip_cfg else ""))
    for tag in ("OLD", "NEW"):
        m = metrics(res[tag], detail[tag], days)
        n_stop = sum(1 for _, _, _, _, sc in detail[tag] if sc)
        if m["n"]:
            print(f"\n===== [{tag}] 配对{m['n']}对 | 已回补胜率"
                  f"{m['win_rate']:.0f}%(胜{m['win']}/亏{m['n'] - m['win']}) "
                  f"| 净盈亏{m['pnl']:+,.2f} | 费用{m['fee']:,.2f} "
                  f"| 成交腿{m['n_leg']} 止损清仓{n_stop}次 "
                  f"| 日盈亏夏普{m['sharpe']:.3f}")
        else:
            print(f"\n===== [{tag}] 无配对 | 成交腿{m['n_leg']} "
                  f"止损清仓{n_stop}次")
        by_day = defaultdict(float)
        for d in days:
            for p in res[tag].get(d, []):
                by_day[d] += p["pnl"]
        for d in sorted(by_day):
            print(f"  {d}: {by_day[d]:+,.2f}")
        for p in sorted((p for d in days for p in res[tag].get(d, [])),
                        key=lambda x: x["dt"]):
            print(f"    {p['dt']} [{p['tag']}] {p['pnl']:+,.2f}")


def sweep(codes, shares_map, cash0=57010.07, m5_lmt=2400, top=15):
    """筹码密集区+滞涨 扫参: 以当前v3(chip off)为基线, 网格寻优"""
    data = load_data(codes, m5_lmt)
    res0, det0, days = run_backtest(codes, shares_map, m5_lmt=m5_lmt,
                                    chip_cfg=None, data=data, run_old_too=False)
    m0 = metrics(res0["NEW"], det0["NEW"], days)
    print(f"基线(v3现状): 配对{m0['n']}对 胜率{m0['win_rate']:.0f}% "
          f"净盈亏{m0['pnl']:+,.2f} 费用{m0['fee']:,.2f} "
          f"腿{m0['n_leg']} 夏普{m0['sharpe']:.3f}")
    rows = []
    for pct in (0.60, 0.70, 0.80):
        for stall_n in (2, 3):
            for stall_dev in (0.010, 0.012, 0.015):
                for support_dev in (0.012, 0.015, 0.020):
                    for inside_block in (True, False):
                        cfg = {"pct": pct, "days": 60, "float_mult": 60.0,
                               "stall_n": stall_n, "stall_dev": stall_dev,
                               "support_dev": support_dev, "edge": 0.006,
                               "inside_block": inside_block}
                        res, det, _ = run_backtest(
                            codes, shares_map, m5_lmt=m5_lmt, chip_cfg=cfg,
                            data=data, run_old_too=False)
                        m = metrics(res["NEW"], det["NEW"], days)
                        rows.append((cfg, m))
                        print(f"pct={pct:.2f} n={stall_n} "
                              f"sd={stall_dev:.3f} sup={support_dev:.3f} "
                              f"blk={'Y' if inside_block else 'N'}: "
                              f"配对{m['n']} 胜率{m['win_rate']:.0f}% "
                              f"净{m['pnl']:+,.2f} 夏普{m['sharpe']:.3f}",
                              flush=True)
    rows.sort(key=lambda x: (-x[1]["pnl"], -x[1]["sharpe"]))
    print(f"\n===== TOP{top} (按净盈亏, 夏普次序) =====")
    print("pct    n  stall  support blk | 配对 胜率   净盈亏    费用   腿  夏普")
    for cfg, m in rows[:top]:
        print(f"{cfg['pct']:.2f}  {cfg['stall_n']}  {cfg['stall_dev']:.3f}  "
              f"{cfg['support_dev']:.3f}   {'Y' if cfg['inside_block'] else 'N'} | "
              f"{m['n']:3d} {m['win_rate']:3.0f}% {m['pnl']:+10,.2f} "
              f"{m['fee']:8,.2f} {m['n_leg']:3d} {m['sharpe']:.3f}")
    print(f"基线:        配对{m0['n']:3d} {m0['win_rate']:3.0f}% "
          f"{m0['pnl']:+10,.2f} {m0['fee']:8,.2f} {m0['n_leg']:3d} "
          f"{m0['sharpe']:.3f}")


def sweep_ml(codes, shares_map, cash0=57010.07, m5_lmt=2400):
    """SVM方向闸门扫参(2026-11-13): 以当前v3(无ML)为基线,
    网格=预测跌时禁正T(pt)/正T+回补(all) × 预测涨时禁反T(开/关), 追净盈亏最大"""
    data = load_data(codes, m5_lmt)
    res0, det0, days = run_backtest(codes, shares_map, m5_lmt=m5_lmt,
                                    chip_cfg=None, data=data, run_old_too=False)
    m0 = metrics(res0["NEW"], det0["NEW"], days)
    print(f"基线(v3现状, 无ML): 配对{m0['n']}对 胜率{m0['win_rate']:.0f}% "
          f"净盈亏{m0['pnl']:+,.2f} 费用{m0['fee']:,.2f} "
          f"腿{m0['n_leg']} 夏普{m0['sharpe']:.3f}")
    rows = []
    for block_buy in (None, "pt", "all"):
        for block_sell in (False, True):
            if block_buy is None and not block_sell:
                continue                      # 全关=基线, 不重跑
            cfg = {"block_buy": block_buy, "block_sell": block_sell}
            res, det, _ = run_backtest(
                codes, shares_map, m5_lmt=m5_lmt, chip_cfg=None, data=data,
                run_old_too=False, ml_cfg=cfg)
            m = metrics(res["NEW"], det["NEW"], days)
            rows.append((cfg, m))
            bb = {"None": "-", "pt": "禁正T", "all": "禁正T+回补"}[
                str(block_buy)]
            print(f"ML闸门[预测跌:{bb} | 预测涨{'禁反T' if block_sell else '放行'}]: "
                  f"配对{m['n']} 胜率{m['win_rate']:.0f}% "
                  f"净{m['pnl']:+,.2f} 夏普{m['sharpe']:.3f}", flush=True)
    rows.sort(key=lambda x: (-x[1]["pnl"], -x[1]["sharpe"]))
    print("\n===== ML闸门TOP(按净盈亏, 夏普次序) =====")
    print("预测跌禁买档位    预测涨禁反T | 配对 胜率   净盈亏    费用   腿  夏普")
    for cfg, m in rows:
        bb = {"None": "-", "pt": "只禁正T", "all": "正T+回补"}[str(cfg["block_buy"])]
        print(f"{bb:<10} {'是' if cfg['block_sell'] else '否':>10} | "
              f"{m['n']:3d} {m['win_rate']:3.0f}% {m['pnl']:+10,.2f} "
              f"{m['fee']:8,.2f} {m['n_leg']:3d} {m['sharpe']:.3f}")
    print(f"基线(无ML):   配对{m0['n']:3d} {m0['win_rate']:3.0f}% "
          f"{m0['pnl']:+10,.2f} {m0['fee']:8,.2f} {m0['n_leg']:3d} "
          f"{m0['sharpe']:.3f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="603169,600557,300207,600480,300455,"
                                        "600879,002009")
    ap.add_argument("--shares", default="603169:14700,600557:3600,300207:500,"
                                        "600480:8500,300455:1500,600879:2000,"
                                        "002009:900")
    ap.add_argument("--m5", type=int, default=2400,
                    help="5分钟K拉取根数(2400≈32+交易日)")
    ap.add_argument("--chip", action="store_true",
                    help="启用筹码密集区+滞涨(参数取config_ttrade)")
    ap.add_argument("--sweep", action="store_true",
                    help="扫参网格(基线vs全组合, 输出TOP榜单)")
    ap.add_argument("--ml", action="store_true",
                    help="启用SVM方向闸门(预测跌禁正T, 默认档)")
    ap.add_argument("--ml-sweep", action="store_true", dest="ml_sweep",
                    help="SVM闸门组合扫参(基线vs全组合, 输出TOP榜单)")
    args = ap.parse_args()
    codes = args.codes.split(",")
    shares_map = {c: 1000 for c in codes}
    if args.shares:
        shares_map = {kv.split(":")[0]: int(kv.split(":")[1])
                      for kv in args.shares.split(",")}
    if args.sweep:
        sweep(codes, shares_map, m5_lmt=args.m5)
    elif args.ml_sweep:
        sweep_ml(codes, shares_map, m5_lmt=args.m5)
    else:
        chip_cfg = None
        if args.chip:
            chip_cfg = {"pct": config.V3_CHIP_PCT, "days": config.V3_CHIP_DAYS,
                        "float_mult": config.V3_CHIP_FLOAT_MULT,
                        "stall_n": config.V3_STALL_N,
                        "stall_dev": config.V3_STALL_DEV,
                        "support_dev": config.V3_SUPPORT_DEV,
                        "edge": config.V3_CHIP_EDGE,
                        "inside_block": config.V3_INSIDE_BLOCK}
        ml_cfg = {"block_buy": "pt", "block_sell": False} if args.ml else None
        res, detail, days = run_backtest(codes, shares_map, m5_lmt=args.m5,
                                         chip_cfg=chip_cfg, ml_cfg=ml_cfg)
        report(res, detail, days, chip_cfg=chip_cfg)
