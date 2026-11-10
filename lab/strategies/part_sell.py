# -*- coding: utf-8 -*-
"""实验: 分批减仓(用户需求3: 破位不一笔清仓)
规则: 趋势腿首次收盘破 MA10 -> 次日开盘只卖出半仓(留一半确认);
     若次日收盘仍收在 MA10 下方 -> 确认破位清仓剩余; 若收回 MA10 上方 -> 解除
     警戒, 剩余半仓继续按原规则持有(假破位保住一半趋势)
状态扩展: st["part"] 0=满仓 1=已减半待确认; 减半卖出后 hold_d 不清零"""
from core import adaptive

from lab.base import LabStrategy


class PartSellStrategy(LabStrategy):
    name = "part_sell"
    desc = "分批减仓: 破MA10先卖半仓, 次日仍破位才清仓; 收回均线则剩余继续持有"

    def init_state(self, cash):
        st = super().init_state(cash)
        st["part"] = 0
        return st

    def decide(self, st, ind, bars, j, o, mkt_cl):
        sh_before = st["sh"]
        d = adaptive.decide(st, ind, bars, j, o=o, mkt_cl=mkt_cl)
        if sh_before > 0 and st["sh_tr"]:
            cl, ma10 = ind["close"][j], ind["ma10"][j]
            if ma10 is not None:
                if cl < ma10:
                    if d and d["act"] == "SELL" and not st["part"]:
                        d = dict(d, reason="破位减半", frac=0.5)
                    elif d and d["act"] == "SELL" and st["part"]:
                        d = dict(d, reason="破位确认")
                elif st["part"]:
                    st["part"] = 0      # 收回均线: 解除警戒, 半仓继续持有
        return d

    def apply_fill(self, st, code, name, date, o, pending, check_open):
        if pending.get("act") == "SELL" and pending.get("frac"):
            q = int(st["sh"] * pending["frac"] // 100 * 100)
            if q < 100:                  # 半仓不足一手: 直接全卖
                return super().apply_fill(st, code, name, date, o, pending,
                                          check_open=check_open)
            amt = round(q * o, 2)
            f = adaptive._fees.sell_fee(amt, code)
            st["cash"] += amt - f
            st["sh"] -= q
            st["part"] = 1               # hold_d 保留: 剩余仓位的持有天数连续
            return {"date": date, "code": code, "name": name, "side": "SELL",
                    "qty": q, "price": round(o, 3), "amount": amt, "fee": f,
                    "reason": pending.get("reason", ""), "leg": "trend"}
        rec = super().apply_fill(st, code, name, date, o, pending,
                                 check_open=check_open)
        if rec and rec["side"] == "SELL":
            st["part"] = 0
        return rec
