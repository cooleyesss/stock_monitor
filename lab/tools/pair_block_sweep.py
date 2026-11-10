# -*- coding: utf-8 -*-
"""配对拦截阈值扫参回测: 同一历史区间 × 1×/2×/3×t_qty 三组阈值
对比维度: 拦截次数 / 做T净收益 / 成交笔数 / 期末现金 / 持仓耗尽风险
2026-10-20 修复: cum_ledger→today_ledger(跨日清零) + 阈值倍数"""
import sys, os, datetime, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config_ttrade as config
from common import quotes
import state as tstate
import replay, strategy, account, vote

def load_data(days=5):
    """一次性拉取分时数据(5日窗口上限), 返回 replay 需要的参数"""
    tplan = tstate.load_t_plan()
    if not tplan:
        print("未找到做T计划")
        sys.exit(1)
    print(f"拉取分时数据({days}日)...")
    index_bars = quotes.fetch_trends_bars(config.REGIME_INDEX_ALIAS, ndays=days)
    bars_map = {}
    for code in tplan:
        try:
            bars_map[code] = quotes.fetch_trends_bars(code, ndays=days)
        except Exception:
            pass
    st = tstate.load_state() or tstate.init_state()
    shares_map = {c: (st["positions"].get(c, {}).get("shares", 0),
                      st["positions"].get(c, {}).get("cost", 0.0))
                  for c in bars_map}
    all_days = sorted({b["dt"][:10] for b in index_bars}
                     | {d for bs in bars_map.values()
                        for d in {b["dt"][:10] for b in bs}})
    print(f"  窗口: {all_days[0]}~{all_days[-1]} ({len(all_days)}交易日 × {len(bars_map)}只)")
    return bars_map, tplan, index_bars, all_days, st["cash"], shares_map


def count_blocks(sig_rows):
    """从 replay 的 sig_rows 里统计配对拦截次数
    sig_rows: [(label, day, time, code, name, side, tag, passed, qty, why), ...]"""
    n = 0
    for r in sig_rows:
        if len(r) >= 10 and "配对:" in (r[9] or ""):
            n += 1
    return n


def run_one(bars_map, tplan, index_bars, all_days, cash0, shares_map, mult):
    """跑单组阈值(mult倍t_qty), 返回关键指标"""
    # monkey-patch _pair_blocked 固定 mult (只影响 replay 调用, 不改实盘)
    _orig = strategy._pair_blocked
    strategy._pair_blocked = lambda st, cd, pl, lt, mm=mult: _orig(st, cd, pl, lt, max_mult=mm)
    try:
        res = replay._replay(bars_map, tplan, index_bars, all_days, cash0,
                             shares_map, tech=True)
    finally:
        strategy._pair_blocked = _orig   # 恢复原函数
    blocks = count_blocks(res["sig_rows"])
    # 持仓耗尽检测: 任何 positions.shares 降到 0 且未止损
    zero_pos = []
    # 没法直接拿到 replay 末 positions(不返回), 改用期末现金+成交数侧面判断
    return {
        "mult": mult,
        "blocks": blocks,
        "n_buy": res["n_buy"],
        "n_sell": res["n_sell"],
        "n_stop": res["n_stop"],
        "realized": res["realized"],
        "floating": res["floating"],
        "stop_pnl": res["stop_pnl"],
        "net": round(res["realized"] + res["floating"] + res["stop_pnl"], 2),
        "cash": round(res["cash"], 2),
        "fees": round(res["fees"], 2),
        "by_code": res["by_code"],
    }


