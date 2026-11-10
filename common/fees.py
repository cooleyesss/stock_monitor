# -*- coding: utf-8 -*-
"""交易费用模型: 佣金/印花税/过户费, ETF豁免判断
供做T模拟盘(ttrade.account)与回测(backtest.*)共用, 保证费用口径唯一"""
import config_common as config


def is_etf(code):
    """ETF/LOF: 沪市51/56/58开头, 深市15开头; 免印花税和过户费
    支持'ZZ1000'这类别名键: 先经ETF_CODE_MAP映射到真实代码再判断"""
    c = config.ETF_CODE_MAP.get(code, code)
    return c[:2] in ("15", "51", "56", "58")


def buy_fee(amount, code=""):
    fee = max(config.FEE_COMMISSION_MIN, amount * config.FEE_COMMISSION)
    if not is_etf(code):
        fee += amount * config.FEE_TRANSFER
    return round(fee, 2)


def sell_fee(amount, code=""):
    fee = max(config.FEE_COMMISSION_MIN, amount * config.FEE_COMMISSION)
    if not is_etf(code):
        fee += amount * config.FEE_TRANSFER + amount * config.FEE_STAMP_SELL
    return round(fee, 2)
