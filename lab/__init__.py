# -*- coding: utf-8 -*-
"""策略实验室(垂直隔离模块)
用途: 新量化策略在此开发/回测/对比, 人工判断可采纳后才并入生产策略, 避免策略混淆
隔离保证:
  ① 生产入口(quant_daily.py / monitor / engine)不引用本包, 生产行为零影响
  ② 实验策略只读复用 core.adaptive 接口, 不修改生产文件
  ③ 对比基线 prod 与实验策略走同一回放器(lab.base.replay_stock), 口径公平
晋升流程(人工确认实验有效后):
  1. 规则并入 core/adaptive.py(config 开关, 全局参数不逐股调参)
  2. backtest/daily.py run_ps_stock 同步并入(保持逐语句等价)
  3. python run_backtest.py 验证基线(对比 BT_CAPITAL 口径的历史基线)
  4. replay 一致性验证(_bt_tmp/verify_quant_replay.py)
  5. 落地生产, 本实验从 REGISTRY 移除或留档
"""
