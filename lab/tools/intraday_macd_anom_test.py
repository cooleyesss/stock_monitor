# -*- coding: utf-8 -*-
"""lab 隔离验证: 1分钟MACD票(第六票) + 异动票(第七票)
================================================================
背景(2026-10-20 用户决策): 
  ① 非对称守门放行低吸后, 低吸侧失去AI侧的MACD制约 -> 新增MACD机械票+低吸确认闸
  ② "瞬间大幅拉升/跌落"需结合疑似利好/利空 -> 新增异动票(无新闻源, 仅量价推断)
  ③ T_BUY_VR_MAX=1.2 导致低吸过量 -> 确认闸收敛低吸笔数
本脚本只读生产代码(import ttrade.vote / ttrade.replay), 不修改生产; 通过运行时改config开关做对比
三部分:
  A. 合成行情单元校验: MACD票/异动票在构造形态下的得分与veto是否符合设计
  B. 真实1分钟bar上的MACD翻转频率(判定MACD1M_AGG=1是否噪声过大)
  C. 分时回放对比: 开关OFF vs MACD(on/accel/any_neg) vs +异动 vs AGG=3
用法: python lab/tools/intraday_macd_anom_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config_ttrade as config
from common import quotes
import state as tstate
import vote, replay, account

FAIL = []


def _chk(label, ok, got):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {got}")
    if not ok:
        FAIL.append(label)


def _mk_bars(closes, vols=None, dt0="2026-10-20 09:31"):
    """按收盘价序列造1分钟bar(开=前收, 高/低包住实体)"""
    import datetime as _dt
    vols = vols or [1000.0] * len(closes)
    t0 = _dt.datetime.strptime(dt0, "%Y-%m-%d %H:%M")
    bars, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        bars.append({"dt": (t0 + _dt.timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M"),
                     "open": o, "close": c,
                     "high": max(o, c) * 1.0005, "low": min(o, c) * 0.9995,
                     "vol": vols[i], "amount": vols[i] * 100 * (o + c) / 2})
        prev = c
    return bars


# ---------------- A. 合成行情单元校验 ----------------
def part_a():
    print("═══ A. 合成行情单元校验 (MACD票 / 异动票) ═══")
    flat = [10.0] * 30
    up = flat + [10.0 + 0.02 * i for i in range(1, 16)]        # 稳步上行 -> 柱转正
    dn_acc = flat + [10.0 - 0.01 * i - 0.002 * i * i for i in range(1, 16)]  # 加速下跌 -> 柱负放大
    dn_calm = flat + [10.0 - 0.05 * i for i in range(1, 16)]    # 匀速下跌 -> 柱负但不放大

    cfg = (config.VOTE_MACD_EN, config.VOTE_MACD_W, config.MACD1M_AGG,
           config.MACD1M_BUY_VETO_MODE)
    config.VOTE_MACD_EN = True
    config.MACD1M_AGG = 1
    try:
        m = vote.calc_macd_1m(_mk_bars(up))
        _chk("MACD 稳步上行 -> 柱为正", bool(m and m["hist"] > 0), m and f"柱{m['hist']:+.4f}")
        config.MACD1M_BUY_VETO_MODE = "accel"
        vu = vote.vote_macd({"price": up[-1]}, _mk_bars(up))
        _chk("MACD票 上行 -> 得分>0 且不否决", vu["score"] > 0 and not vu["veto_buy"],
             (vu["score"], vu["reason"]))
        vd = vote.vote_macd({"price": dn_acc[-1]}, _mk_bars(dn_acc))
        _chk("MACD票 加速下跌 -> 得分<0 且 accel否决",
             vd["score"] < 0 and vd["veto_buy"], (vd["score"], vd["reason"]))
        vc = vote.vote_macd({"price": dn_calm[-1]}, _mk_bars(dn_calm))
        _chk("MACD票 匀速下跌 -> accel档不否决(柱负但收敛)",
             not vc["veto_buy"], (vc["score"], vc["reason"]))
        config.MACD1M_BUY_VETO_MODE = "any_neg"
        vc2 = vote.vote_macd({"price": dn_calm[-1]}, _mk_bars(dn_calm))
        _chk("MACD票 any_neg档 匀速下跌 -> 否决", vc2["veto_buy"], vc2["reason"])
        config.MACD1M_BUY_VETO_MODE = "off"
        vu3 = vote.vote_macd({"price": dn_acc[-1]}, _mk_bars(dn_acc))
        _chk("MACD票 veto=off -> 不否决", not vu3["veto_buy"], vu3["reason"])
    finally:
        (config.VOTE_MACD_EN, config.VOTE_MACD_W, config.MACD1M_AGG,
         config.MACD1M_BUY_VETO_MODE) = cfg

    # ---- 异动票 ----
    cfg2 = (config.VOTE_ANOM_EN, config.VOTE_ANOM_W, config.ANOM_PCT, config.ANOM_PCT_EXTREME)
    config.VOTE_ANOM_EN = True
    n = 25
    HV = [6000.0, 6000.0, 6400.0, 7000.0]      # 末4根放量(末根为形成中bar, 实际计入前3根)
    LV = [1000.0] * n
    try:
        # 急拉: 近3分钟放量拉升(10.05 -> 10.20)
        bump = [10.0] * n + [10.05, 10.10, 10.15, 10.20]
        va = vote.vote_anomaly({"price": 10.20}, _mk_bars(bump, LV + HV))
        _chk("异动票 放量急拉 -> 支持高抛(score<0)",
             va["valid"] and va["score"] < 0, (va["score"], va["reason"]))
        # 极端急跌: -2.5%, 放量
        dump = [10.0] * n + [9.95, 9.90, 9.85, 9.75]
        vd2 = vote.vote_anomaly({"price": 9.75}, _mk_bars(dump, LV + HV))
        _chk("异动票 极端放量急跌 -> veto_buy 禁止低吸",
             vd2["valid"] and vd2["veto_buy"], (vd2["score"], vd2["reason"]))
        # 中等急跌(-1.4%, 未达极端档) + 末根完成bar长下影企稳
        mid = [10.0] * n + [10.0, 9.95, 9.90, 9.86]
        bars = _mk_bars(mid, LV + HV)
        lb = bars[-2]                          # 末根"已完成"bar
        lb.update({"open": 9.92, "close": 9.90, "low": 9.80})
        vt = vote.vote_anomaly({"price": 9.86}, bars)
        _chk("异动票 中等急跌后末根企稳 -> 支持低吸(score>0)",
             vt["valid"] and vt["score"] > 0, (vt["score"], vt["reason"]))
        # 中等急跌但未见企稳 -> 弃权
        bars2 = _mk_bars(mid, LV + HV)
        bars2[-2].update({"open": 9.90, "close": 9.82, "low": 9.815})
        vn2 = vote.vote_anomaly({"price": 9.86}, bars2)
        _chk("异动票 急跌未见企稳 -> 弃权(不加分不否决)",
             (not vn2["valid"]) and (not vn2["veto_buy"]), vn2["reason"])
        # 无异动 -> 弃权
        vn = vote.vote_anomaly({"price": 10.0}, _mk_bars([10.0] * (n + 4)))
        _chk("异动票 价格平稳 -> 弃权(valid=False)",
             (not vn["valid"]) and vn["score"] == 0, vn["reason"])
    finally:
        (config.VOTE_ANOM_EN, config.VOTE_ANOM_W, config.ANOM_PCT,
         config.ANOM_PCT_EXTREME) = cfg2

    # ---- evaluate 门控联通性 ----
    print("  -- evaluate 硬否决联通性 --")
    plan = {"name": "X", "buy_low": 9.0, "buy_high": 10.5, "t_qty": 100}
    bars = _mk_bars(dump, LV + HV)
    cfg3 = (config.VOTE_MACD_EN, config.VOTE_ANOM_EN, config.MACD1M_AGG)
    config.VOTE_MACD_EN, config.VOTE_ANOM_EN, config.MACD1M_AGG = False, True, 1
    try:
        res = vote.evaluate(plan, {"price": 9.75, "vr": 0.5}, bars)
        _chk("evaluate 异动极端急跌 -> 低吸gate=anom_veto",
             res["buy"]["gate"] == "anom_veto", (res["buy"]["gate"], res["buy"]["pass"]))
    finally:
        config.VOTE_MACD_EN, config.VOTE_ANOM_EN, config.MACD1M_AGG = cfg3


# ---------------- B. MACD 翻转频率 ----------------
def part_b(bars_map):
    print("═══ B. 真实1分钟bar上 MACD 翻转频率 (判定 AGG 档位) ═══")
    for agg in (1, 3):
        flips, n = 0, 0
        for code, bars in bars_map.items():
            by_day = {}
            for b in bars:
                by_day.setdefault(b["dt"][:10], []).append(b)
            for day, db in by_day.items():
                prev_sign = None
                for j in range(31, len(db)):
                    m = vote.calc_macd_1m(db[:j + 1], agg=agg)
                    if not m:
                        continue
                    s = 0 if abs(m["hist"]) < 1e-9 else (1 if m["hist"] > 0 else -1)
                    if prev_sign is not None and s != 0 and prev_sign != 0 and s != prev_sign:
                        flips += 1
                    if s:
                        prev_sign = s
                    n += 1
        print(f"  AGG={agg}分钟: 有效bar {n}, 柱体符号翻转 {flips} 次"
              f" (翻转率 {flips / n * 100:.1f}%, 每{'-' if not flips else round(n / flips, 1)}bar一次)")
    print("  -- 异动触发率诊断(真实bar, 阈值网格) --")
    k = config.ANOM_WIN
    grid = [(1.2, 2.5), (1.2, 2.0), (1.0, 2.0), (1.0, 1.5), (0.8, 2.0), (0.8, 1.5)]
    stats = {g: {"up": 0, "dn": 0, "dn_ext": 0, "n": 0} for g in grid}
    for code, bars in bars_map.items():
        by_day = {}
        for b in bars:
            by_day.setdefault(b["dt"][:10], []).append(b)
        for day, db in by_day.items():
            for j in range(21, len(db)):
                done = db[:j]
                c0 = done[-k]["close"]
                p = db[j]["close"]
                if not c0:
                    continue
                move = (p / c0 - 1) * 100.0
                mv = sum(b["vol"] for b in done[-20:]) / 20.0
                vm = (sum(b["vol"] for b in done[-k:]) / k / mv) if mv > 0 else 0.0
                for (ap, av) in grid:
                    s = stats[(ap, av)]
                    s["n"] += 1
                    if abs(move) < ap or vm < av:
                        continue
                    if move > 0:
                        s["up"] += 1
                    else:
                        s["dn"] += 1
                        if -move >= config.ANOM_PCT_EXTREME:
                            s["dn_ext"] += 1
    for (ap, av) in grid:
        s = stats[(ap, av)]
        print(f"    幅度≥{ap}% 量≥{av}倍: 急拉{s['up']} 急跌{s['dn']}"
              f"(其中极端档{s['dn_ext']}) / 共{s['n']}bar"
              f" = 触发率{(s['up'] + s['dn']) / max(s['n'], 1) * 100:.2f}%")


# ---------------- C. 分时回放对比 ----------------
MODES = [
    ("M0 基线(新票全关)",      dict(VOTE_MACD_EN=False, VOTE_ANOM_EN=False, MACD1M_BUY_VETO_MODE="off", MACD1M_AGG=1, ANOM_PCT=1.2, ANOM_VOL_MULT=2.5)),
    ("M1 MACD软票(veto=off)",  dict(VOTE_MACD_EN=True, VOTE_ANOM_EN=False, MACD1M_BUY_VETO_MODE="off", MACD1M_AGG=1, ANOM_PCT=1.2, ANOM_VOL_MULT=2.5)),
    ("M2 MACD确认闸(accel)",   dict(VOTE_MACD_EN=True, VOTE_ANOM_EN=False, MACD1M_BUY_VETO_MODE="accel", MACD1M_AGG=1, ANOM_PCT=1.2, ANOM_VOL_MULT=2.5)),
    ("M3 MACD确认闸(any_neg)", dict(VOTE_MACD_EN=True, VOTE_ANOM_EN=False, MACD1M_BUY_VETO_MODE="any_neg", MACD1M_AGG=1, ANOM_PCT=1.2, ANOM_VOL_MULT=2.5)),
    ("M4 MACD(accel)+异动(严)", dict(VOTE_MACD_EN=True, VOTE_ANOM_EN=True, MACD1M_BUY_VETO_MODE="accel", MACD1M_AGG=1, ANOM_PCT=1.2, ANOM_VOL_MULT=2.5)),
    ("M5 MACD 3分钟聚合(accel)", dict(VOTE_MACD_EN=True, VOTE_ANOM_EN=False, MACD1M_BUY_VETO_MODE="accel", MACD1M_AGG=3, ANOM_PCT=1.2, ANOM_VOL_MULT=2.5)),
    ("M6 MACD(accel)+异动(松)", dict(VOTE_MACD_EN=True, VOTE_ANOM_EN=True, MACD1M_BUY_VETO_MODE="accel", MACD1M_AGG=1, ANOM_PCT=0.8, ANOM_VOL_MULT=2.0)),
    ("M7 生产最终配置", dict(VOTE_MACD_EN=True, VOTE_ANOM_EN=True, MACD1M_BUY_VETO_MODE="accel", MACD1M_AGG=1, ANOM_PCT=0.8, ANOM_VOL_MULT=1.5)),
]


def part_c(bars_map, tplan, index_bars, all_days, cash0, shares_map):
    print("═══ C. 分时回放对比 (真实腾讯1分钟bar, 无AI守门/无降级升级) ═══")
    keys = ("VOTE_MACD_EN", "VOTE_ANOM_EN", "MACD1M_BUY_VETO_MODE", "MACD1M_AGG",
            "ANOM_PCT", "ANOM_VOL_MULT")
    base = {k: getattr(config, k) for k in keys}
    hdr = f"  {'模式':<24}{'买':>4}{'卖':>4}{'止损':>5}{'持仓净增':>9}{'期末现金':>13}{'已回补':>11}{'浮动':>11}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    detail = {}
    try:
        for label, cfg in MODES:
            for k in keys:
                setattr(config, k, cfg[k])
            res = replay._replay(bars_map, tplan, index_bars, all_days,
                                 cash0, shares_map, tech=True)
            # 净持仓变化 = 成交低吸股数 - 成交高抛股数
            net_delta = 0
            for r in res["sig_rows"]:
                if r[5] == "成交":
                    net_delta += r[12] if r[4] == "低吸" else -r[12]
            net = round(res["realized"] + res["floating"] + res["stop_pnl"], 2)
            print(f"  {label:<24}{res['n_buy']:>4}{res['n_sell']:>4}{res['n_stop']:>5}"
                  f"{net_delta:>9}{res['cash']:>13,.0f}"
                  f"{res['realized']:>11,.0f}{res['floating']:>11,.0f}")
            gates = {}
            for r in res["sig_rows"]:
                if r[4] == "低吸" and r[5] == "投票未共振":
                    gates[r[6]] = gates.get(r[6], 0) + 1
            detail[label] = {"net": net, "gates": gates, "n_buy": res["n_buy"],
                             "n_sell": res["n_sell"], "net_delta": net_delta,
                             "cash": res["cash"], "fees": res["fees"]}
    finally:
        for k in keys:
            setattr(config, k, base[k])
    print("\n  -- 低吸被拦 gate 明细(仅'投票未共振'记录) --")
    allg = sorted({g for d in detail.values() for g in d["gates"]})
    print("  " + f"{'模式':<24}" + "".join(f"{g:>14}" for g in allg))
    for label, _ in MODES:
        g = detail[label]["gates"]
        print(f"  {label:<24}" + "".join(f"{g.get(x, 0):>14}" for x in allg))
    return detail


def main():
    print("═" * 78)
    print("lab 隔离验证: MACD票(第六票) + 异动票(第七票)  [生产开关默认关闭, 本脚本只读]")
    print("═" * 78)
    part_a()

    tplan = tstate.load_t_plan()
    if not tplan:
        print("\n[中止] 未找到做T计划:", config.T_PLAN_CSV)
        return
    print(f"\n做T计划 {len(tplan)} 只: " + ", ".join(tplan[c].get("name", c) for c in tplan))
    print("拉取1分钟bar(东财trends已拒连 -> 走腾讯回退)...")
    try:
        index_bars = quotes.fetch_kline_bars(config.REGIME_INDEX_ALIAS, klt=1, lmt=config.K1_LMT)
    except Exception as e:
        print("[中止] 指数1分钟bar获取失败:", e)
        return
    bars_map = {}
    for code in tplan:
        try:
            b = quotes.fetch_kline_bars(code, klt=1, lmt=config.K1_LMT)
            if b:
                bars_map[code] = b
        except Exception as e:
            print(f"  {code} 失败跳过: {e}")
    if not bars_map:
        print("[中止] 无个股1分钟bar")
        return
    days = sorted({b["dt"][:10] for b in index_bars}
                  | {d for bs in bars_map.values() for d in {b["dt"][:10] for b in bs}})
    print(f"窗口: {days[0]} ~ {days[-1]} ({len(days)}日) | "
          f"{len(bars_map)}只 | 共{sum(len(v) for v in bars_map.values())}根1分钟bar")

    part_b(bars_map)

    st = tstate.load_state() or tstate.init_state()
    shares_map = {c: (st["positions"].get(c, {}).get("shares", 0),
                      st["positions"].get(c, {}).get("cost", 0.0)) for c in bars_map}
    cash0 = st["cash"]
    print(f"\n初始: 现金{cash0:,.0f} 底仓{sum(1 for s, _ in shares_map.values() if s > 0)}只")
    detail = part_c(bars_map, tplan, index_bars, days, cash0, shares_map)

    print("\n═══ 结论(供人工确认, 生产默认仍为关闭) ═══")
    fail = "有FAIL项" if FAIL else "全部PASS"
    print(f"  A单元校验: {fail}" + (f" -> {FAIL}" if FAIL else ""))
    m0, m2 = detail["M0 基线(新票全关)"], detail["M2 MACD确认闸(accel)"]
    print(f"  低吸笔数 基线{m0['n_buy']} -> accel闸{m2['n_buy']}; "
          f"高抛 基线{m0['n_sell']} -> {m2['n_sell']}")
    print(f"  净持仓 基线{m0['net_delta']}股 -> {m2['net_delta']}股")


if __name__ == "__main__":
    main()
