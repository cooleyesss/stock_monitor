# -*- coding: utf-8 -*-
"""T 子系统入口 - 监控 + 做T模拟盘(薄转发)
实际主循环在 ttrade/runner.py; 本入口保持 start.bat/start.ps1 及
历史用法(python ttrade/main.py [--test/--test-eod/--reset/--manual-sell/--backtest])不变
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ttrade.runner import main

if __name__ == "__main__":
    main()
