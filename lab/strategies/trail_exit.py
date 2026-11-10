# -*- coding: utf-8 -*-
"""实验: 移动止盈(用户需求2: 卖出点随股价变动上移)
规则: 趋势腿持仓跟踪进场后最高收盘价 hi, 收盘回撤超 PCT 触发"移动止盈"卖出;
     与原 MA10 破位双轨并行, 先到先出; scalp 腿/空仓逻辑不变
状态扩展: st["hi"] (买入时重置为成交价, 每日收盘 max 更新)"""
from core import adaptive

from lab.base import LabStrategy


class TrailExitStrategy(LabStrategy):
    PCT = 0.07

    def init_state(self, cash):
        st = super().init_state(cash)
        st["hi"] = 0.0
        return st

    def decide(self, st, ind, bars, j, o, mkt_cl):
        sh_before = st["sh"]
        d = adaptive.decide(st, ind, bars, j, o=o, mkt_cl=mkt_cl)
        if sh_before > 0 and st["sh_tr"]:
            cl = ind["close"][j]
            st["hi"] = max(st.get("hi", 0.0), cl)
            if d is None and st["hi"] > 0 and cl <= st["hi"] * (1 - self.PCT):
                return {"act": "SELL", "reason": f"移动止盈{int(self.PCT * 100)}%"}
        return d

    def apply_fill(self, st, code, name, date, o, pending, check_open):
        rec = super().apply_fill(st, code, name, date, o, pending,
                                 check_open=check_open)
        if rec and rec["side"] == "BUY":
            st["hi"] = o          # 新一轮持有, 跟踪基准=本次买入价
        return rec


class Trail5(TrailExitStrategy):
    PCT = 0.05
    name = "trail5"
    desc = "移动止盈5%: 跟踪最高收盘回撤5%即卖, 与破MA10双轨先到先出"


class Trail7(TrailExitStrategy):
    PCT = 0.07
    name = "trail7"
    desc = "移动止盈7%: 跟踪最高收盘回撤7%即卖, 与破MA10双轨先到先出"


class Trail10(TrailExitStrategy):
    PCT = 0.10
    name = "trail10"
    desc = "移动止盈10%: 跟踪最高收盘回撤10%即卖, 与破MA10双轨先到先出"
