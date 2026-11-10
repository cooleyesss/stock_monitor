# -*- coding: utf-8 -*-
"""lab: 日内T「止损跳空豁免+收盘确认」A/B 验证 (垂直隔离, 只读生产代码, 不改生产默认)

问题: 2026-10-20 09:30:32 航天电子开盘12.88已深穿止损线13.20(2.4%) -> 直接清仓5500股,
      当日尾盘收复并收红 => 卖在最低点。
候选: ST_GAP_EXEMPT_EN=True -> 开盘跳空深穿不立即清仓, 挂起待 ST_GAP_CONFIRM 收盘确认,
      期间收复止损线自动解除(假破位)。

口径: 复用 ttrade/replay._replay(与实盘零漂移的逐bar回放, 全套费用/T+1/限频/冷却/配对T)
数据: 腾讯1分钟K线(东财trends2故障时回退), 实际窗口≈2个交易日 -> 结论仅作方向性参考
用法: python -u lab/tools/intraday_gap_test.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config_ttrade as config
from common import quotes
import state as tstate
import account, replay, vote

LMT = 1200
# 2026-10-20 开盘前基准(实验起点口径; 个性化数据, 在 config_local.py 的 LAB_BASE_* 配置)
BASE_CASH = getattr(config, "LAB_BASE_CASH", 0.0)
BASE_POS = dict(getattr(config, "LAB_BASE_POS", {}) or {})
BASE_COST = dict(getattr(config, "LAB_BASE_COST", {}) or {})

MODES = [
    ("OFF 原行为(开盘破线立即清仓)", dict(ST_GAP_EXEMPT_EN=False)),
    ("ON  跳空豁免+收盘确认", dict(ST_GAP_EXEMPT_EN=True, ST_GAP_PCT=1.0,
                                   ST_GAP_CONFIRM="14:50")),
]


def _load_bars(day=None):
    """拉 t_plan 各股 + 指数的1分钟bars; day!=None 时只保留该交易日
    返回(tplan, bars_map, index_bars, fails)"""
    tplan = tstate.load_t_plan()
    bars_map, fails = {}, []
    for c in list(tplan):
        try:
            b = quotes.fetch_kline_bars(c, klt=1, lmt=LMT)
        except Exception as e:
            b = None
            fails.append((c, str(e)[:60]))
        if b:
            bars_map[c] = [x for x in b if x["dt"][:10] == day] if day else b
            if not bars_map[c]:
                bars_map.pop(c)
    try:
        index_bars = quotes.fetch_kline_bars(config.REGIME_INDEX_ALIAS, klt=1, lmt=LMT)
    except Exception as e:
        index_bars = None
        fails.append((config.REGIME_INDEX_ALIAS, str(e)[:60]))
    if index_bars and day:
        index_bars = [x for x in index_bars if x["dt"][:10] == day]
    return tplan, bars_map, index_bars, fails


def _dump_day(tplan, bars_map):
    """打印当日各股 开/高/低/收 与止损线, 便于人工核对跳空/收盘确认结果"""
    print("── 当日行情概览(分钟bar聚合) ──")
    for c, bs in bars_map.items():
        plan = tplan[c]
        stops = plan.get("stop_loss") or []
        line = max(stops) if stops else None
        print(f"  {plan.get('name', c)}({c}): 开{bs[0]['open']:.3f} "
              f"高{max(b['high'] for b in bs):.3f} 低{min(b['low'] for b in bs):.3f} "
              f"收{bs[-1]['close']:.3f} 止损线"
              f"{('%0.3f' % line) if line else '-'} | 14:50价"
              f"{_px_at(bs, '14:50'):.3f}")
    print()


def _px_at(bars, hhmm):
    """取当日某时刻(含)前最后一根bar的收盘价; 无则返回0"""
    sel = [b for b in bars if b["dt"][11:] <= hhmm]
    return sel[-1]["close"] if sel else 0.0


def _stop_events(res, code):
    """从 sig_rows 抽出该股止损相关事件"""
    out = []
    for r in res["sig_rows"]:
        if r[2] == code and r[5] in ("挂起待确认", "豁免解除") or \
                (r[2] == code and r[6] in ("stop_gap",)):
            out.append(r)
    return out


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else None
    tplan, bars_map, index_bars, fails = _load_bars(day)
    if not bars_map or not index_bars:
        print("数据不足, 中止。失败:", fails)
        return
    all_days = sorted({b["dt"][:10] for b in index_bars}
                      | {d for bs in bars_map.values() for d in {b["dt"][:10] for b in bs}})
    n_bars = sum(len(vote.bars_of_day(bars_map[c], d)) for c in bars_map for d in all_days)
    shares_map = {c: (BASE_POS.get(c, 0), BASE_COST.get(c, 0.0)) for c in bars_map}
    print(f"窗口: {all_days[0]} ~ {all_days[-1]} ({len(all_days)}个交易日, "
          f"{len(bars_map)}只/{n_bars}根1分钟bar)")
    print(f"基准: 现金{account.fmt_amt(BASE_CASH)} | 底仓 " +
          ", ".join(f"{tplan[c].get('name', c)}{s}股" for c, (s, _) in shares_map.items() if s))
    if fails:
        print("拉取失败:", fails)
    if day:
        _dump_day(tplan, bars_map)
    print()

    results = {}
    for label, attrs in MODES:
        for k, v in attrs.items():
            setattr(config, k, v)
        res = replay._replay(bars_map, tplan, index_bars, all_days, BASE_CASH,
                             shares_map, tech=True)
        results[label] = res

    # ---- 汇总表 ----
    print("══════════ A/B 汇总 ══════════")
    print(f"{'模式':<30}{'买':>4}{'卖':>4}{'止损':>5}{'期末现金':>13}"
          f"{'已回补':>11}{'浮动':>11}{'止损盈亏':>11}")
    for label in results:
        r = results[label]
        print(f"{label:<30}{r['n_buy']:>4}{r['n_sell']:>4}{r['n_stop']:>5}"
              f"{r['cash']:>13,.2f}{r['realized']:>11,.2f}"
              f"{r['floating']:>11,.2f}{r['stop_pnl']:>11,.2f}")

    # ---- 逐股对比 ----
    print("\n══════════ 逐股已回补/浮动 ══════════")
    for code in bars_map:
        name = tplan[code].get("name", code)
        line = f"  {name}({code}): "
        for label in results:
            d = results[label]["by_code"].get(code) or {}
            line += (f"[{label.split()[0]}] 回补{d.get('realized', 0):+,.0f}/"
                     f"浮动{d.get('floating', 0):+,.0f}  ")
        print(line)

    # ---- 600879 止损路径对比(本次问题的标的) ----
    print("\n══════════ 航天电子(600879) 止损路径 ══════════")
    for label in results:
        r = results[label]
        evs = [x for x in r["sig_rows"] if x[2] == "600879" and x[6].startswith("stop")]
        if not evs:
            print(f"[{label}] 无止损事件")
        for e in evs:
            print(f"[{label}] {e[1]} {e[5]} 价{e[11]} 股{e[12]} {e[13]}")

    print("\n口径提示: 窗口仅≈2个交易日(腾讯1分钟K线上限320根), 结论为方向性参考; "
          "AI守门未模拟, 实盘低吸成交可能多于回放。")


if __name__ == "__main__":
    main()
