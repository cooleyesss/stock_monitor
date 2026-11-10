# -*- coding: utf-8 -*-
"""文本/数值解析公共工具(公共系统): 日期串与金额解析, T 与量化两侧共用"""

import datetime


def today_str(now=None):
    return (now or datetime.datetime.now()).strftime("%Y-%m-%d")


def _parse_money(s):
    """解析 '147.93' / '约18200' 之类的金额"""
    s = (s or "").strip().replace("约", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _f(v):
    v = (v or "").strip()
    try:
        return float(v)
    except ValueError:
        return None
