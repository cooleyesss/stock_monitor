# -*- coding: utf-8 -*-
"""分钟级回放器(5m K线): 与 lab/base.py 日线回放器完全同口径的分钟级模拟
语义(与 ttrade/strategy.py 实盘一致):
  每根5m bar收盘 decide -> pending -> 下一根bar开盘价成交(confirm_fill最后闸门)
  含全套费用(佣金/印花税/过户费), 资金/股数整百取整, 现金不做中间四舍五入
数据: Baostock 5m K线(common/quotes.fetch_minute_bars_bs, 半年133天6384根)
决策核: 复用 ttrade/vote.evaluate(同一纯函数, 杜绝与实盘漂移)
实验策略只需表达"决策差异", 成交/费用/权益计量框架统一, 保证对比公平"""
import datetime
import math

from common import quotes
import vote
from common import fees as _fees
from core.indicators import build_ind
import config_quant as config

BEG = "2026-04-01"
END = "2026-10-19"
IDX_CODE = "999999"  # 上证指数(大盘regime)


class MinuteStrategy:
    """分钟级实验策略基类(接口与日线 LabStrategy 对齐)
    子类最少只需覆盖 decide; confirm_fill/apply_fill 给高级改造预留"""

    name = "minute_base"
    desc = ""

    def init_state(self, cash):
        """每股独立子账户状态: cash可用资金, sh持股, pos_rate目标仓位率(0~1连续变量),
        target_rate目标仓位率(分批建仓中间态), entry_px入场均价, atr入场ATR,
        sig_low信号低点, hold_bars持仓bar数, cool冷却bar数"""
        return {"cash": float(cash), "sh": 0, "pos_rate": 0.0,
                "target_rate": 0.0, "entry_px": 0.0, "entry_atr": 0.0,
                "sig_low": 0.0, "hold_bars": 0, "cool": 0}

    def decide(self, st, bars, j, o, mkt_bars):
        """第j根5m bar收盘决策 -> pending dict 或 None
        bars: 当日5m bar列表(截至j), j: 当前bar索引, o: 下一根bar开盘价
        mkt_bars: 当日上证指数5m bar列表(regime用), 长度可能不一致(按时间对齐)"""
        raise NotImplementedError

    def decide_daily(self, st, ind, bars_d, j, o, mkt_cl):
        """日线级收盘决策钩子(每日最后一根5m bar收盘时调用, 与 adaptive.decide 同语义)
        ind/bars_d: 截至当日的日K指标与序列(窗口内优先bt_daily_cache同源, 其后5m聚合)
        j: 当日在日K序列的索引, o: 次日开盘价( scalp确认用, 与日线回测口径一致 )
        默认None(不决策); 趋势类实验策略覆盖此方法"""
        return None

    def confirm_fill(self, st, pending, o, prev_close):
        """下一根bar开盘成交前最后闸门: 返回False=撤销本笔"""
        return True

    def apply_fill(self, st, code, name, date, o, pending, check_open):
        """以开盘价o执行pending, 更新子账户资金/持股/仓位率"""
        return apply_fill_minute(st, code, name, date, o, pending, check_open)


