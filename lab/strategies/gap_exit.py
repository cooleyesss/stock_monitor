# -*- coding: utf-8 -*-
"""实验策略: 高开撤卖(gap exit)
动机: 趋势破位后若次日大幅高开, 常伴随隔夜情绪修复/利好, 机械按开盘价卖出
      可能卖在反弹起点; 撤销卖出、继续持有待收盘重新判定, 可规避该损耗
风险: 高开低走情形会放大亏损(撤销后继续承担趋势下行敞口一夜以上)
规则: 仅作用于 act=SELL 且 reason=趋势破位 的挂单, 次日开盘价 >= 前收*(1+GAP)
      则撤销本笔卖出( scalp 止盈/止损/超时 等其他出场不受影响, 纪律保留)
参数: GAP=0.03(高开阈值, 全局统一不逐股调参)"""
from lab.strategies.prod import ProdStrategy


class GapExitStrategy(ProdStrategy):
    name = "gap_exit"
    desc = "高开撤卖：趋势破位卖出遇次日高开≥3%则撤销，持有待重新判定"
    GAP = 0.03

    def confirm_fill(self, st, pending, o, prev_close):
        if pending.get("act") == "SELL" and pending.get("reason") == "趋势破位" \
                and prev_close and o >= prev_close * (1 + self.GAP):
            return False
        return True
