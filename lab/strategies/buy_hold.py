# -*- coding: utf-8 -*-
"""买入持有对照(分钟级框架基准): 首日首根5m bar发出满仓信号后不再操作
用途: ①验证连续仓位分批建仓框架(3笔推进至target_rate=1.0)与费用口径
      ②作为分钟级实验的买入持有对照组(等价等权B&H基准)"""
from lab.minute import MinuteStrategy


class BuyHoldStrategy(MinuteStrategy):
    name = "buy_hold"
    desc = "买入持有: 首日满仓(target_rate=1.0, 分3笔推进), 之后持有不动"

    def decide(self, st, bars, j, o, mkt_bars):
        # 仅完全空仓且未设目标时发一次满仓信号; 后续由回放器"分批推进"自动完成
        if st["target_rate"] < 1.0 and st["sh"] == 0:
            return {"act": "BUY", "reason": "买入持有建仓", "target_rate": 1.0}
        return None
