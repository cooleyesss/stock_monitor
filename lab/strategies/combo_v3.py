# -*- coding: utf-8 -*-
"""实验: 组合策略(用户需求2+3的合成)
规则: 分批建仓(首笔60%+站上MA5加仓40%) + 保本止盈(浮盈达8%武装, 跌回成本即卖)
     + 原有破MA10退出/超跌/冰点/保险丝全部保留
继承 ScaleInStrategy 的分批建仓, 叠加 BreakEven 的保本线"""
from core import adaptive

from lab.strategies.break_even import BreakEvenStrategy
from lab.strategies.scale_in import ScaleInStrategy


class ComboStrategy(ScaleInStrategy):
    ARM = 0.08
    name = "combo"
    desc = "组合: 分批建仓60/40 + 保本止盈8%武装 + 破MA10退出(全规则保留)"

    def init_state(self, cash):
        st = super().init_state(cash)      # scale_in 的 add 字段
        st["my_cost"] = 0.0
        st["armed"] = 0
        return st

    def decide(self, st, ind, bars, j, o, mkt_cl):
        sh_before = st["sh"]
        d = adaptive.decide(st, ind, bars, j, o=o, mkt_cl=mkt_cl)
        if sh_before > 0 and st["sh_tr"]:
            cl = ind["close"][j]
            # 保本止盈(优先于加仓: 武装后跌回成本直接走)
            if st.get("my_cost", 0) > 0:
                if not st["armed"] and cl >= st["my_cost"] * (1 + self.ARM):
                    st["armed"] = 1
                if st["armed"] and d is None and cl < st["my_cost"]:
                    st["add"] = 0.0
                    return {"act": "SELL", "reason": "保本止盈8%"}
            # 分批加仓(原 scale_in 逻辑)
            if d is None and st["add"] > 0:
                ma5 = ind["ma5"][j]
                if ma5 is not None and cl > ma5:
                    return {"act": "BUY_ADD", "reason": "分批加仓"}
        return d

    def apply_fill(self, st, code, name, date, o, pending, check_open):
        sh0, c0 = st["sh"], st.get("my_cost", 0.0)
        rec = super().apply_fill(st, code, name, date, o, pending,
                                 check_open=check_open)   # scale_in 语义
        if rec and rec["side"] == "BUY":
            sh1 = st["sh"]
            st["my_cost"] = (c0 * sh0 + o * (sh1 - sh0)) / sh1 if sh1 else 0.0
        elif rec and rec["side"] == "SELL" and st["sh"] == 0:
            st["my_cost"], st["armed"] = 0.0, 0
        return rec


class Scale50(ScaleInStrategy):
    FRAC1 = 0.5
    name = "scale50"
    desc = "分批建仓50/50: 趋势首笔50%资金, 站上MA5次日加仓剩余50%"


class Scale70(ScaleInStrategy):
    FRAC1 = 0.7
    name = "scale70"
    desc = "分批建仓70/30: 趋势首笔70%资金, 站上MA5次日加仓剩余30%"
