# -*- coding: utf-8 -*-
"""策略实验室入口(垂直隔离模块, 不影响生产系统)
  python run_lab.py --list                 列出全部已注册策略
  python run_lab.py -s gap_exit            prod(生产基线) vs gap_exit 逐标的对比
  python run_lab.py -s gap_exit --html     追加生成 report/dashboard_lab.html
新策略开发: 在 lab/strategies/ 新建文件继承 LabStrategy(或 ProdStrategy),
  登记进 lab/strategies/__init__.py 的 REGISTRY 即可参与对比;
  人工确认有效后按晋升流程并入 core/adaptive.py(见 lab/__init__.py)"""
import sys

from lab.runner import main

if __name__ == "__main__":
    sys.exit(main())
