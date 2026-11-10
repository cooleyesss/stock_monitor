# -*- coding: utf-8 -*-
"""ATR风险预算仓位+动态止损(实验): v2.2信号语义不变, 仓位与退出改为风险预算连续表达
  入场sizing: target = clip(RISK_PCT/(SIZING_STOP*atr_pct), T_MIN, T_MAX)
    atr_pct=ATR/close — 高波动小仓/低波动满仓, 每笔风险预算=总资产1%
    趋势腿=base_target, 超跌/冰点腿=base_target*0.5(保持相对半仓语义)
  趋势腿退出阶梯(替代prod破MA10一次性清仓):
    ① 收盘破吊灯止损(入场以来最高收盘 - TRAIL_MULT*入场ATR) -> target=0清仓
    ② 收盘破MA10且本腿尚未减半 -> target=当前仓位一半(每腿只减一次, 之后由吊灯止损接管)
  超跌腿退出保持prod语义(中轨止盈/信号低点止损/8日超时)作对照
  成交=一次性调整到目标仓位(不做trend_cont式时间分批——P1已证时间分批净负贡献-2.69pp)
验证目标: 隔离"ATR sizing+阶梯退出"相对prod二元仓位的增量"""
import config_quant as config
from common import fees as _fees
from lab.minute import MinuteStrategy


