# -*- coding: utf-8 -*-
"""lab/tools/ - 一次性验证/扫描脚本存放处(垂直隔离, 不属于 lab 策略注册表)

  pair_block_test.py          配对T拦截阈值 合成行情单测
  pair_block_sweep.py         配对T拦截阈值 扫描
  intraday_gap_test.py        T 止损跳空豁免 A/B 回放验证
  intraday_macd_anom_test.py  1分钟 MACD/异动票 A/B 回放验证

用法: python -u lab/tools/<脚本名>.py (脚本自带 sys.path 推导, 可直接运行)
"""