def main():
    # 同时扫 "旧bug"(cum_ledger+1×t_qty) vs "新修复"(today_ledger+1×/2×/3×)
    # 旧bug 需要临时把 net_sell 改回读 cum_ledger
    bars_map, tplan, index_bars, all_days, cash0, shares_map = load_data(days=5)
    if not bars_map:
        print("无数据, 中止")
        return

    results = []

    # ── ① 旧bug基线: cum_ledger + 1×t_qty ──
    _orig_net = strategy.net_sell
    strategy.net_sell = lambda st, cd: int(
        (st.get("cum_ledger", {}).get(cd) or {}).get("sq", 0)
        - (st.get("cum_ledger", {}).get(cd) or {}).get("bq", 0))
    config.PAIR_MAX_MULT = 1
    r_old = run_one(bars_map, tplan, index_bars, all_days, cash0, shares_map, 1)
    r_old["label"] = "旧bug(cum_ledger+1×)"
    results.append(r_old)
    strategy.net_sell = _orig_net   # 恢复

    # ── ② 新修复: today_ledger + 1×/2×/3× ──
    for mult in (1, 2, 3):
        config.PAIR_MAX_MULT = mult
        r = run_one(bars_map, tplan, index_bars, all_days, cash0, shares_map, mult)
        r["label"] = f"新修复(today+{mult}×)"
        results.append(r)

    # ── 输出 ──
    print()
    print("═══ 配对拦截阈值扫参回测 ═══")
    print(f"窗口: {all_days[0]}~{all_days[-1]} ({len(all_days)}交易日, {len(bars_map)}只)")
    print(f"初始现金: {cash0:,.0f} | 底仓: {sum(1 for s,_ in shares_map.values() if s>0)}只")
    print()
    hdr = ["方案", "阈值", "配对拦截", "买笔", "卖笔", "止损笔",
           "已回补", "浮动", "止损", "净收益", "期末现金", "费用"]
    print(" | ".join(hdr))
    print("-" * 120)
    for r in results:
        row = [
            r["label"],
            f"{r['mult']}×t_qty",
            str(r["blocks"]),
            str(r["n_buy"]), str(r["n_sell"]), str(r["n_stop"]),
            f"{r['realized']:+,.0f}", f"{r['floating']:+,.0f}",
            f"{r['stop_pnl']:+,.0f}", f"{r['net']:+,.0f}",
            f"{r['cash']:,.0f}", f"{r['fees']:,.0f}",
        ]
        print(" | ".join(row))

    print()
    print("── 每只股票净收益对比(新修复3组 vs 旧bug) ──")
    codes = sorted({c for r in results for c in r["by_code"]})
    name_of = {c: tplan[c].get("name", c) for c in codes if c in tplan}
    hdr2 = ["代码", "名称"] + [r["label"] for r in results]
    print(" | ".join(hdr2))
    for c in codes:
        row = [c, name_of.get(c, c)]
        for r in results:
            d = r["by_code"].get(c) or {}
            net = round((d.get("realized", 0) + d.get("floating", 0)), 2)
            p = d.get("pair", 0)
            row.append(f"{net:+,.0f}({p})" if p else f"{net:+,.0f}")
        print(" | ".join(row))

    # ── 判定最优 ──
    print()
    print("── 结论 ──")
    best = max(results, key=lambda r: r["net"])
    best_blocks = min(r["blocks"] for r in results if r["net"] >= results[0]["net"] - 100)
    print(f"净收益最高: {best['label']} (净{best['net']:+,.0f}, 拦截{best['blocks']}次)")
    if any(r["blocks"] == 0 and r["net"] > 0 for r in results):
        z = next(r for r in results if r["blocks"] == 0 and r["net"] > 0)
        print(f"零拦截且正收益: {z['label']}")
    # 最终推荐: 选拦截下降最多且净收益不降的组
    old_b = results[0]["blocks"]
    old_net = results[0]["net"]
    print(f"旧bug基线: 拦截{old_b}次, 净{old_net:+,.0f}")
    for r in results[1:]:
        drop = old_b - r["blocks"]
        net_delta = r["net"] - old_net
        print(f"  {r['label']}: 拦截↓{drop}, 净收益{net_delta:+,.0f}")


if __name__ == "__main__":
    main()
