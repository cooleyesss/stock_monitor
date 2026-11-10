# -*- coding: utf-8 -*-
"""实验: 分批建仓(用户需求3: 不一笔满仓)
规则: BUY_TREND 首笔只投入 60% 资金; 买入后收盘仍站上 MA5 则次日开盘加仓
     剩余 40%; 跌破 MA5 则不加仓、资金留守(破位卖出时一并回笼)
状态扩展: st["add"] 待加仓资金; 首笔与加仓均为整百股"""
from core import adaptive

from lab.base import LabStrategy


class ScaleInStrategy(LabStrategy):
    FRAC1 = 0.6
    name = "scale_in"
    desc = "分批建仓: 趋势首笔60%资金, 站上MA5次日加仓剩余40%, 跌破MA5不加仓"

    def init_state(self, cash):
        st = super().init_state(cash)
        st["add"] = 0.0
        return st

    def decide(self, st, ind, bars, j, o, mkt_cl):
        sh_before = st["sh"]
        d = adaptive.decide(st, ind, bars, j, o=o, mkt_cl=mkt_cl)
        if sh_before > 0 and st["sh_tr"] and d is None and st["add"] > 0:
            cl, ma5 = ind["close"][j], ind["ma5"][j]
            if ma5 is not None and cl > ma5:
                return {"act": "BUY_ADD", "reason": "分批加仓"}
        return d

    def apply_fill(self, st, code, name, date, o, pending, check_open):
        if pending.get("act") == "BUY_ADD":
            q = int(st["add"] / o // 100 * 100)
            st["add"] = 0.0
            if q >= 100:
                amt = round(q * o, 2)
                f = adaptive._fees.buy_fee(amt, code)
                if st["cash"] >= amt + f:
                    st["cash"] -= amt + f
                    st["sh"] += q
                    return {"date": date, "code": code, "name": name,
                            "side": "BUY", "qty": q, "price": round(o, 3),
                            "amount": amt, "fee": f, "reason": "分批加仓",
                            "leg": "trend"}
            return None
        if pending.get("act") == "BUY_TREND":
            q = int(st["cash"] * self.FRAC1 / o // 100 * 100)
            if q >= 100:
                amt = round(q * o, 2)
                f = adaptive._fees.buy_fee(amt, code)
                if st["cash"] >= amt + f:
                    st["cash"] -= amt + f
                    st["sh"] += q
                    st["add"] = st["cash"]     # 剩余现金全部作为待加仓额度
                    return {"date": date, "code": code, "name": name,
                            "side": "BUY", "qty": q, "price": round(o, 3),
                            "amount": amt, "fee": f, "reason": "趋势站上·首笔",
                            "leg": "trend"}
            return None
        rec = super().apply_fill(st, code, name, date, o, pending,
                                 check_open=check_open)
        if rec and rec["side"] == "SELL":
            st["add"] = 0.0
        return rec
