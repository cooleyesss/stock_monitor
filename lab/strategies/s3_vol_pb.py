# -*- coding: utf-8 -*-
"""S3 放量突破+缩量回踩(候选·短线): 不追突破, 等缩量回踩突破位企稳再入
全局规则(不逐股调参):
  突破检测(不追买): bar收盘 > 昨日最高 且 bar量 >= 2.0x 前20根5m均量
    -> 记录突破位brk_px/突破量brk_vol, 进入待回踩状态(有效期5个交易日)
  回踩买入: 突破后1~5日内, bar低点触及 突破位+0.3ATR 区间内 且 收阳
    且 bar量 <= 0.6x 突破量 -> 半仓, sig_low=回踩bar低点
  退出: ①止损 收盘 < sig_low ②止盈 收盘 >= 突破位 + 2*ATR
        ③超时 持仓满5个交易日收盘清
  约束: T+1(买入当日不卖); 卖出当日清突破状态并冷却不再买
突破当日不回踩(当日追高风险大), 回踩判定从次日起"""
from lab.minute import MinuteStrategy, apply_fill_minute

BRK_VOL_X = 2.0    # 突破放量倍数
PB_DAYS = 5        # 待回踩有效期(交易日)
PB_ZONE = 0.3      # 回踩区间(ATR倍数, 相对突破位)
PB_VOL_SHR = 0.6   # 缩量倍数(对突破量)
POS = 0.5          # 目标仓位率
TAKE_ATR = 2.0     # 止盈(ATR倍数, 相对突破位)
MAX_HOLD_D = 5     # 超时(交易日)
MIN_J = 20         # 均量就绪所需当日bar数


class S3VolPbStrategy(MinuteStrategy):
    name = "s3_vol_pb"
    desc = (f"S3 放量突破+缩量回踩: 收>昨高且量>={BRK_VOL_X}x->待回踩; "
            f"{PB_DAYS}日内缩量(<={PB_VOL_SHR}x)收阳触及突破位+{PB_ZONE}ATR->半仓; "
            f"破低止损/{TAKE_ATR}ATR止盈/{MAX_HOLD_D}日超时; T+1")

    def init_state(self, cash):
        st = super().init_state(cash)
        st.update({"pd_high": None, "pd_atr": None,
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
        if day == st["cool_date"]:
            return None
        if st["stage"] == 0:
            # 突破检测(不追买)
            v20 = sum(b["vol"] for b in bars[-21:-1]) / 20
            if v20 <= 0:
                return None
            if cur["close"] > st["pd_high"] and cur["vol"] >= BRK_VOL_X * v20:
                st["stage"] = 1
                st["brk_px"] = cur["close"]
                st["brk_vol"] = cur["vol"]
                st["brk_days"] = 0
            return None
        # stage==1: 待回踩(突破当日不判定, brk_days>=1即次日起来自decide_daily推进)
        if st["brk_days"] >= 1:
            zone = PB_ZONE * st["pd_atr"]
            if cur["low"] <= st["brk_px"] + zone \
                    and cur["close"] >= st["brk_px"] - zone \
                    and cur["close"] > cur["open"] \
                    and cur["vol"] <= PB_VOL_SHR * st["brk_vol"]:
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
                st["stage"] = 0                   # 突破待回踩过期
        st["pd_high"] = bars[j]["high"]
        st["pd_atr"] = ind["atr"][j]
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