# -*- coding: utf-8 -*-
"""实验回放器: 与生产回测/实盘完全同口径的日频模拟
语义(与 backtest/daily.py run_ps_stock 及 quant/engine._advance 一致):
  每根收盘 decide -> pending -> 次日开盘价成交(confirm_fill 最后闸门) -> 含全套费用
  scalp 入场带 need 确认位, 次日开盘校验(o>=need 才成交, 与实盘 fill 语义一致)
数据: bt_daily_cache.json(与 run_backtest.py 同一份缓存, 窗口口径 BEG~END)
实验策略只需表达"决策差异", 成交/费用/权益计量框架统一, 保证对比公平"""
import json
import os

from core.indicators import build_ind

CACHE = r"D:\trae_projects\_bt_tmp\bt_daily_cache.json"
IDX = "999999"          # 上证指数(bo动量/冰点左侧的市场序列来源, 与生产一致)


class LabStrategy:
    """实验策略基类(接口与 core.adaptive 对齐, 采纳后可低摩擦并入生产)
    子类最少只需覆盖 decide; confirm_fill/apply_fill/init_state 给高级改造预留"""

    name = "base"
    desc = ""

    def init_state(self, cash):
        from core import adaptive
        return adaptive.new_leg_state(cash=cash)

    def decide(self, st, ind, bars, j, o, mkt_cl):
        """第j根收盘决策 -> pending dict 或 None(语义同 adaptive.decide)"""
        raise NotImplementedError

    def confirm_fill(self, st, pending, o, prev_close):
        """次日开盘成交前的最后闸门: 返回 False=撤销本笔(账户状态不变,
        当日收盘照常 decide 重新生成信号)"""
        return True

    def apply_fill(self, st, code, name, date, o, pending, check_open):
        from core import adaptive
        return adaptive.apply_fill(st, code, name, date, o, pending,
                                   check_open=check_open)


def load_universe():
    """读取回测缓存(与 run_backtest.py 同源); 缺失时提示先跑回测生成"""
    if not os.path.exists(CACHE):
        raise RuntimeError(
            f"回测缓存不存在: {CACHE}\n请先运行 python run_backtest.py 生成缓存")
    return json.load(open(CACHE, encoding="utf-8"))["data"]


def replay_stock(uni, code, capital, strategy):
    """单标的回放 -> {code,name,ops,equity,return,mdd,trades,final}
    与 verify_quant_replay.replay 同循环: ①开盘fill昨日pending ②收盘decide->新pending"""
    bars = uni[code]["bars"]
    name = uni[code].get("name", code)
    ind = build_ind(bars)
    ib = (uni.get(IDX) or {}).get("bars") or []
    mkt_cl = [b["close"] for b in ib] if len(ib) == len(bars) else None
    st = strategy.init_state(cash=capital)
    pending, ops, equity = None, [], []
    for i in range(1, len(bars)):
        if pending:
            if strategy.confirm_fill(st, pending, bars[i]["open"],
                                       bars[i - 1]["close"]):
                rec = strategy.apply_fill(st, code, name, bars[i]["dt"],
                                          bars[i]["open"], pending,
                                          check_open=not pending.get("_chk"))
                if rec:
                    ops.append(rec)
            pending = None
        o_nxt = bars[i + 1]["open"] if i + 1 < len(bars) else None
        d = strategy.decide(st, ind, bars, i, o=o_nxt, mkt_cl=mkt_cl)
        if d:
            d["_chk"] = o_nxt is not None
            pending = d
        equity.append(round(st["cash"] + st["sh"] * bars[i]["close"], 2))
    final = equity[-1] if equity else capital
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak else 0)
    return {"code": code, "name": name, "ops": ops, "equity": equity,
            "return": round(final / capital - 1, 4), "mdd": round(mdd, 4),
            "trades": sum(1 for o in ops if o["side"] == "BUY"),
            "final": round(final, 2)}
