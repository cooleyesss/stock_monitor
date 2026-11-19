# -*- coding: utf-8 -*-
"""做T模拟盘(日内T独立子系统, 2026-11-05 收拢为 ttrade/ 垂直包, 与 quant/ 对称)

模块职责(与量化子系统、公共回测完全隔离):
  config_ttrade.py     T 专属配置(继承 config_common, 注意运行期改写必须走模块属性)
  main.py              入口薄转发(start.ps1/stop.ps1/计划任务统一调 python ttrade/main.py)
  vote.py              多策略投票决策核(纯函数 evaluate)
  strategy.py          做T信号判断 + 统一调度 + 止损跳空豁免
  account.py           做T模拟盘账户: 成交/做T台账/估值/收盘报告数据
  ledger.py            做T盈亏纯函数(t_pnl/t_pnl_detail), 消除 state↔account 循环导入
  state.py             T 状态文件 / 做T计划(t_plan.csv) / 成交流水日志 读写
  ai.py                DeepSeek AI节点分析(做T信号守门)
  runner.py            监控+做T一体化主循环(9:26竞价自动更新t_plan / 9:30-15:00逐轮tick / 15:00收盘战报)
  auction.py           9:26 集合竞价自动更新 t_plan (高开/低开/跳空极宽三分支, 不做剧烈重算)
  tplan_gen.py         9:25 四因子重算 t_plan 价位(行情/大盘/竞价/新闻)
  replay.py            ≤5日分时逐bar回放(做T专用回测)
  ttrade_v3.py         v3做T策略(STRAT_V3_EN 开关, 接管 check_signal)
  dashboard.py         生成 report/ttrade/dashboard_ttrade.html(纯日内T看板, 与量化看板互不覆盖)
  bt_ttrade.py         做T回测(读 data/ttrade/ 流水)

依赖边界:
  ① 只依赖 config_common/config_ttrade(配置) + common(行情/通知/费用/文本解析)
     + core.adaptive(EOD次日信号表共享决策核, 见 runner.py 显式标注)
  ② 量化侧(quant/lab/backtest.daily)不引用本包任何代码
  ③ 持仓来源 = data/ttrade/holdings.csv(由母表 mystock_test.csv 一键分发), 不读写母表
  ④ 包内互相引用统一 `from ttrade import xxx`; 入口/直接运行文件头部已加
     项目根 sys.path 引导(python ttrade/main.py 可从任意 cwd 运行)
"""
