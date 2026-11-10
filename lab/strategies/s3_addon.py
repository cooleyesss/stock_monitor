# -*- coding: utf-8 -*-
"""s3_addon(实验): v2.2全信号(趋势/超跌/冰点, 连续仓位版) + S3缩量回踩第三入场模式
设计: 隔离"S3入场"单一变量 — 基线=trend_cont(纯v2.2信号), 本策略=trend_cont + 盘中S3入场
  S3入场仅在三条件全满足时触发: ①空仓(sh==0且target_rate==0) ②v2.2日线无pending
    ③v2.2 cool==0(尊重日线止损冷却, 同一账户信号不打架)
  入场表达=scalp腿半仓(target_rate=0.5, sh_tr=False, sig_low=回踩bar低点),
  出场完全复用v2.2 scalp腿退出(中轨止盈/破sig_low止损cool=3/8日超时) — 增量纯净
S3状态机(与s3_vol_pb standalone同参数, 扫描平台中心值):
  突破: 5m收盘>昨日最高 且 bar量>=2.0x前20根均量 -> 待回踩(有效期5交易日, 当日不回踩)
  回踩: bar低点触及突破位+0.3ATR内 且 收阳 且 量<=0.6x突破量 -> 入场"""
from lab.strategies.trend_cont import TrendContStrategy

BRK_VOL_X = 2.0    # 突破放量倍数
PB_DAYS = 5        # 待回踩有效期(交易日)
PB_ZONE = 0.3      # 回踩区间(ATR倍数)
PB_VOL_SHR = 0.6   # 缩量倍数
MIN_J = 20         # 均量就绪所需当日bar数


class S3AddonStrategy(TrendContStrategy):
    name = "s3_addon"
    desc = ("v2.2全信号+S3缩量回踩第三入场: 收>昨高且量>=2x->待回踩5日; "
            "缩量收阳触及突破位+0.3ATR->scalp半仓(复用v2.2 scalp退出); "
            "隔离S3入场单一变量, 基线=trend_cont")

    def init_state(self, cash):
        st = super().init_state(cash)
        st.update({"pd_high": None, "pd_atr": None,
                   "stage": 0, "brk_px": 0.0, "brk_vol": 0.0, "brk_days": 0})
        return st

    def decide(self, st, bars, j, o, mkt_bars):
        # 持仓/建仓中: 全部交decide_daily与分批推进, 本钩子不干预
        if st["sh"] > 0 or st["target_rate"] > 0:
            return None
        if st["pd_high"] is None or st["pd_atr"] is None or j < MIN_J:
            return None
        if st["cool"] > 0:
            return None                            # 尊重v2.2日线冷却
        cur = bars[-1]
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
                    and cur["vol"] <= PB_VOL_SHR * st["brk_vol"]:
                st["stage"] = 0
                return {"act": "BUY_SCALP", "reason": "S3缩量回踩",
                        "sig_low": cur["low"], "target_rate": 0.5}
        return None

    def decide_daily(self, st, ind, bars, j, o, mkt_cl):
        pend = super().decide_daily(st, ind, bars, j, o, mkt_cl)
        # S3状态维护: 突破待回踩计数(仅空仓时; 过期清零)
        if st["sh"] == 0 and st["target_rate"] == 0 and st["stage"] == 1:
            st["brk_days"] += 1
            if st["brk_days"] > PB_DAYS:
                st["stage"] = 0
        st["pd_high"] = bars[j]["high"]
        st["pd_atr"] = ind["atr"][j]
        return pend