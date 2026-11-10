# -*- coding: utf-8 -*-
"""S2 VWAP回归(候选·超短): 盘中价格深跌偏离当日VWAP后出现反转bar -> 买回归
全局规则(不逐股调参):
  入场: 10:00后(j>=6) 且 (VWAP-收盘) >= 0.8*ATR14 且 当前bar收阳(反转迹象)
        且 当日累计跌幅 > -4%(恐慌过滤, 不接飞刀) -> 半仓, sig_low=当日最低
  退出: ①回归 收盘 >= VWAP 止盈 ②止损 收盘 < sig_low - 0.3*ATR
        ③超时 持仓满2个交易日收盘清
  约束: T+1(买入当日不卖); 卖出当日冷却不再买
VWAP=当日累计成交额/累计成交量(Baostock 5m bar含amount)"""
from lab.minute import MinuteStrategy, apply_fill_minute

DEV_ATR = 0.8      # 偏离阈值(ATR倍数)
STOP_BUF = 0.3     # 止损缓冲(ATR倍数)
POS = 0.5          # 目标仓位率
PANIC_DAY = -0.04  # 当日累计跌幅恐慌过滤
MAX_HOLD_D = 2     # 超时(交易日)
MIN_J = 6          # 开盘30分钟后才参与(VWAP稳定)


class S2VwapRevStrategy(MinuteStrategy):
    name = "s2_vwap_rev"
    desc = (f"S2 VWAP回归: 偏离VWAP>={DEV_ATR}ATR且收阳(日跌{PANIC_DAY:.0%}过滤) -> 半仓; "
            f"回归VWAP止盈/破低{STOP_BUF}ATR止损/{MAX_HOLD_D}日超时; T+1")

    def init_state(self, cash):
        st = super().init_state(cash)
        st.update({"pd_atr": None, "pd_close": None,
                   "hold_d": 0, "entry_date": "", "cool_date": ""})
        return st

    @staticmethod
    def _vwap(bars):
        v = sum(b["vol"] for b in bars)
        if v <= 0:
            return None
        return sum(b.get("amount", 0.0) for b in bars) / v

    def decide(self, st, bars, j, o, mkt_bars):
        cur = bars[-1]
        day = cur["dt"][:10]
        if st["pd_atr"] is None or st["pd_close"] is None:
            return None
        vw = self._vwap(bars)
        if vw is None:
            return None
        if st["sh"] > 0:
            if day == st["entry_date"]:
                return None                       # T+1
            if cur["close"] >= vw:
                return {"act": "SELL", "reason": "VWAP回归", "target_rate": 0.0}
            if st["sig_low"] and cur["close"] < st["sig_low"] - STOP_BUF * st["pd_atr"]:
                return {"act": "SELL", "reason": "止损", "target_rate": 0.0}
            return None
        # 空仓: 深跌偏离+反转bar
        if j < MIN_J or day == st["cool_date"]:
            return None
        day_ret = cur["close"] / st["pd_close"] - 1
        if day_ret <= PANIC_DAY:
            return None                           # 恐慌日不接飞刀
        if (vw - cur["close"]) >= DEV_ATR * st["pd_atr"] \
                and cur["close"] > cur["open"]:
            sig = min(b["low"] for b in bars)
            return {"act": "BUY", "reason": "VWAP偏离回归", "target_rate": POS,
                    "sig_low": sig}
        return None

    def decide_daily(self, st, ind, bars, j, o, mkt_cl):
        pend = None
        if st["sh"] > 0:
            st["hold_d"] += 1
            if st["hold_d"] >= MAX_HOLD_D:
                pend = {"act": "SELL", "reason": "超时", "target_rate": 0.0}
        st["pd_atr"] = ind["atr"][j]
        st["pd_close"] = bars[j]["close"]
        return pend

    def apply_fill(self, st, code, name, date, o, pending, check_open):
        rec = apply_fill_minute(st, code, name, date, o, pending, check_open)
        if rec:
            if rec["side"] == "BUY":
                st["entry_date"] = date[:10]
                if pending.get("sig_low"):
                    st["sig_low"] = pending["sig_low"]
            if st["sh"] == 0:
                st["hold_d"] = 0
                st["entry_date"] = ""
                st["sig_low"] = 0.0
                if rec["side"] == "SELL":
                    st["cool_date"] = date[:10]
        return rec