def _apply_fill_immediate(st, code, name, date, o, act):
    """一次性调整到target_rate(整百取整, 费用与日线口径一致); 成交后target_rate=实际达成率
    (不残留目标差, 避免回放器②-b分批推进钩子追加噪音单)"""
    a = act["act"]
    reason = act.get("reason", "")
    total = st["cash"] + st["sh"] * o
    if total <= 0:
        return None
    cur = st["sh"] * o / total

    if a == "SELL":
        target = act.get("target_rate", 0.0)
        if target >= cur - 1e-9:
            return None
        if target <= 1e-9:
            q = st["sh"]                       # 清仓(A股卖出允许零股一次性申报)
        else:
            q = int((cur - target) * total / o // 100 * 100)
            if q < config.LOT_MIN:
                return None                    # 仓位太薄无法拆分, 维持现状
            q = min(q, st["sh"])
        amt = round(q * o, 2)
        f = _fees.sell_fee(amt, code)
        st["cash"] = st["cash"] + amt - f
        st["sh"] -= q
        st["pos_rate"] = 0.0 if st["sh"] == 0 \
            else st["sh"] * o / (st["cash"] + st["sh"] * o)
        st["target_rate"] = st["pos_rate"]
        if st["sh"] == 0:
            st["entry_px"] = 0.0
        return {"date": date, "code": code, "name": name, "side": "SELL",
                "qty": q, "price": round(o, 3), "amount": amt, "fee": f,
                "reason": reason, "pos_rate": round(st["pos_rate"], 4)}

    if a == "BUY":
        target = act.get("target_rate", 1.0)
        diff = target - cur
        if diff <= 1e-9:
            return None
        budget = min(diff * total, st["cash"])
        q = int(budget / o // 100 * 100)
        if q < config.LOT_MIN:
            return None
        amt = round(q * o, 2)
        f = _fees.buy_fee(amt, code)
        if st["cash"] < amt + f:
            return None
        old_cost = st["entry_px"] * st["sh"] if st["sh"] > 0 else 0.0
        st["cash"] = st["cash"] - amt - f
        st["sh"] += q
        st["entry_px"] = round((old_cost + q * o) / st["sh"], 4)
        st["pos_rate"] = st["sh"] * o / (st["cash"] + st["sh"] * o)
        st["target_rate"] = st["pos_rate"]
        return {"date": date, "code": code, "name": name, "side": "BUY",
                "qty": q, "price": round(o, 3), "amount": amt, "fee": f,
                "reason": reason, "pos_rate": round(st["pos_rate"], 4)}
    return None


class TrendAtrStrategy(MinuteStrategy):
    name = "trend_atr"
    desc = ("v2.2信号+ATR风险预算sizing(目标仓=clip(1%/(2·ATR%),0.2,1), 超跌腿减半)+"
            "趋势腿阶梯退出(破MA10减半一次/破吊灯止损[最高收盘-3ATR]清仓)")

    RISK_PCT = 0.01      # 每笔风险预算占总资产比例
    SIZING_STOP = 2.0    # sizing止损距离(ATR倍数)
    TRAIL_MULT = 3.0     # 吊灯止损ATR倍数
    T_MIN, T_MAX = 0.2, 1.0

    def init_state(self, cash):
        st = super().init_state(cash)
        st["sh_tr"] = True       # 腿型: True趋势/False超跌
        st["hold_d"] = 0         # 持仓天数(每日收盘决策时推进)
        st["high_close"] = 0.0   # 本腿入场以来最高收盘(吊灯止损锚)
        st["halved"] = False     # 本腿是否已因破MA10减半过
        return st

    def _target(self, atr, cl):
        """ATR风险预算目标仓位率: 止损距离SIZING_STOP*ATR时, 单笔亏损≈总资产RISK_PCT"""
        if atr <= 0 or cl <= 0:
            return self.T_MIN
        return min(self.T_MAX, max(self.T_MIN,
                                   self.RISK_PCT / (self.SIZING_STOP * atr / cl)))

    def decide(self, st, bars, j, o, mkt_bars):
        return None             # 分钟级钩子不用: 本策略只在每日收盘决策

    def decide_daily(self, st, ind, bars, j, o, mkt_cl):
        """信号判定逐语句复刻 adaptive.decide; 差异仅在仓位表达与趋势腿退出"""
        closes = ind["close"]
        ma20, ma5, ma10, ma60 = (ind["ma20"][j], ind["ma5"][j],
                                 ind["ma10"][j], ind["ma60"][j])
        lo, mid, rsi, atr = ind["lo"][j], ind["mid"][j], ind["rsi"][j], ind["atr"][j]
        cl, pc = closes[j], bars[j]["close"]
        if None in (ma20, ma5, ma10, lo, mid, rsi, atr) or atr <= 0 or j < 10:
            return None                      # 指标未就绪(hold_d也不推进)
        up_reg = cl > ma20 and ma5 > ma10
        ma20_up = ind["ma20"][j - 5] is not None and ma20 > ind["ma20"][j - 5]
        mom10 = cl / closes[j - 10] - 1
        up_ok = up_reg and ma60 is not None and cl > ma60 and ma20_up \
            and mom10 > 0.02
        if st["sh"] > 0:
            st["hold_d"] += 1
            if st["sh_tr"]:
                st["high_close"] = max(st["high_close"], cl)
                trail = st["high_close"] - self.TRAIL_MULT * st["entry_atr"]
                if st["entry_atr"] > 0 and cl < trail:
                    return {"act": "SELL", "reason": "吊灯止损", "target_rate": 0.0}
                if cl < ma10 and not st["halved"]:
                    st["halved"] = True
                    return {"act": "SELL", "reason": "破MA10减半",
                            "target_rate": round(st["pos_rate"] / 2.0, 4)}
            else:
                if pc >= mid:
                    return {"act": "SELL", "reason": "止盈", "target_rate": 0.0}
                if pc <= st["sig_low"]:
                    st["cool"] = 3
                    return {"act": "SELL", "reason": "止损", "target_rate": 0.0}
                if st["hold_d"] >= 8:
                    return {"act": "SELL", "reason": "超时", "target_rate": 0.0}
            return None
        if up_ok:
            t = round(self._target(atr, cl), 4)
            return {"act": "BUY_TREND", "reason": "趋势站上(仓%.2f)" % t,
                    "target_rate": t, "atr": atr}
        if not up_reg and j >= 20:
            if st["cool"] > 0:
                st["cool"] -= 1
                return None
            # 突发保险丝: 恐慌日(暴跌/放量长阴)次日不接飞刀
            yret = pc / bars[j - 1]["close"] - 1
            v5a = sum(ind["vol"][j - 5:j]) / 5
            panic = yret <= -0.07 or (v5a > 0 and ind["vol"][j] > 3 * v5a
                                      and yret <= -0.04)
            deep, why, sig = False, "", 0.0
            if not panic:
                deep = cl > lo and bars[j]["low"] < lo and rsi < 40
                if not (pc > bars[j]["open"]):
                    deep = False
                if (cl / closes[j - 5] - 1) < -0.12:
                    deep = False
                if deep and o is not None and (o - bars[j]["low"]) < 0.3 * atr:
                    deep = False             # 开盘距信号低点不足0.3ATR, 放弃
                if deep:
                    why, sig = "超跌", bars[j]["low"]
                # v2.2规则A: 市场连续冰点+个股超卖未崩 -> 左侧(受保险丝约束)
                if not deep and config.ICE_LEFT_EN and mkt_cl:
                    k0 = j - config.ICE_MKT_DAYS
                    mkt_ice = False
                    if k0 >= 0:
                        dn = sum(1 for k in range(k0 + 1, j + 1)
                                 if mkt_cl[k] < mkt_cl[k - 1])
                        cum = mkt_cl[j] / mkt_cl[k0] - 1
                        mkt_ice = dn >= config.ICE_MKT_DAYS \
                            or cum <= config.ICE_MKT_DROP
                    stk_ok = (rsi < config.ICE_RSI
                              and (cl / closes[j - 5] - 1) >= config.ICE_STK_DROP5
                              and pc < bars[j - 1]["close"])
                    if mkt_ice and stk_ok \
                            and (o is None or (o - bars[j]["low"]) >= 0.3 * atr):
                        deep, why, sig = True, "冰点左侧", bars[j]["low"]
            if deep:
                t = round(self._target(atr, cl) * 0.5, 4)
                return {"act": "BUY_SCALP", "reason": "%s(仓%.2f)" % (why, t),
                        "sig_low": sig, "need": round(sig + 0.3 * atr, 4),
                        "target_rate": t, "atr": atr}
        return None

    def apply_fill(self, st, code, name, date, o, pending, check_open):
        """一次性调整到目标仓位; 新腿首批成交记录入场ATR/吊灯锚/减半标志"""
        a = pending.get("act")
        # scalp开盘空间确认(与 adaptive.apply_fill check_open 语义一致)
        if a == "BUY_SCALP" and check_open and pending.get("need") is not None \
                and o < pending["need"]:
            return None
        mapped = dict(pending)
        mapped["act"] = "BUY" if a in ("BUY_TREND", "BUY_SCALP") else a
        rec = _apply_fill_immediate(st, code, name, date, o, mapped)
        if rec:
            if a == "BUY_TREND":
                st["sh_tr"] = True
            elif a == "BUY_SCALP":
                st["sh_tr"] = False
                st["sig_low"] = pending.get("sig_low", 0.0)
            if a in ("BUY_TREND", "BUY_SCALP") and st["sh"] == rec["qty"]:
                # 新腿首批成交(从0建起): 锚定吊灯止损与入场ATR
                st["entry_atr"] = pending.get("atr", 0.0)
                st["high_close"] = o
                st["halved"] = False
            if st["sh"] == 0:
                st["hold_d"] = 0
                st["high_close"] = 0.0
                st["halved"] = False
            rec["leg"] = "trend" if st["sh_tr"] else "scalp"
        return rec
