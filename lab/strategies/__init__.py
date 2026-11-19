# -*- coding: utf-8 -*-
"""实验策略注册表: 新策略在此登记后才可被 run_lab 调用
key 为命令行使用的策略名; prod 为固定基线(每次对比自动携带)"""
from lab.strategies.buy_hold import BuyHoldStrategy
from lab.strategies.gap_exit import GapExitStrategy
from lab.strategies.prod import ProdStrategy
from lab.strategies.trend_atr import TrendAtrStrategy
from lab.strategies.s1_atr_break import S1AtrBreakStrategy
from lab.strategies.s2_vwap_rev import S2VwapRevStrategy
from lab.strategies.s3_vol_pb import S3VolPbStrategy
from lab.strategies.s3_addon import S3AddonStrategy
from lab.strategies.s2_vwap_rev2 import S2VwapRev2Strategy
from lab.strategies.s3_vol_pb2 import S3VolPb2Strategy
from lab.strategies.trend_cont import TrendContStrategy
from lab.strategies.trail_exit import Trail5, Trail7, Trail10
from lab.strategies.part_sell import PartSellStrategy
from lab.strategies.scale_in import ScaleInStrategy
from lab.strategies.break_even import BE5, BE8
from lab.strategies.combo_v3 import ComboStrategy, Scale50, Scale70
from lab.strategies.ml_svm import MlSvmStrategy

REGISTRY = {
    "prod": ProdStrategy,
    "gap_exit": GapExitStrategy,
    "buy_hold": BuyHoldStrategy,
    "trend_cont": TrendContStrategy,
    "trend_atr": TrendAtrStrategy,
    "s1_atr_break": S1AtrBreakStrategy,
    "s2_vwap_rev": S2VwapRevStrategy,
    "s3_vol_pb": S3VolPbStrategy,
    "s3_addon": S3AddonStrategy,
    "s2_vwap_rev2": S2VwapRev2Strategy,
    "s3_vol_pb2": S3VolPb2Strategy,
    "trail5": Trail5,
    "trail7": Trail7,
    "trail10": Trail10,
    "part_sell": PartSellStrategy,
    "scale_in": ScaleInStrategy,
    "be5": BE5,
    "be8": BE8,
    "combo": ComboStrategy,
    "scale50": Scale50,
    "scale70": Scale70,
    "ml_svm": MlSvmStrategy,
}
