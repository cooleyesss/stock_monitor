# -*- coding: utf-8 -*-
"""量化子系统(量化独立账户): 以持仓快照为期初, v2.2双模策略日频自动交易 + 复盘日报 + 选股池

与 T 子系统(ttrade/, sim_state.json)完全独立, 数据文件在 data/quant/:
  quant_state.json / quant_trades.csv / quant_trades.log / watchlist.csv / quant_run.log
结构:
  account.py   子账户状态/流水/日志读写 + 从 data/quant/holdings.csv 初始化
  engine.py    日频编排(逐股 decide -> 次日开盘成交 -> 归集/选股调仓) + 复盘日报推送
  picker.py    智能选股(热门板块动量 -> 候选标的; 由 engine 在调仓日调用)
  dashboard.py 生成 report/dashboard_quant.html(纯实盘量化看板)
依赖边界: config_quant(配置) + common(共享基础层) + core(公共决策核) + backtest.daily(取 BT_CAPITAL/POOL)
"""
