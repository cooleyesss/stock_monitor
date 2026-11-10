# -*- coding: utf-8 -*-
"""策略回测独立入口(薄转发)
单独启动回测迭代策略, 不影响监控进程:
    python run_backtest.py                  半年回测+参数优化+生成dashboard
    python run_backtest.py --no-dashboard   只跑回测不生成HTML
    python run_backtest.py --quant          当前量化账户标的池 · v2.3回测+报告
实际引擎: backtest/daily.py(标的池) / backtest/quant_bt.py(量化账户)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if "--quant" in sys.argv:
    from backtest.quant_bt import main
else:
    from backtest.daily import main

if __name__ == "__main__":
    main()
