# -*- coding: utf-8 -*-
"""连续仓位双模(实验): v2.2信号语义逐语句复刻 strategies/adaptive.decide,
唯一差异=仓位管理: 二元腿(满仓/半仓/清仓) -> 连续变量 target_rate 分批推进
  BUY_TREND: 一次性全仓 -> target_rate=1.0(分3批建仓)
  BUY_SCALP: 一次性半仓 -> target_rate=0.5(分批)
  SELL:      一次性清仓 -> target_rate=0.0(分批退出, 末批尾仓全出)
验证目标: 隔离"仓位分批"单一变量, 与prod基线对比评估平滑建/退对收益与回撤的影响
信号判定数据源与prod严格同源(窗口内直接用bt_daily_cache日K), 排除数据差异干扰"""
import config_quant as config
from lab.minute import MinuteStrategy, apply_fill_minute


class TrendContStrategy(MinuteStrategy):
    name = "trend_cont"
    desc = ("v2.2信号连续仓位版: 趋势target=1.0/scalp=0.5/破位=0, "
            "分3批推进(隔离仓位分批单一变量)")

    def init_state(self, cash):
        st = super().init_state(cash)
        st["sh_tr"] = True      # 腿型: True趋势/False超跌(与 adaptive.sh_tr 一致)
        st["hold_d"] = 0        # 持仓天数(每日收盘决策时推进, 与 adaptive 一致)
        return st

    def decide(self, st, bars, j, o, mkt_bars):
        return None             # 分钟级钩子不用: 本策略只在每日收盘决策(decide_daily)

    def decide_daily(self, st, ind, bars, j, o, mkt_cl):
        """逐语句复刻 adaptive.decide, 仅把 BUY/SELL 的仓位表达改为 target_rate"""
        closes = ind["close"]
        ma20, ma5, ma10, ma60 = (ind["ma20"][j], ind["ma5"][j],
                                 ind["ma10"][j], ind["ma60"][j])
        lo, mid, rsi, atr = ind["lo"][j], ind["mid"][j], ind["rsi"][j], ind["atr"][j]
        cl, pc = closes[j], bars[j]["close"]
        if None in (ma20, ma5, ma10, lo, mid, rsi, atr) or atr <= 0 or j < 10:
            return None                      # 指标未就绪(hold_d也不推进)
        up_reg = cl > ma20 and ma5 > ma10
        ma20_up = ind["ma20"][j - 5] is not None and ma20 > ind["ma20"][j - 5]
        mom10 = cl / closes[j - 10] - 1
        up_ok = up_reg and ma60 is not None and cl > ma60 and ma20_up \
            and mom10 > 0.02
        if st["sh"] > 0:
            st["hold_d"] += 1
            if st["sh_tr"]:
                if cl < ma10:
                    return {"act": "SELL", "reason": "趋势破位", "target_rate": 0.0}
            else:
                if pc >= mid:
                    return {"act": "SELL", "reason": "止盈", "target_rate": 0.0}
                if pc <= st["sig_low"]:
                    st["cool"] = 3
                    return {"act": "SELL", "reason": "止损", "target_rate": 0.0}
                if st["hold_d"] >= 8:
                    return {"act": "SELL", "reason": "超时", "target_rate": 0.0}
            return None
        if up_ok:
            return {"act": "BUY_TREND", "reason": "趋势站上", "target_rate": 1.0}
        if not up_reg and j >= 20:
            if st["cool"] > 0:
                st["cool"] -= 1
                return None
            # 突发保险丝: 恐慌日(暴跌/放量长阴)次日不接飞刀
            yret = pc / bars[j - 1]["close"] - 1
            v5a = sum(ind["vol"][j - 5:j]) / 5
            panic = yret <= -0.07 or (v5a > 0 and ind["vol"][j] > 3 * v5a
                                      and yret <= -0.04)
            deep, why, sig = False, "", 0.0
            if not panic:
                deep = cl > lo and bars[j]["low"] < lo and rsi < 40
                if not (pc > bars[j]["open"]):
                    deep = False
                if (cl / closes[j - 5] - 1) < -0.12:
                    deep = False
                if deep and o is not None and (o - bars[j]["low"]) < 0.3 * atr:
                    deep = False             # 开盘距信号低点不足0.3ATR, 放弃
                if deep:
                    why, sig = "超跌", bars[j]["low"]
                # v2.2规则A: 市场连续冰点+个股超卖未崩 -> 左侧半仓(受保险丝约束)
                if not deep and config.ICE_LEFT_EN and mkt_cl:
                    k0 = j - config.ICE_MKT_DAYS
                    mkt_ice = False
                    if k0 >= 0:
                        dn = sum(1 for k in range(k0 + 1, j + 1)
                                 if mkt_cl[k] < mkt_cl[k - 1])
                        cum = mkt_cl[j] / mkt_cl[k0] - 1
                        mkt_ice = dn >= config.ICE_MKT_DAYS \
                            or cum <= config.ICE_MKT_DROP
                    stk_ok = (rsi < config.ICE_RSI
                              and (cl / closes[j - 5] - 1) >= config.ICE_STK_DROP5
                              and pc < bars[j - 1]["close"])
                    if mkt_ice and stk_ok \
                            and (o is None or (o - bars[j]["low"]) >= 0.3 * atr):
                        deep, why, sig = True, "冰点左侧", bars[j]["low"]
            if deep:
                return {"act": "BUY_SCALP", "reason": why, "sig_low": sig,
                        "need": round(sig + 0.3 * atr, 4), "target_rate": 0.5}
        return None

    def apply_fill(self, st, code, name, date, o, pending, check_open):
        """先按连续仓位框架成交, 再补 v2.2 腿型字段(腿型首批成交即设定, 幂等)"""
        a = pending.get("act")
        # scalp开盘空间确认(与 adaptive.apply_fill check_open 语义一致)
        if a == "BUY_SCALP" and check_open and pending.get("need") is not None \
                and o < pending["need"]:
            return None
        mapped = dict(pending)
        mapped["act"] = "BUY" if a in ("BUY_TREND", "BUY_SCALP") else a
        rec = apply_fill_minute(st, code, name, date, o, mapped, check_open)
        if rec:
            if a == "BUY_TREND":
                st["sh_tr"] = True
            elif a == "BUY_SCALP":
                st["sh_tr"] = False
                st["sig_low"] = pending.get("sig_low", 0.0)
            if st["sh"] == 0:
                st["hold_d"] = 0
            rec["leg"] = "trend" if st["sh_tr"] else "scalp"
        return rec
