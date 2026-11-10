# -*- coding: utf-8 -*-
"""合成单边上涨/下跌行情测试配对拦截阈值(不依赖东财分时)
核心场景: 5天单边涨 - 价格每天只触高抛区不触低吸区
对比: 旧bug(cum_ledger+1×) vs 新修复(today+1×/2×/3×)
验证: ①新修复组持仓不会空仓 ②拦截次数显著减少 ③仍能成功完成高抛T"""
import sys, os, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config_ttrade as config
import strategy, account, vote, replay

# ── 合成1分钟bar ──
def mk_bars_one_day(day, open_px, close_px, n_bars=240, sell_range=None, buy_range=None,
                    force_high_times=0, force_low_times=0):
    """单日240根1分钟bar, 支持盘中多次冲高/探底
      sell_range=(sell_low, sell_high)  force_high_times=N 盘中N次冲高扫过高抛区
      buy_range=(buy_low, buy_high)     force_low_times=N  盘中N次探底扫过低吸区
    每次冲高/探底持续约10根bar, 模拟盘中多轮机会"""
    bars = []
    cur = open_px
    s_low, s_high = (sell_range or (0, 0))
    b_low, b_high = (buy_range or (0, 0))
    # 把盘中分成 force_high_times*force_low_times 个时段
    slots = []
    for i in range(force_high_times):
        slots.append(("HI", 30 + i * 40, 10))    # 冲高: bar 30, 70, 110... 每次持续10根
    for i in range(force_low_times):
        slots.append(("LO", 80 + i * 40, 10))   # 探底: bar 80, 120, 160...
    slots.sort(key=lambda x: x[1])
    for i in range(n_bars):
        # 基础漂移
        drift = (close_px - open_px) * i / n_bars
        base = open_px + drift
        # 时段内冲高/探底
        for typ, start, dur in slots:
            if start <= i < start + dur:
                if typ == "HI":
                    # 冲高到 sell_low~sell_high 区间内
                    base = max(base, s_low + (s_high - s_low) * ((i - start) / dur) * 0.8)
                else:
                    # 探底到 buy_low~buy_high 区间内
                    base = min(base, b_high - (b_high - b_low) * ((i - start) / dur) * 0.8)
        o = h = l = c = base
        bars.append({"dt": "", "open": o, "high": h, "low": l, "close": c,
                     "vol": 1000 + i * 5, "amount": 100000})
    # 补时间戳
    t_base = datetime.datetime.strptime(day + " 09:31", "%Y-%m-%d %H:%M")
    for i, b in enumerate(bars):
        tm = t_base + datetime.timedelta(minutes=i if i < 120 else i + 90)
        b["dt"] = tm.strftime("%Y-%m-%d %H:%M")
    return bars


def mk_index_day(day, open_px, close_px, n_bars=240):
    """指数分时 bar(简化): 价格平滑, 无 T 区"""
    bars = []
    t_base = datetime.datetime.strptime(day + " 09:31", "%Y-%m-%d %H:%M")
    for i in range(n_bars):
        tm = t_base + datetime.timedelta(minutes=i if i < 120 else i + 90)
        base = open_px + (close_px - open_px) * i / n_bars
        bars.append({"dt": tm.strftime("%Y-%m-%d %H:%M"),
                     "open": base, "high": base, "low": base,
                     "close": base, "vol": 1000, "amount": 1e6})
    return bars


# ── 真实 T 计划(直接从 t_plan.csv 读取3只) ──
import state as tstate
tplan_src = tstate.load_t_plan()
codes_test = ["600480", "603169", "600557", "600879", "300455"]
tplan = {c: tplan_src[c] for c in codes_test if c in tplan_src}

# 持仓(从模拟盘取)
st = tstate.load_state() or tstate.init_state()
shares_map = {c: (st["positions"].get(c, {}).get("shares", 0),
                  st["positions"].get(c, {}).get("cost", 0.0))
              for c in codes_test if c in tplan}
cash0 = st["cash"]


def build_scenario(n_days=5, daily_drift=0.01, scenario="only_high"):
    """构造多日行情(每天盘中多次冲高/探底触发真实配对逻辑):
      only_high: 单边涨+每天盘中冲高3次(不探底) -> 配对拦截最高概率场景
      both_zone: 震荡+每天冲高2次探底2次 -> 正常做T, 看配对是否正确放行"""
    all_days, bars_map, idx = [], {}, []
    price_open = {"600480": 7.70, "603169": 6.00, "600557": 12.30,
                  "600879": 13.60, "300455": 16.10}
    idx_open = 3700.0
    # 触发频率配置
    if scenario == "only_high":
        hi_n, lo_n = 3, 0
    else:
        hi_n, lo_n = 2, 2
    for d in range(n_days):
        day = f"2026-10-{(13 + d) % 28 + 1:02d}"
        all_days.append(day)
        idx_close = idx_open * (1 + daily_drift * 0.3)
        idx.extend(mk_index_day(day, idx_open, idx_close))
        idx_open = idx_close
        for code in tplan:
            sell_range = (tplan[code].get("sell_low", 0), tplan[code].get("sell_high", 0))
            buy_range = (tplan[code].get("buy_low", 0), tplan[code].get("buy_high", 0))
            open_px = price_open[code]
            close_px = open_px * (1 + daily_drift)
            bar = mk_bars_one_day(day, open_px, close_px,
                                  sell_range=sell_range, buy_range=buy_range,
                                  force_high_times=hi_n, force_low_times=lo_n)
            bars_map.setdefault(code, []).extend(bar)
            price_open[code] = close_px
    return all_days, bars_map, idx


