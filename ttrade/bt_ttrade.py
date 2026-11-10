# -*- coding: utf-8 -*-
"""T侧做T策略回测器: 5分钟K线重放, 旧策略(现行区间+止损) vs 新策略(反T编排+网格回补)
用法: python bt_ttrade.py [--codes 603169,...] [--shares 603169:14700,...]
数据: 腾讯5分钟K(约9个交易日) + 日K(前一日BOLL20/ATR14/MA20基准)
口径: T+1约束(当日买入不可卖)与实盘一致; 配对=当日FIFO(反T先卖后买/正T跨日出场)
     正T腿T+1当日不可卖 -> 次日开盘出场(隔日T, 如实计提隔夜风险)
"""
import argparse
import sys
from collections import defaultdict

sys.path.insert(0, r"d:\trae_projects\stock_monitor")
from common import quotes, fees
from ttrade import config_ttrade as config

FEE_BUY, FEE_SELL = fees.buy_fee, fees.sell_fee
DAILY_CACHE = {}


# ---------------- 日线技术基准(BOLL20/ATR14/MA20, 与tplan_gen四因子同源) ----------------
def daily_base(code, end_date):
    key = (code, end_date)
    if key in DAILY_CACHE:
        return DAILY_CACHE[key]
    bars = quotes.fetch_kline_bars(code, 101, 80)
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
            "ma20": ma20, "prev_close": bars[-1]["close"]}
    DAILY_CACHE[key] = base
    return base


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


def run_new(acct, bars, plan, yd_close, ma20):
    """返回当日正T腿(隔日出场, 由外层跨日处理)"""
    gap = bars[0]["open"] / yd_close - 1
    weak = yd_close < ma20                       # 前一日收MA20下=弱势股
    one_way_dn = gap <= -0.02 or (weak and gap <= 0)     # 单边下跌日: 禁正T
    flat = abs(gap) < GAP_FLAT                   # 平开日: 不开新腿(研究: 平开放弃)
    sprd = max(plan["sprd"] * 0.75, yd_close * 0.012)    # 网格间距≥1.2%
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
        # ①高抛腿(反T卖出, 任何环境可卖): 高抛区 或 VWAP上乖离≥2%收阳
        if (not late and not flat and n_sell < 2 and acct.yd >= 100):
            hot = (b["high"] >= plan["hi1"]
                   or (px / vwap - 1 >= VWAP_DEV and b["close"] > b["open"]))
            if hot:
                price = max(plan["hi1"], px)
                q = acct.sell(b["dt"], min(lot_of(acct.shares), acct.yd),
                              price, "新反T卖")
                if q:
                    n_sell += 1
                    sold.append({"price": price, "qty": q,
                                 "fills": [False] * 3})
        # ②网格回补(核心动作): 未回补卖出腿分3档接回
        for leg in sold:
            if leg["qty"] <= 0:
                continue
            per = max(std_lot(leg["qty"] // 3), 100) if leg["qty"] >= 300 \
                else leg["qty"]
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
        lows.append(b["low"])
        if (not late and not flat and not one_way_dn and n_buy < 1
                and vwap and px / vwap - 1 <= -VWAP_DEV
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


def run_backtest(codes, shares_map, cash0=57010.07):
    data = {}
    for c in codes:
        m5 = quotes.fetch_kline_bars(c, 5, 400)
        cnt = defaultdict(int)
        for b in m5:
            cnt[b["dt"][:10]] += 1
        days = sorted(d for d, n in cnt.items() if n >= 20)[1:]   # 首日不全丢弃
        data[c] = {"m5": m5, "days": days}
    all_days = sorted(set().union(*[set(d["days"]) for d in data.values()]))
    names = {"603169": "兰石重装", "600557": "康缘药业", "300207": "欣旺达",
             "600480": "凌云股份", "300455": "航天智装", "600879": "航天电子"}
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
            if c not in stopped["OLD"]:
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
                nb = run_new(a2, bars, plan, base["prev_close"], base["ma20"])
                for p in fifo_pairs(a2.trades):
                    res["NEW"][d].append(p)
                detail["NEW"] += [(d, c, nm, a2.trades, a2.stop_cleared)]
                if nb:
                    carry[c] = nb[0]
    return res, detail, all_days


def report(res, detail, days, cash0=57010.07):
    print(f"回测区间: {days[0]} ~ {days[-1]} ({len(days)}个交易日) "
          f"底仓&现金: 统一初始快照")
    for tag in ("OLD", "NEW"):
        ps = [p for d in days for p in res[tag].get(d, [])]
        n, win = len(ps), sum(1 for p in ps if p["pnl"] > 0)
        pnl = sum(p["pnl"] for p in ps)
        fee = sum(t["fee"] for _, _, _, ts, _ in detail[tag] for t in ts)
        n_leg = sum(1 for _, _, _, ts, _ in detail[tag]
                    for t in ts if t["side"] in ("buy", "sell"))
        n_stop = sum(1 for _, _, _, _, sc in detail[tag] if sc)
        print(f"\n===== [{tag}] 配对{n}对 | 已回补胜率"
              f"{win / n * 100:.0f}%(胜{win}/亏{n - win}) | 净盈亏{pnl:+,.2f} "
              f"| 费用{fee:,.2f} | 成交腿{n_leg} 止损清仓{n_stop}次" if n else
              f"\n===== [{tag}] 无配对 | 成交腿{n_leg} 止损清仓{n_stop}次")
        by_day = defaultdict(float)
        for p in ps:
            by_day[p["dt"][:10]] += p["pnl"]
        for d in sorted(by_day):
            print(f"  {d}: {by_day[d]:+,.2f}")
        for p in sorted(ps, key=lambda x: x["dt"]):
            print(f"    {p['dt']} [{p['tag']}] {p['pnl']:+,.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="603169,600557,300207,600480,300455,600879")
    ap.add_argument("--shares", default="603169:14700,600557:5100,300207:1700,"
                                        "600480:8500,300455:1100,600879:2900")
    args = ap.parse_args()
    codes = args.codes.split(",")
    shares_map = {c: 1000 for c in codes}
    if args.shares:
        shares_map = {kv.split(":")[0]: int(kv.split(":")[1])
                      for kv in args.shares.split(",")}
    res, detail, days = run_backtest(codes, shares_map)
    report(res, detail, days)
