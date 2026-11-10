# -*- coding: utf-8 -*-
"""S2v2 VWAP回归 + RSI6超卖确认 + 大盘regime(用户2026-10-20要求: 必含RSI, 必看大盘)
在s2_vwap_rev基础上加两个全局过滤, 修复v1"接飞刀"缺陷:
  ①RSI确认: 5m级 RSI6(Cutler, 最近6根5m涨跌) < 30 = 盘中超卖才接, 非超卖的普通偏离不做
  ②大盘regime: 上证指数收盘 > MA20 才允许入场(昨日收盘判定, 无未来函数;
    指数数据缺失时 mkt_ok=False 保守不入场)
其余(偏离阈值0.8ATR/收阳反转/恐慌过滤/回归止盈/破低止损/2日超时/T+1)与v1一致"""
from lab.minute import MinuteStrategy, apply_fill_minute

DEV_ATR = 0.8      # 偏离阈值(ATR倍数)
STOP_BUF = 0.3     # 止损缓冲(ATR倍数)
POS = 0.5          # 目标仓位率
PANIC_DAY = -0.04  # 当日累计跌幅恐慌过滤
MAX_HOLD_D = 2     # 超时(交易日)
MIN_J = 20         # 开盘100分钟后才参与(RSI6与VWAP稳定)
RSI6_OVS = 30.0    # 5m RSI6超卖阈值


def _rsi6(bars):
    """Cutler RSI6: 最近6根5m bar涨跌(简单平均, 无需平滑暖机)"""
    if len(bars) < 7:
        return None
    ch = [bars[k]["close"] - bars[k - 1]["close"] for k in range(len(bars) - 6, len(bars))]
    up = sum(x for x in ch if x > 0) / 6
    dn = -sum(x for x in ch if x < 0) / 6
    if up + dn <= 0:
        return 50.0
    return 100.0 * up / (up + dn)


class S2VwapRev2Strategy(MinuteStrategy):
    name = "s2_vwap_rev2"
    desc = (f"S2v2 VWAP回归+RSI6+大盘: 偏离>={DEV_ATR}ATR且5mRSI6<{RSI6_OVS:.0f}且收阳 "
            f"且指数>MA20 -> 半仓; 回归止盈/破低止损/{MAX_HOLD_D}日超时; T+1")

    def init_state(self, cash):
        st = super().init_state(cash)
        st.update({"pd_atr": None, "pd_close": None, "mkt_ok": False,
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
        if j < MIN_J or day == st["cool_date"] or not st["mkt_ok"]:
            return None                           # 大盘弱市不接刀
        day_ret = cur["close"] / st["pd_close"] - 1
        if day_ret <= PANIC_DAY:
            return None
        r6 = _rsi6(bars)
        if r6 is None or r6 >= RSI6_OVS:
            return None                           # 非盘中超卖不做
        if (vw - cur["close"]) >= DEV_ATR * st["pd_atr"] \
                and cur["close"] > cur["open"]:
            sig = min(b["low"] for b in bars)
            return {"act": "BUY", "reason": "VWAP偏离+RSI超卖", "target_rate": POS,
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
        st["mkt_ok"] = False
        if mkt_cl and j >= 19:
            st["mkt_ok"] = mkt_cl[j] > sum(mkt_cl[j - 19:j + 1]) / 20
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