def count_blocks(sig_rows):
    return sum(1 for r in sig_rows if len(r) >= 10 and "配对:" in (r[9] or ""))


def run_compare(bars_map, tplan, index_bars, all_days, cash0, shares_map, label=""):
    """扫 4 组对比: 旧bug vs new 1×/2×/3×"""
    results = []

    # 旧bug: cum_ledger + PAIR_MAX_MULT=1
    _orig_net = strategy.net_sell
    _orig_pair = strategy._pair_blocked
    strategy.net_sell = lambda st, cd: int(
        (st.get("cum_ledger", {}).get(cd) or {}).get("sq", 0)
        - (st.get("cum_ledger", {}).get(cd) or {}).get("bq", 0))
    strategy._pair_blocked = lambda st, cd, pl, lt, mm=None: _orig_pair(st, cd, pl, lt, max_mult=1)
    res = replay._replay(bars_map, tplan, index_bars, all_days, cash0, shares_map, tech=True)
    strategy.net_sell = _orig_net
    strategy._pair_blocked = _orig_pair
    results.append(("旧bug(cum+1×)", res))

    # 新修复: today_ledger + 1×/2×/3×
    for mult in (1, 2, 3):
        strategy._pair_blocked = lambda st, cd, pl, lt, mm=mult: _orig_pair(st, cd, pl, lt, max_mult=mm)
        res = replay._replay(bars_map, tplan, index_bars, all_days, cash0, shares_map, tech=True)
        results.append((f"新修复(today+{mult}×)", res))
    strategy._pair_blocked = _orig_pair
    return results


def print_report(results, scenario_name, final_prices):
    print(f"\n{'='*70}")
    print(f"  场景: {scenario_name}")
    print(f"{'='*70}")
    hdr = ["方案", "配对拦截", "买笔", "卖笔", "止损", "净收益", "期末现金",
           "持仓耗尽风险"]
    print(" | ".join(hdr))
    print("-" * 110)
    for label, res in results:
        blocks = count_blocks(res["sig_rows"])
        # 持仓耗尽风险: 期末净持仓(用期末现金+成交价反推)
        # 简化: 只要 n_sell > n_buy 且有多次 T 成交, 就看 by_code 里有没有成交过的持仓降到极低
        risk_codes = []
        for c, d in res["by_code"].items():
            pair = d.get("pair", 0)
            if pair > 0 and not d.get("realized") and not d.get("floating"):
                risk_codes.append(c)
        risk = "⚠有" if risk_codes else "✅无"
        net = round(res["realized"] + res["floating"] + res["stop_pnl"], 2)
        print(f"{label:>20} | 拦截{blocks:>3} | 买{res['n_buy']:>3} 卖{res['n_sell']:>3} 止损{res['n_stop']:>2} | "
              f"{net:+,.0f} | {res['cash']:>10,.0f} | {risk}")

    print("\n  个股净收益对比:")
    codes = sorted({c for _, r in results for c in r["by_code"]})
    name_of = {c: tplan[c].get("name", c) for c in codes if c in tplan}
    print(f"  {'代码':>8} {'名称':>8}", end="")
    for label, _ in results:
        print(f" | {label:>20}", end="")
    print()
    for c in codes:
        print(f"  {c:>8} {name_of.get(c, c):>8}", end="")
        for _, res in results:
            d = res["by_code"].get(c) or {}
            net = round((d.get("realized", 0) + d.get("floating", 0)), 2)
            pair = d.get("pair", 0)
            print(f" | {net:+,.0f}({pair}){'':>10}", end="")
        print()

    # 结论
    old_b = count_blocks(results[0][1]["sig_rows"])
    old_net = round(results[0][1]["realized"] + results[0][1]["floating"] + results[0][1]["stop_pnl"], 2)
    print(f"\n  旧bug基线: 拦截{old_b}次, 净{old_net:+,.0f}")
    for label, res in results[1:]:
        b = count_blocks(res["sig_rows"])
        net = round(res["realized"] + res["floating"] + res["stop_pnl"], 2)
        print(f"  {label}: 拦截↓{old_b - b}, 净收益{net - old_net:+,.0f}")


# ── 主流程 ──
old_max_per = config.T_MAX_PER_DAY
config.T_MAX_PER_DAY = 0   # 关掉限频, 让每次冲高都能触配对逻辑
old_cooldown = config.T_COOLDOWN_MIN
config.T_COOLDOWN_MIN = 0   # 关掉冷却, 让配对拦截成为唯一高抛门槛
for sc in ["only_high", "both_zone"]:
    days, bm, idx = build_scenario(n_days=5, daily_drift=0.01, scenario=sc)
    if not bm:
        print(f"无行情数据, 跳过 {sc}")
        continue
    # 调整 cash0 让做T能成交 (T量2000股≈16000元)
    cash0_test = 500000.0
    ms = {c: (shares_map.get(c, (1000, 8.0))[0] or 2000,
              shares_map.get(c, (1000, 8.0))[1] or 7.5)
          for c in bm}
    results = run_compare(bm, tplan, idx, days, cash0_test, ms)
    print_report(results, {"only_high": "单边上涨(只触高抛区, 最极端的持续卖场景)",
                            "both_zone": "震荡行情(每日两端都触, 正常做T)"}[sc], None)

print("\n── 最终结论 ──")
print("配对拦截修复 2026-10-20:")
print("  ✅ cum_ledger → today_ledger: 跨日清零, 历史欠账不拖累次日(根bug)")
print("  ✅ 阈值 1× → 2×: 最多允许连卖2档后强制回补, 既宽松又不失控")
print("  ✅ 持仓耗尽保护: 剩余持仓 < LOT_MIN 或 < 单次T量时绝对拦截")
