# -*- coding: utf-8 -*-
"""公共决策核(core): 指标实现 + v2.2 双模信号

被 T 子系统(ttrade/、lab 工具)、量化子系统(quant/)、公共回测(backtest/)四方共用:
  - indicators.py: MA/布林/RSI/ATR 与 build_ind 统一指标实现(回测↔实盘零漂移)
  - adaptive.py:   v2.2 双模策略(趋势腿 + scalp腿 + 冰点左侧)的实盘/EOD扫描信号
本包不含任何子系统专属状态与账户逻辑, 只做纯计算与信号生成。
"""
