# -*- coding: utf-8 -*-
"""T 子系统做T台账盈亏计算(纯函数, 无状态依赖)

从原 common/data.py 的局部导入拆出为独立模块, 供 ttrade/state.py 与 ttrade/account.py 共用,
彻底消除 "state ↔ account" 的循环导入。account.py 顶层重导出这两个函数(account.t_pnl 兼容)。
"""


def t_pnl(led, price):
    """做T盈亏 = (卖出额-买入额) + (买入股数-卖出股数)*现价 (未扣费)"""
    return (led["sa"] - led["ba"]) + (led["bq"] - led["sq"]) * price


def t_pnl_detail(led, price):
    """做T盈亏拆分: (已回补T盈亏, 未回补浮动)
    已回补 = min(买,卖)股数 * (卖出均价-买入均价), 即日内完成配对的T
    未回补 = 净未配对股数按现价的浮动盈亏(多买看现价, 多卖看回补差价)"""
    total = t_pnl(led, price)
    bq, sq, ba, sa = led["bq"], led["sq"], led["ba"], led["sa"]
    matched = min(bq, sq)
    realized = 0.0
    if matched > 0:
        avg_b = ba / bq if bq else 0.0
        avg_s = sa / sq if sq else 0.0
        realized = matched * (avg_s - avg_b)
    return realized, total - realized
