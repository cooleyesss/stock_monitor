# -*- coding: utf-8 -*-
"""v5个股情况估算层(2026-11-19 用户指令, 信息层·不参与任何自动决策)

继承v4管线零改动, 仅在扫描与日报侧补充三块信息:
①关键价位: 核心支撑=MA20(回踩确认锚) / 强支撑=近PICK_V5_SUP_N日最低;
  压力=近PICK_V5_RES_N1日最高, 强压力=近PICK_V5_RES_N2日最高(与压力同价不重复列)
②当日情况: 位置档位(回踩确认/中位/近新高) + 当日涨跌幅 + 量能状态(vr)
③入仓时机: 按位置档位与价位生成具体操作建议(支撑企稳低吸/突破压力跟随等)
引擎仍按v4信号执行买卖; 时机建议仅供人工参考, 实盘采纳需先lab回测验证(项目铁律)。
"""
import config_quant as config


def _f(x):
    """价位两位小数(缺失显示-)"""
    return f"{x:.2f}" if x is not None else "-"


def snapshot(bars):
    """v5价位快照(与picker._v3_factors同源bars, 末端为已收盘bar; 无未来函数):
    res1压力(近N1日高) / res2强压力(近N2日高, 同价不列) / sup1核心支撑(MA20)
    / sup2强支撑(近N日低) / pct_chg当日涨跌"""
    n1, n2, ns = config.PICK_V5_RES_N1, config.PICK_V5_RES_N2, config.PICK_V5_SUP_N
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    res1 = max(highs[-n1:]) if len(highs) >= n1 else None
    res2 = max(highs[-n2:]) if len(highs) >= n2 else None
    sup2 = min(lows[-ns:]) if len(lows) >= ns else None
    sup1 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    pct_chg = (closes[-1] / closes[-2] - 1) if len(closes) >= 2 else None
    return {"res1": res1, "res2": res2 if res2 and res1 and res2 > res1 else None,
            "sup1": sup1, "sup2": sup2, "pct_chg": pct_chg}


def pos_state(e):
    """当日情况·位置档位(仿图片位置标签, 阈值同v4标签): 回踩确认/中位/近新高"""
    b = e.get("bias")
    if b is None:
        return None
    if b <= config.PICK_V5_PULL_BIAS:
        return "回踩确认"
    return "中位" if b <= 0.10 else "近新高"


def vol_state(e):
    """当日情况·量能状态: vr>=1.1放量 / <0.85缩量 / 其余量稳"""
    vr = e.get("vr")
    if vr is None:
        return None
    if vr >= 1.1:
        return "放量"
    return "缩量" if vr < 0.85 else "量稳"


def levels_txt(e):
    """关键价位说明行: 支撑(MA20/近低); 压力(近高/强高)"""
    s = f"支撑{_f(e.get('sup1'))}(MA20)"
    if e.get("sup2") is not None:
        s += f" / {_f(e['sup2'])}({config.PICK_V5_SUP_N}日低)"
    s += f"; 压力{_f(e.get('res1'))}({config.PICK_V5_RES_N1}日高)"
    if e.get("res2") is not None:
        s += f" / {_f(e['res2'])}({config.PICK_V5_RES_N2}日高)"
    return s


def entry_plan(e, gate_open=True):
    """入仓具体操作时机(v5信息层): 按位置档位+价位生成操作建议, 不参与自动决策
    - 回踩确认: 贴近MA20, 支撑上方企稳分批低吸, 跌破强支撑止损观望
    - 中位: 回调至MA20低吸, 或放量突破压力轻仓跟随
    - 近新高: 勿追, 突破压力回踩不破再入, 或深回调至MA20"""
    st = pos_state(e)
    if not st or e.get("sup1") is None:
        return None
    sup1 = _f(e.get("sup1"))
    if e.get("extreme"):
        plan = "⚠连板极端沉底: 不给时机建议, 仅观察"
    elif st == "回踩确认":
        stop = e.get("sup2")
        plan = (f"回踩确认: 现价贴近MA20回踩位, 时机=支撑{sup1}上方企稳"
                "(收阳/缩量止跌)分批低吸; 跌破"
                + (f"{_f(stop)}({config.PICK_V5_SUP_N}日低)或MA20失守即止损观望"
                   if stop is not None else "MA20失守即止损观望"))
    elif st == "中位":
        plan = (f"中位: 时机=回调至支撑{sup1}(MA20)附近企稳低吸; "
                f"或放量突破压力{_f(e.get('res1'))}轻仓跟随; 跌破{sup1}观望")
    else:
        plan = (f"近新高: 现价处高位区勿追; 时机=放量突破压力{_f(e.get('res1'))}"
                f"后回踩不破再入, 或深回调至支撑{sup1}(MA20)附近")
    head = "" if gate_open else "[大盘门控关闭, 以下为门控开放后预案] "
    return head + plan + f" | {levels_txt(e)}"
