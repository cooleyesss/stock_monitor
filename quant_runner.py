# -*- coding: utf-8 -*-
"""量化子系统盘中双窗口runner入口(薄转发)
实际主循环在 quant/runner.py; 本入口保持 start.ps1/stop.ps1 及
手动用法(python quant_runner.py [--dry --now "HH:MM"])不变
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quant.runner import main

if __name__ == "__main__":
    main()