def apply_fill_minute(st, code, name, date, o, act, check_open=False):
    """分钟级成交执行(与 adaptive.apply_fill 逐语句同构)
    连续仓位: pos_rate当前仓位率, target_rate目标仓位率
    分批: 每次调整仓位率差值的1/3(渐进达成, 避免单日冲击)
    数量/费用: 整百取整, 佣金/印花税/过户费与日线完全一致"""
    a = act["act"] if isinstance(act, dict) else act
    reason = act.get("reason", "") if isinstance(act, dict) else ""
    fees = _fees

    if a == "SELL":
        # 卖出: 按仓位率差值分批卖(默认全清, 支持部分卖)
        target = act.get("target_rate", 0.0) if isinstance(act, dict) else 0.0
        rate_diff = st["pos_rate"] - target
        if rate_diff <= 0:
            return None
        # 分批: 每次卖 rate_diff 的 1/3; 最后一批(剩余≤1/3)或整百取整不足一手时全出
        # (A股卖出允许零股一次性申报, 与日线版"卖出全部整百"语义等价收敛)
        if rate_diff <= 1.0 / 3.0 + 1e-9:
            q, sell_rate = st["sh"], rate_diff
        else:
            sell_rate = max(rate_diff / 3.0, min(rate_diff, 1.0 / 3.0))
            q = int(st["sh"] * (sell_rate / max(st["pos_rate"], 0.001)) // 100 * 100)
            if q < config.LOT_MIN:
                q, sell_rate = st["sh"], rate_diff
        if q <= 0:
            return None
        amt = round(q * o, 2)
        f = fees.sell_fee(amt, code)
        st["cash"] = st["cash"] + amt - f
        st["sh"] -= q
        st["pos_rate"] = max(0.0, st["pos_rate"] - sell_rate)
        st["target_rate"] = target
        if st["sh"] == 0:
            st["pos_rate"] = 0.0
            st["entry_px"] = 0.0
        return {"date": date, "code": code, "name": name, "side": "SELL",
                "qty": q, "price": round(o, 3), "amount": amt, "fee": f,
                "reason": reason, "pos_rate": round(st["pos_rate"], 4)}

    if a == "BUY":
        target = act.get("target_rate", 1.0) if isinstance(act, dict) else 1.0
        rate_diff = target - st["pos_rate"]
        if rate_diff <= 0:
            return None
        # 分批: 每次买 rate_diff 的 1/3; budget锚定总资产(现金+持仓市值按成交价估),
        # 而非当前现金比例——否则几何衰减导致3批后仓位推不满(现金残余~30%)
        buy_rate = max(rate_diff / 3.0, min(rate_diff, 1.0 / 3.0))
        total = st["cash"] + st["sh"] * o
        budget = min(total * buy_rate, st["cash"])
        q = int(budget / o // 100 * 100)
        if q < config.LOT_MIN:
            return None
        amt = round(q * o, 2)
        f = fees.buy_fee(amt, code)
        if st["cash"] < amt + f:
            return None
        old_cost = st["entry_px"] * st["sh"] if st["sh"] > 0 else 0.0
        st["cash"] = st["cash"] - amt - f
        st["sh"] += q
        st["pos_rate"] = min(1.0, st["pos_rate"] + buy_rate)
        st["target_rate"] = target
        st["entry_px"] = round((old_cost + q * o) / st["sh"], 4) if st["sh"] > 0 else 0.0
        return {"date": date, "code": code, "name": name, "side": "BUY",
                "qty": q, "price": round(o, 3), "amount": amt, "fee": f,
                "reason": reason, "pos_rate": round(st["pos_rate"], 4)}
    return None


def _daily_bars_of(minute_bars, day):
    """取某交易日的5m bar切片(day='2026-10-19')"""
    return [b for b in minute_bars if b["dt"][:10] == day]


def _agg_daily(day_bars):
    """当日5m bar聚合成日K bar(与东财日K同字段); 用于缓存截止日之后的日线序列延续"""
    return {"dt": day_bars[0]["dt"][:10],
            "open": day_bars[0]["open"],
            "close": day_bars[-1]["close"],
            "high": max(b["high"] for b in day_bars),
            "low": min(b["low"] for b in day_bars),
            "vol": sum(b["vol"] for b in day_bars),
            "amount": sum(b.get("amount", 0.0) for b in day_bars)}


def _build_quote(bar, prev_bar, day_bars):
    """从5m bar构造vote.evaluate需要的quote对象
    price=最新bar收盘, pct=较昨收涨跌幅, vr=量比(当日累计量/昨日同时段累计量)"""
    if not day_bars:
        return {"price": bar["close"], "pct": 0.0, "vr": 1.0}
    today_vol = sum(b["vol"] for b in day_bars)
    # 昨日同时段: 近似用昨日全天量(5m粒度无昨日分时, 用日K量比近似)
    # 简化: vr = 当日累计量 / 前一日bar数 * 当日bar数 / 昨日总bar数
    # 实际用: 当日累计量 / (当日bar数 * 前一日平均bar量)
    prev_day = None
    if len(day_bars) > 0:
        cur_day = day_bars[0]["dt"][:10]
        # 从minute_bars里找前一日
        prev_day = None
        # 简化: vr用当日量相对前一日量的比例
    return {"price": bar["close"], "pct": 0.0, "vr": 1.0}


def _build_day_bars(all_bars, day):
    """构造某交易日的5m bar列表(含开盘形成中bar)"""
    return [b for b in all_bars if b["dt"][:10] == day]


def _align_mkt_bars(stock_bars, idx_bars):
    """按时间对齐个股与指数5m bar(指数可能缺某些bar, 用前值填充)"""
    idx_map = {b["dt"]: b for b in idx_bars}
    out = []
    last = None
    for b in stock_bars:
        ib = idx_map.get(b["dt"], last)
        if ib is None:
            continue
        out.append(ib)
        last = ib
    return out


def _quote_from_bars(bar, day_bars, prev_close):
    """从5m bar构造vote.evaluate的quote对象
    price=当前bar收盘, pct=较昨收涨跌幅, vr=当日累计量/前一日总bar数*当日bar数"""
    price = bar["close"]
    if prev_close and prev_close > 0:
        pct = price / prev_close - 1.0
    else:
        pct = 0.0
    # vr: 当日累计量 / (当日bar数 * 前一日平均每bar量)
    # 简化: 用当日量相对前一日总bar量的比例
    today_vol = sum(b["vol"] for b in day_bars)
    today_bars = len(day_bars)
    # 近似: vr = 当日累计量 / 当日bar数 (无昨日数据时用1.0)
    vr = today_vol / max(today_bars, 1) / max(today_vol / max(today_bars, 1), 1) if today_vol > 0 else 1.0
    # 简化: vr=1.0 (5m粒度无可靠昨日分时量比, 用1.0中性)
    vr = 1.0
    return {"price": price, "pct": pct, "vr": vr}


def replay_stock_minute(code, name, capital, strategy, start=BEG, end=END):
    """单标的分钟级回放 -> {code,name,ops,equity,return,mdd,trades,final}
    与日线 replay_stock 同循环: ①开盘fill昨日pending ②收盘decide->新pending
    分钟级语义: 每根5m bar收盘都是一次决策点"""
    # 拉取5m bars
    bars = quotes.fetch_minute_bars_bs(code, freq="5", start=start, end=end)
    if not bars:
        return {"code": code, "name": name, "ops": [], "equity": [capital],
                "return": 0.0, "mdd": 0.0, "trades": 0, "final": capital}
    # 拉取指数5m bars(regime用)
    idx_bars = quotes.fetch_minute_bars_bs(IDX_CODE, freq="5", start=start, end=end) \
        if IDX_CODE else []
    mkt_aligned = _align_mkt_bars(bars, idx_bars) if idx_bars else []

    # 日线历史(decide_daily钩子用): 与日线回测同源bt_daily_cache;
    # 缓存覆盖段内每日决策直接用缓存日K(与prod基线严格同源同信号),
    # 缓存截止日后用5m聚合bar逐日延续(synth_tail), 保证指标序列连续无缺口
    from lab.base import load_universe
    uni_d = load_universe()
    cached = (uni_d.get(code) or {}).get("bars") or []
    cached_di = {b["dt"][:10]: k for k, b in enumerate(cached)}
    idx_cl = {b["dt"][:10]: b["close"]
              for b in ((uni_d.get(IDX_CODE) or {}).get("bars") or [])}
    synth_tail = []

    st = strategy.init_state(cash=capital)
    pending, ops, equity = None, [], []

    # 按交易日分组
    days = sorted(set(b["dt"][:10] for b in bars))
    day_bars_map = {d: _daily_bars_of(bars, d) for d in days}

    # 逐bar遍历
    for i in range(1, len(bars)):
        cur_day = bars[i]["dt"][:10]
        day_bars = day_bars_map[cur_day]
        # 当日bar在day_bars中的索引
        j = next((k for k, b in enumerate(day_bars) if b["dt"] == bars[i]["dt"]), None)
        if j is None:
            continue
        # 昨收: 前一日最后一根bar收盘
        prev_close = None
        prev_day_idx = days.index(cur_day)
        if prev_day_idx > 0:
            prev_day_bars = day_bars_map[days[prev_day_idx - 1]]
            if prev_day_bars:
                prev_close = prev_day_bars[-1]["close"]
        if prev_close is None:
            prev_close = bars[i - 1]["close"]

        # ① 开盘fill昨日pending(分钟级: 用当前bar开盘价)
        if pending:
            if strategy.confirm_fill(st, pending, bars[i]["open"],
                                     bars[i - 1]["close"]):
                rec = strategy.apply_fill(st, code, name, bars[i]["dt"],
                                          bars[i]["open"], pending,
                                          check_open=not pending.get("_chk"))
                if rec:
                    ops.append(rec)
            pending = None

        # ② 收盘决策 -> 新pending(优先级: 日线收盘钩子 > 分批推进 > 分钟级钩子)
        o_nxt = bars[i + 1]["open"] if i + 1 < len(bars) else None
        is_close = (i + 1 >= len(bars)) or (bars[i + 1]["dt"][:10] != cur_day)
        # ②-a 日线级收盘决策(每日最后一根5m bar): 语义=当日收盘决策, 次日开盘成交
        if pending is None and is_close:
            if cur_day in cached_di:
                bars_d = cached[:cached_di[cur_day] + 1]
            else:
                bars_d = cached + synth_tail + [_agg_daily(day_bars[:j + 1])]
            if len(bars_d) >= 12:
                ind_d = build_ind(bars_d)
                mc = [idx_cl.get(b["dt"][:10]) for b in bars_d]
                mc = None if any(v is None for v in mc) else mc
                d2 = strategy.decide_daily(st, ind_d, bars_d, len(bars_d) - 1,
                                           o=o_nxt, mkt_cl=mc)
                if d2:
                    d2["_chk"] = o_nxt is not None
                    pending = d2
        # ②-b 分批推进(双向): target_rate未达成时每根bar推进1/3差值
        if pending is None and st["sh"] > 0:
            if st["target_rate"] > st["pos_rate"]:
                pending = {"act": "BUY", "reason": "分批推进",
                           "target_rate": st["target_rate"],
                           "_chk": o_nxt is not None}
            elif st["target_rate"] < st["pos_rate"]:
                pending = {"act": "SELL", "reason": "分批退出",
                           "target_rate": st["target_rate"],
                           "_chk": o_nxt is not None}
        # ②-c 分钟级策略钩子(日内决策类实验策略用)
        if pending is None:
            mkt_day_bars = None
            if mkt_aligned:
                mkt_day_bars = [b for b in mkt_aligned
                                if b["dt"][:10] == cur_day]
                mkt_j = next((k for k, b in enumerate(mkt_day_bars)
                              if b["dt"] == bars[i]["dt"]), None)
                mkt_day_bars = (mkt_day_bars[:mkt_j + 1]
                                if mkt_j is not None else None)
            d = strategy.decide(st, day_bars[:j + 1], j, o=o_nxt,
                                mkt_bars=mkt_day_bars)
            if d:
                d["_chk"] = o_nxt is not None
                pending = d
        # ②-d 日终: 缓存截止日后的交易日以5m聚合延续日线序列
        if is_close and cur_day not in cached_di:
            synth_tail.append(_agg_daily(day_bars))
        equity.append(round(st["cash"] + st["sh"] * bars[i]["close"], 2))

    final = equity[-1] if equity else capital
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak else 0)
    return {"code": code, "name": name, "ops": ops, "equity": equity,
            "return": round(final / capital - 1, 4), "mdd": round(mdd, 4),
            "trades": sum(1 for o in ops if o["side"] == "BUY"),
            "final": round(final, 2)}


def run_compare_minute(names, capital, codes, start=BEG, end=END):
    """分钟级对比: 同一回放器跑 prod(日线基线) vs 分钟级实验策略
    prod基线: 日线级回放(lab.base.replay_stock), 分钟级实验策略: 5m回放"""
    from lab.base import load_universe, replay_stock
    from lab.strategies import REGISTRY
    out = {}
    # 日线基线(prod)
    uni = load_universe()
    for nm in names:
        if nm == "prod":
            strat = REGISTRY["prod"]()
            out[nm] = {c: replay_stock(uni, c, capital, strat) for c in codes}
        else:
            strat = REGISTRY[nm]()
            out[nm] = {c: replay_stock_minute(c, uni[c].get("name", c),
                                              capital, strat, start, end)
                       for c in codes}
    return out
