# -*- coding: utf-8 -*-
"""实验: 保本止盈(用户需求2的温和实现: 卖出点随股价上移但不截断上方利润)
规则: 趋势腿浮盈曾达 ARM(如8%)后"武装"保本线; 此后收盘跌回成本价下方 -> 保本卖出;
     未武装/未触发时仍按原 MA10 规则; 不设利润上限, 不截断趋势
状态扩展: st["my_cost"] 加权成本价, st["armed"] 武装标记"""
from core import adaptive

from lab.base import LabStrategy


class BreakEvenStrategy(LabStrategy):
    ARM = 0.08

    def init_state(self, cash):
        st = super().init_state(cash)
        st["my_cost"] = 0.0
        st["armed"] = 0
        return st

    def decide(self, st, ind, bars, j, o, mkt_cl):
        sh_before = st["sh"]
        d = adaptive.decide(st, ind, bars, j, o=o, mkt_cl=mkt_cl)
        if sh_before > 0 and st["sh_tr"] and st.get("my_cost", 0) > 0:
            cl = ind["close"][j]
            if not st["armed"] and cl >= st["my_cost"] * (1 + self.ARM):
                st["armed"] = 1
            if st["armed"] and d is None and cl < st["my_cost"]:
                return {"act": "SELL", "reason": f"保本止盈{int(self.ARM * 100)}%"}
        return d

    def apply_fill(self, st, code, name, date, o, pending, check_open):
        sh0 = st["sh"]
        c0 = st.get("my_cost", 0.0)
        rec = super().apply_fill(st, code, name, date, o, pending,
                                 check_open=check_open)
        if rec and rec["side"] == "BUY":
            sh1 = st["sh"]
            st["my_cost"] = (c0 * sh0 + o * (sh1 - sh0)) / sh1 if sh1 else 0.0
        elif rec and rec["side"] == "SELL" and st["sh"] == 0:
            st["my_cost"], st["armed"] = 0.0, 0
        return rec


class BE5(BreakEvenStrategy):
    ARM = 0.05
    name = "be5"
    desc = "保本止盈5%: 浮盈达5%后, 跌回成本即卖; 不截断上方利润"


class BE8(BreakEvenStrategy):
    ARM = 0.08
    name = "be8"
    desc = "保本止盈8%: 浮盈达8%后, 跌回成本即卖; 不截断上方利润"
