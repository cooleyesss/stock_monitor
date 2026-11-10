# -*- coding: utf-8 -*-
"""S1 ATR突破(候选·短线): 盘中5m收盘放量突破"昨高+0.5ATR"入场, ATR止损+吊灯止盈
全局规则(不逐股调参):
  入场: bar收盘 > 昨日最高 + 0.5*ATR14 且 当前bar量 >= 1.5x 前20根5m均量 -> 半仓
  退出: ①固定止损 收盘 < 入场均价 - 1.5*ATR ②吊灯 收盘 < 持仓期最高收盘 - 2.5*ATR
        ③超时 持仓满3个交易日收盘清
  约束: T+1(买入当日不卖); 卖出当日冷却不再买(防whipsaw)
跨日状态(pd_high/pd_atr等)由 decide_daily 每日收盘维护"""
from lab.minute import MinuteStrategy, apply_fill_minute

BRK_BUF = 0.5      # 突破缓冲(ATR倍数)
VOL_X = 1.5        # 放量倍数(对前20根5m均量)
POS = 0.5          # 目标仓位率
STOP_ATR = 1.5     # 固定止损(ATR倍数)
TRAIL_ATR = 2.5    # 吊灯止盈(ATR倍数)
MAX_HOLD_D = 3     # 超时(交易日)
MIN_J = 20         # 均量就绪所需当日bar数


class S1AtrBreakStrategy(MinuteStrategy):
    name = "s1_atr_break"
    desc = (f"S1 ATR突破: 5m收盘>昨高+{BRK_BUF}ATR且量>={VOL_X}x均量 -> 半仓; "
            f"止损{STOP_ATR}ATR/吊灯{TRAIL_ATR}ATR/{MAX_HOLD_D}日超时; T+1")

    def init_state(self, cash):
        st = super().init_state(cash)
        st.update({"pd_high": None, "pd_atr": None,
                   "hold_d": 0, "hi_close": 0.0, "atr_e": 0.0,
                   "entry_date": "", "cool_date": ""})
        return st

    def decide(self, st, bars, j, o, mkt_bars):
        cur = bars[-1]
        day = cur["dt"][:10]
        if st["sh"] > 0:
            st["hi_close"] = max(st["hi_close"], cur["close"])
            if day == st["entry_date"]:
                return None                       # T+1: 买入当日不卖
            if cur["close"] < st["entry_px"] - STOP_ATR * st["atr_e"]:
                return {"act": "SELL", "reason": "ATR止损", "target_rate": 0.0}
            if cur["close"] < st["hi_close"] - TRAIL_ATR * st["atr_e"]:
                return {"act": "SELL", "reason": "吊灯止盈", "target_rate": 0.0}
            return None
        # 空仓: 突破入场
        if st["pd_high"] is None or st["pd_atr"] is None or j < MIN_J:
            return None
        if day == st["cool_date"]:
            return None                           # 卖出当日冷却
        v20 = sum(b["vol"] for b in bars[-21:-1]) / 20
        if v20 <= 0:
            return None
        if cur["close"] > st["pd_high"] + BRK_BUF * st["pd_atr"] \
                and cur["vol"] >= VOL_X * v20:
            return {"act": "BUY", "reason": "ATR突破", "target_rate": POS,
                    "atr_e": st["pd_atr"]}
        return None

    def decide_daily(self, st, ind, bars, j, o, mkt_cl):
        pend = None
        if st["sh"] > 0:
            st["hold_d"] += 1
            if st["hold_d"] >= MAX_HOLD_D:
                pend = {"act": "SELL", "reason": "超时", "target_rate": 0.0}
        # 维护跨日状态(无论是否交易都更新)
        st["pd_high"] = bars[j]["high"]
        st["pd_atr"] = ind["atr"][j]
        return pend

    def apply_fill(self, st, code, name, date, o, pending, check_open):
        rec = apply_fill_minute(st, code, name, date, o, pending, check_open)
        if rec:
            if rec["side"] == "BUY":
                if pending.get("atr_e"):
                    st["atr_e"] = pending["atr_e"]
                st["entry_date"] = date[:10]
                st["hi_close"] = max(st["hi_close"], o)
            if st["sh"] == 0:
                st["hold_d"] = 0
                st["hi_close"] = 0.0
                st["entry_date"] = ""
                if rec["side"] == "SELL":
                    st["cool_date"] = date[:10]
        return rec