# -*- coding: utf-8 -*-
"""S3v2 放量突破+缩量回踩 + RSI过滤 + 大盘regime(用户2026-10-20要求: 必含RSI, 必看大盘)
在s3_vol_pb(参数扫描平台中心值)基础上只加两个全局过滤, 其余规则不变:
  ①RSI过滤: 回踩入场当日 日RSI14 < 60(不追高热, 缩量回踩应处于中温区)
  ②大盘regime: 上证指数收盘 > MA20 才允许入场(昨日收盘判定, 无未来函数;
    指数数据缺失时 mkt_ok=False 保守不入场)
突破/回踩/退出/仓位/T+1规则与s3_vol_pb逐语句一致"""
from lab.minute import MinuteStrategy, apply_fill_minute

BRK_VOL_X = 2.0    # 突破放量倍数
PB_DAYS = 5        # 待回踩有效期(交易日)
PB_ZONE = 0.3      # 回踩区间(ATR倍数)
PB_VOL_SHR = 0.6   # 缩量倍数
POS = 0.5          # 目标仓位率
TAKE_ATR = 2.0     # 止盈(ATR倍数)
MAX_HOLD_D = 5     # 超时(交易日)
MIN_J = 20         # 均量就绪所需当日bar数
RSI_MAX = 60.0     # 入场RSI14上限(日级)


class S3VolPb2Strategy(MinuteStrategy):
    name = "s3_vol_pb2"
    desc = (f"S3v2 放量回踩+RSI+大盘: 在S3基础上加 日RSI14<{RSI_MAX:.0f} 且 "
            f"指数>MA20 双过滤(其余规则同S3)")

    def init_state(self, cash):
        st = super().init_state(cash)
        st.update({"pd_high": None, "pd_atr": None, "pd_rsi": None,
                   "mkt_ok": False,
                   "stage": 0, "brk_px": 0.0, "brk_vol": 0.0, "brk_days": 0,
                   "hold_d": 0, "entry_date": "", "cool_date": ""})
        return st

    def decide(self, st, bars, j, o, mkt_bars):
        cur = bars[-1]
        day = cur["dt"][:10]
        if st["sh"] > 0:
            if day == st["entry_date"]:
                return None                       # T+1
            if st["sig_low"] and cur["close"] < st["sig_low"]:
                return {"act": "SELL", "reason": "止损", "target_rate": 0.0}
            if st["pd_atr"] and cur["close"] >= st["brk_px"] + TAKE_ATR * st["pd_atr"]:
                return {"act": "SELL", "reason": "止盈", "target_rate": 0.0}
            return None
        if st["pd_high"] is None or st["pd_atr"] is None or j < MIN_J:
            return None
        if day == st["cool_date"] or not st["mkt_ok"]:
            return None                           # 大盘弱市不入场
        if st["stage"] == 0:
            v20 = sum(b["vol"] for b in bars[-21:-1]) / 20
            if v20 <= 0:
                return None
            if cur["close"] > st["pd_high"] and cur["vol"] >= BRK_VOL_X * v20:
                st["stage"] = 1
                st["brk_px"] = cur["close"]
                st["brk_vol"] = cur["vol"]
                st["brk_days"] = 0
            return None
        if st["brk_days"] >= 1:
            zone = PB_ZONE * st["pd_atr"]
            if cur["low"] <= st["brk_px"] + zone \
                    and cur["close"] >= st["brk_px"] - zone \
                    and cur["close"] > cur["open"] \
                    and cur["vol"] <= PB_VOL_SHR * st["brk_vol"] \
                    and st["pd_rsi"] is not None and st["pd_rsi"] < RSI_MAX:
                st["stage"] = 0
                return {"act": "BUY", "reason": "缩量回踩", "target_rate": POS,
                        "sig_low": cur["low"]}
        return None

    def decide_daily(self, st, ind, bars, j, o, mkt_cl):
        pend = None
        if st["sh"] > 0:
            st["hold_d"] += 1
            if st["hold_d"] >= MAX_HOLD_D:
                pend = {"act": "SELL", "reason": "超时", "target_rate": 0.0}
        elif st["stage"] == 1:
            st["brk_days"] += 1
            if st["brk_days"] > PB_DAYS:
                st["stage"] = 0
        st["pd_high"] = bars[j]["high"]
        st["pd_atr"] = ind["atr"][j]
        st["pd_rsi"] = ind["rsi"][j]
        # 大盘regime: 指数收盘>MA20(本日收盘判定, 次日起盘中生效)
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
                    st["stage"] = 0
        return rec