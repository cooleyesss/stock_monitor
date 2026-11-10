# -*- coding: utf-8 -*-
"""公共系统 - 共享基础层(两个子系统与回测/lab 都可用, 不含任何子系统专属逻辑)

  notify.py     PushPlus 推送 + HTML 着色/表格
  quotes.py     东财行情(实时/K线/板块) + 腾讯K线回退 + 指标缓存
  fees.py       交易费用模型(佣金/过户费/印花税, ETF 免印花)
  textparse.py  日期串与金额解析通用工具(today_str/_parse_money/_f)

配置见 config_common.py; T 专属配置见 config_ttrade.py; 量化专属配置见 config_quant.py。
"""
