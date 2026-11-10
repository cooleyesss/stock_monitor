# -*- coding: utf-8 -*-
"""生产基线策略: core.adaptive 原样包装, 不做任何改动
与 run_ps_stock 的等价性已由 _bt_tmp/verify_quant_replay.py 全量验证
(6只个股收益/操作序列/权益曲线完全一致)"""
from core import adaptive

from lab.base import LabStrategy


class ProdStrategy(LabStrategy):
    name = "prod"
    desc = "生产基线 v2.2（趋势腿破MA10离场 + 超跌腿 + 冰点左侧 + 保险丝）"

    def decide(self, st, ind, bars, j, o, mkt_cl):
        return adaptive.decide(st, ind, bars, j, o=o, mkt_cl=mkt_cl)
