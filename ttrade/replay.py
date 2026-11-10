# -*- coding: utf-8 -*-
"""历史回测: 东财trends2分时(最多5个交易日)逐bar回放量化投票引擎
与实盘零漂移: 逐bar调vote.evaluate(同一决策核), 复刻T+1/费用模型/整手/限频/冷却/配对T
输出: 控制台每股一行+合计+zone-only对照 + backtest_signals_*.csv + backtest_summary_*.csv
诚实口径(见运行时头部声明): AI守门未模拟 / 量比增量法近似 / 窗口≤5日 / 首日基准近似
用法: python ttrade/main.py --backtest [--bt-days 5] [--bt-codes 600613,601727] [--bt-zone-only]
"""
import csv
import datetime
import os

from ttrade import config_ttrade as config
from ttrade import state as tstate
from common import quotes
from ttrade import account
from ttrade import strategy
from ttrade import vote

SIG_HEADERS = ["模式", "日期", "时间", "代码", "名称", "方向", "结果", "gate",
               "总分", "共振", "主导票", "量比", "价格", "股数", "备注"]


# ---------------- 数据工具 ----------------
def _prev_close(bars, day, day_list):
    """前一日收盘价; 窗口首日无前收 -> 当日首bar开盘价近似"""
    i = day_list.index(day)
    if i > 0:
        pb = vote.bars_of_day(bars, day_list[i - 1])
        if pb:
            return pb[-1]["close"]
    db = vote.bars_of_day(bars, day)
    return db[0]["open"] if db else None


def _vol_per_min(bars, day_list, day, lookback=5):
    """day之前(最多lookback个交易日)的每分钟平均量(手); 无历史日返回None"""
    i = day_list.index(day)
    tot_v, tot_n = 0.0, 0
    for d in day_list[max(0, i - lookback):i]:
        db = vote.bars_of_day(bars, d)
        tot_v += sum(b["vol"] for b in db)
        tot_n += len(db)
    return (tot_v / tot_n) if tot_n else None


def _precompute_vr(day_bars, prev_vol_per_min):
    """量比增量法近似: 当日截至本bar的每分钟均量 ÷ 前几日每分钟均量
    无历史日基准 -> 全天None(等效实盘量比无数据, 不构成否决)"""
    n = len(day_bars)
    if not prev_vol_per_min or n == 0:
        return [None] * n
    cum, out = 0.0, []
    for i, b in enumerate(day_bars):
        cum += b["vol"]
        out.append(cum / (i + 1) / prev_vol_per_min)
    return out


# ---------------- 轻量账户 ----------------
def _acct_new(cash0, shares_map, tplan):
    """初始现金/底仓取自实盘模拟盘state; 不写文件不推送"""
    return {
        "cash": cash0,
        "positions": {c: {"name": tplan[c].get("name", c),
                          "shares": s, "cost": cst, "today_bought": 0}
                      for c, (s, cst) in shares_map.items()},
        "today_lot": {}, "t_hist": {}, "armed": [], "zone_since": {},
        "cum_ledger": {}, "today_ledger": {}, "stop_done": {}, "stop_pending": {},
        "trades": [], "fees": 0.0, "stop_pnl": 0.0,
    }


def _day_reset(bt):
    """日切(同tstate.rollover的日内重置)"""
    bt["today_lot"] = {}
    bt["t_hist"] = {}
    bt["armed"] = []
    bt["zone_since"] = {}
    bt["today_ledger"] = {}
    bt["stop_done"] = {}
    bt["stop_pending"] = {}
    for pos in bt["positions"].values():
        pos.pop("today_bought", None)


def _exec_buy(bt, code, name, qty, price, tag, day, tm):
    amount = round(qty * price, 2)
    fee = account.buy_fee(amount, code)
    bt["cash"] = round(bt["cash"] - amount - fee, 2)
    pos = bt["positions"].setdefault(code, {"name": name, "shares": 0, "cost": 0.0})
    old = pos["shares"]
    pos["shares"] = old + qty
    pos["cost"] = round((pos["cost"] * old + amount + fee) / pos["shares"], 4)
    pos["today_bought"] = pos.get("today_bought", 0) + qty
    for led in (bt["today_ledger"], bt["cum_ledger"]):
        e = led.setdefault(code, {"bq": 0, "ba": 0.0, "sq": 0, "sa": 0.0})
        e["bq"] += qty
        e["ba"] = round(e["ba"] + amount, 2)
    bt["fees"] = round(bt["fees"] + fee, 2)
    bt["trades"].append({"date": day, "time": tm, "code": code, "name": name,
                         "side": "buy", "qty": qty, "price": price,
                         "fee": fee, "strat": tag})


def _exec_sell(bt, code, name, qty, price, tag, day, tm,
               side="sell", in_ledger=True):
    pos = bt["positions"].get(code)
    if not pos or qty < config.LOT_MIN:
        return False
    amount = round(qty * price, 2)
    fee = account.sell_fee(amount, code)
    bt["cash"] = round(bt["cash"] + amount - fee, 2)
    if in_ledger:
        for led in (bt["today_ledger"], bt["cum_ledger"]):
            e = led.setdefault(code, {"bq": 0, "ba": 0.0, "sq": 0, "sa": 0.0})
            e["sq"] += qty
            e["sa"] = round(e["sa"] + amount, 2)
    else:   # 止损清仓: 按成本口径记止损盈亏
        bt["stop_pnl"] = round(bt["stop_pnl"]
                               + qty * (price - pos.get("cost", 0.0)) - fee, 2)
    bt["fees"] = round(bt["fees"] + fee, 2)
    pos["shares"] -= qty
    if pos["shares"] == 0:
        pos["cost"] = 0.0
        pos["today_bought"] = 0
    bt["trades"].append({"date": day, "time": tm, "code": code, "name": name,
                         "side": side, "qty": qty, "price": price,
                         "fee": fee, "strat": tag})
    return True


# ---------------- 逐bar信号检查(镜像strategy.check_signal, 无AI无推送) ----------------
def _bt_check(bt, code, name, plan, q, bars_today, regime, tech,
              sig_rows, day, now):
    p, vr = q["price"], q["vr"]
    dt_txt = now.strftime("%Y-%m-%d %H:%M")
    tm = dt_txt[11:] + ":00"
    pos = bt["positions"].setdefault(code, {"name": name, "shares": 0, "cost": 0.0})
    lot = account.lot_of(bt, code, pos["shares"], plan.get("t_qty"))

    def _armed(k):
        return f"{code}|{k}" in bt["armed"]

    def _arm(k):
        if f"{code}|{k}" not in bt["armed"]:
            bt["armed"].append(f"{code}|{k}")

    def _disarm(k):
        if f"{code}|{k}" in bt["armed"]:
            bt["armed"].remove(f"{code}|{k}")

    def _sig(side, res, executed, qty, note=""):
        s = res[side] if res else {}
        if executed:
            result = "成交"
        elif res and s.get("gate") and s.get("gate") != "ok":
            result = "投票未共振"
        else:
            result = "未成交"
        sig_rows.append([day, tm, code, name,
                         "低吸" if side == "buy" else "高抛",
                         result, s.get("gate", ""), s.get("score", ""),
                         f"{s.get('agree', '-')}/{s.get('agree_need', '-')}",
                         vote.main_tag(res, side) if res else "",
                         f"{vr:.2f}" if vr is not None else "",
                         f"{p:.3f}", qty, note])

    # ── 止损挂起(跳空豁免中): 收复->解除; 到收盘确认时点仍破位->清仓 ──
    pend_s = bt["stop_pending"]
    if pend_s.get(code) == day and (plan.get("stop_loss") or []):
        sline = max(plan["stop_loss"])
        if p >= sline:
            pend_s.pop(code)
            sig_rows.append([day, tm, code, name, "止损", "豁免解除", "stop_gap", "",
                             "", "", "", f"{p:.3f}", 0,
                             "收复止损线, 假破位解除"])
        else:
            if now.time() >= strategy._stop_confirm_time():
                pend_s.pop(code)
                bt["stop_done"][code] = day
                _disarm("buy")
                _disarm("sell")
                if config.STOP_LOSS_MODE == "clear" and pos["shares"] >= config.LOT_MIN:
                    qty0 = pos["shares"]
                    _exec_sell(bt, code, name, qty0, p, "", day, tm,
                               side="stop_clear", in_ledger=False)
                    sig_rows.append([day, tm, code, name, "止损", "成交",
                                     "stop_gap", "", "", "", "",
                                     f"{p:.3f}", qty0, "收盘确认仍破位, 清仓"])
                return
            return   # 挂起中: 本轮跳过其它T动作

    # ── 止损(优先, 纪律不走投票不走AI, 同实盘模式) ──
    stops = plan.get("stop_loss") or []
    if stops and config.STOP_LOSS_MODE != "alert":
        line = max(stops)
        if bt["stop_done"].get(code) != day and p <= line:
            if config.ST_GAP_EXEMPT_EN:
                op = bars_today[0]["open"] if bars_today else None
                gap = ((line - op) / line * 100.0) if op else 0.0
                if op and gap >= config.ST_GAP_PCT:
                    bt["stop_pending"][code] = day
                    sig_rows.append([day, tm, code, name, "止损", "挂起待确认",
                                     "stop_gap", "", "", "", "",
                                     f"{p:.3f}", 0,
                                     f"开盘{op:.3f}深穿止损线{gap:.2f}%, 豁免待收盘确认"])
                    return
            bt["stop_done"][code] = day
            _disarm("buy")
            _disarm("sell")
            if config.STOP_LOSS_MODE == "clear":
                if pos["shares"] >= config.LOT_MIN:
                    qty0 = pos["shares"]
                    _exec_sell(bt, code, name, qty0, p, "", day, tm,
                               side="stop_clear", in_ledger=False)
                    sig_rows.append([day, tm, code, name, "止损", "成交",
                                     "stop", "", "", "", "",
                                     f"{vr:.2f}" if vr is not None else "",
                                     f"{p:.3f}", qty0, "止损清仓"])
            else:   # t_only
                led = bt["today_ledger"].get(code) or {}
                extra = (led.get("bq", 0) - led.get("sq", 0)) // 100 * 100
                tb = pos.get("today_bought", 0)
                avail = (pos["shares"] - tb) // 100 * 100
                qty = min(extra, avail)
                if qty >= config.LOT_MIN:
                    _exec_sell(bt, code, name, qty, p, "", day, tm, side="stop_t")
                    sig_rows.append([day, tm, code, name, "止损", "成交",
                                     "stop", "", "", "", "",
                                     f"{vr:.2f}" if vr is not None else "",
                                     f"{p:.3f}", qty, "止损卖出T仓"])
            return
    if bt["stop_done"].get(code) == day:
        return

    # ── 低吸: 到位+量比(或dwell超时豁免) -> 投票共振 -> 成交 ──
    if "buy_low" in plan:
        if plan["buy_low"] <= p <= plan["buy_high"]:
            zkey = f"{code}|buy"
            ent = bt["zone_since"].get(zkey)
            if not ent:
                bt["zone_since"][zkey] = dt_txt
                dwell = 0.0
            else:
                try:
                    t0 = datetime.datetime.strptime(ent, "%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    t0 = now
                dwell = (now - t0).total_seconds() / 60
            vmax = plan.get("buy_vr_max")
            vr_ok = vmax is None or vr is None or vr <= vmax
            if (not vr_ok) and config.T_DWELL_MIN \
                    and dwell >= config.T_DWELL_MIN:
                vr_ok = True   # 低吸区停留超时豁免(同实盘)
            if vr_ok and not _armed("buy"):
                _arm("buy")
                blocked, why = strategy._freq_blocked(bt, code, "buy", now)
                if blocked:
                    _sig("buy", None, False, 0, f"限频:{why}")
                else:
                    res = vote.evaluate(plan, q, bars_today, dwell_min=dwell,
                                         regime=regime, tech=tech)
                    if res["buy"]["pass"]:
                        if lot < config.LOT_MIN:
                            _sig("buy", res, False, 0, f"T量不足{lot}股")
                        else:
                            qty = account.affordable_qty(bt, code, p, lot)
                            if qty < config.LOT_MIN:
                                _sig("buy", res, False, 0, "现金不足")
                            else:
                                tag = vote.main_tag(res, "buy")
                                _exec_buy(bt, code, name, qty, p, tag, day, tm)
                                strategy._record_t_hist(bt, code, "buy", now)
                                _sig("buy", res, True, qty)
                    else:
                        _disarm("buy")
                        if not _armed("votefail"):
                            _arm("votefail")
                            _sig("buy", res, False, 0)
        else:
            _disarm("buy")
            _disarm("buytouch")
            _disarm("votefail")
            bt["zone_since"].pop(f"{code}|buy", None)

    # ── 高抛: 到位+量比 -> 配对检查 -> 投票共振 -> 成交(T+1卖昨仓) ──
    if "sell_low" in plan:
        if plan["sell_low"] <= p <= plan["sell_high"]:
            vmin = plan.get("sell_vr_min")
            vr_ok = vmin is None or vr is None or vr >= vmin
            if vr_ok and not _armed("sell"):
                _arm("sell")
                blocked, why = strategy._freq_blocked(bt, code, "sell", now)
                if blocked:
                    _sig("sell", None, False, 0, f"限频:{why}")
                else:
                    pblocked, pwhy = strategy._pair_blocked(bt, code, plan, lot)
                    if pblocked:
                        _sig("sell", None, False, 0, f"配对:{pwhy}")
                    else:
                        res = vote.evaluate(plan, q, bars_today, dwell_min=0.0,
                                             regime=regime, tech=tech)
                        if res["sell"]["pass"]:
                            tb = pos.get("today_bought", 0)
                            avail = pos["shares"] - (tb if config.T_PLUS_ONE else 0)
                            qty = min(lot, avail // 100 * 100)
                            if qty < config.LOT_MIN:
                                _sig("sell", res, False, 0, "T+1可卖不足")
                            else:
                                tag = vote.main_tag(res, "sell")
                                _exec_sell(bt, code, name, qty, p, tag, day, tm)
                                strategy._record_t_hist(bt, code, "sell", now)
                                _sig("sell", res, True, qty)
                        else:
                            _disarm("sell")
                            if not _armed("votefail"):
                                _arm("votefail")
                                _sig("sell", res, False, 0)
        else:
            _disarm("sell")
            _disarm("selltouch")
            _disarm("votefail")


# ---------------- 回放主流程 ----------------
def _replay(bars_map, tplan, index_bars, all_days, cash0, shares_map, tech):
    bt = _acct_new(cash0, shares_map, tplan)
    sig_rows = []
    for day in all_days:
        _day_reset(bt)
        # 大盘门控: 指数逐bar(全股共享), 按dt映射; 指数缺数据=neutral不拦截
        idx_d = vote.bars_of_day(index_bars, day)
        idx_prev = _prev_close(index_bars, day, all_days)
        regime_by_dt = {}
        for j, ib in enumerate(idx_d):
            if idx_prev:
                iq = {"price": ib["close"],
                      "pct": (ib["close"] / idx_prev - 1) * 100}
                regime_by_dt[ib["dt"]] = vote.regime_state(iq, idx_d[:j + 1])
        # 逐股逐bar回放
        for code, plan in tplan.items():
            bars = bars_map.get(code)
            if not bars:
                continue
            d_bars = vote.bars_of_day(bars, day)
            if not d_bars:
                continue
            name = plan.get("name", code)
            prev = _prev_close(bars, day, all_days)
            vrs = _precompute_vr(d_bars, _vol_per_min(bars, all_days, day))
            for j, b in enumerate(d_bars):
                now = datetime.datetime.strptime(b["dt"], "%Y-%m-%d %H:%M")
                q = {"price": b["close"], "vr": vrs[j],
                     "pct": ((b["close"] / prev - 1) * 100) if prev else 0}
                _bt_check(bt, code, name, plan, q, d_bars[:j + 1],
                          regime_by_dt.get(b["dt"]), tech, sig_rows, day, now)
    # 结算: FIFO配对已回补盈亏 + 未回补腿按窗口末价浮动
    pairs, opens = account.fifo_pairs(bt["trades"])
    realized = round(sum(p["pnl"] for p in pairs), 2)
    by_code = {}
    for p in pairs:
        d = by_code.setdefault(p["code"], {"realized": 0.0, "floating": 0.0,
                                           "win": 0, "pair": 0})
        d["realized"] = round(d["realized"] + p["pnl"], 2)
        d["pair"] += 1
        if p["pnl"] > 0:
            d["win"] += 1
    floating = 0.0
    for o in opens:
        bars = bars_map.get(o["code"]) or []
        px = bars[-1]["close"] if bars else o["price"]
        diff = (px - o["price"]) if o["side"] == "buy" else (o["price"] - px)
        fl = o["qty"] * diff - o["fps"] * o["qty"]
        floating += fl
        d = by_code.setdefault(o["code"], {"realized": 0.0, "floating": 0.0,
                                           "win": 0, "pair": 0})
        d["floating"] = round(d["floating"] + fl, 2)
    res = {"realized": realized, "floating": round(floating, 2),
           "stop_pnl": bt["stop_pnl"], "fees": bt["fees"],
           "cash": bt["cash"], "sig_rows": sig_rows,
           "n_buy": sum(1 for t in bt["trades"] if t["side"] == "buy"),
           "n_sell": sum(1 for t in bt["trades"] if t["side"] == "sell"),
           "n_stop": sum(1 for t in bt["trades"] if t["side"].startswith("stop")),
           "by_code": by_code}
    return res


# ---------------- 入口 ----------------
def run(days=5, codes=None, zone_only=False):
    days = max(1, min(int(days), 5))
    tplan = tstate.load_t_plan()
    if not tplan:
        print("未找到做T计划:", config.T_PLAN_CSV)
        return
    if codes:
        codes = [c.strip() for c in codes if c.strip()]
        tplan = {c: tplan[c] for c in codes if c in tplan}
        if not tplan:
            print("指定的代码都不在做T计划中:", ",".join(codes))
            return
    print(f"拉取分时数据(东财trends2, 最近{days}个交易日, 上限5)...")
    try:
        index_bars = quotes.fetch_trends_bars(config.REGIME_INDEX_ALIAS, ndays=days)
    except Exception as e:
        print("指数分时获取失败, 中止回测:", e)
        return
    if not index_bars:
        print("指数分时数据为空(可能非交易时段接口维护), 中止回测")
        return
    bars_map = {}
    for code in tplan:
        try:
            bars = quotes.fetch_trends_bars(code, ndays=days)
            if bars:
                bars_map[code] = bars
            else:
                print(f"  {code} 无分时数据, 跳过")
        except Exception as e:
            print(f"  {code} 分时获取失败, 跳过: {e}")
    if not bars_map:
        print("无可用个股分时数据, 中止回测")
        return
    all_days = sorted({b["dt"][:10] for b in index_bars}
                     | {d for c, bs in bars_map.items()
                        for d in {b["dt"][:10] for b in bs}})
    # 初始账户: 沿用实盘模拟盘现金与底仓(做T需底仓, 无底仓代码只有买入侧生效)
    st = tstate.load_state() or tstate.init_state()
    shares_map = {c: (st["positions"].get(c, {}).get("shares", 0),
                      st["positions"].get(c, {}).get("cost", 0.0))
                  for c in bars_map}
    cash0 = st["cash"]
    now = datetime.datetime.now()
    sig_csv = os.path.join(config.BASE_DIR, f"backtest_signals_{now:%Y%m%d}.csv")
    sum_csv = os.path.join(config.BASE_DIR, f"backtest_summary_{now:%Y%m%d}.csv")

    modes = [("zone-only(旧策略)", True)] if zone_only else \
            [("多策略投票", False), ("zone-only(旧策略对照)", True)]
    results = []
    for label, tech_off in modes:
        res = _replay(bars_map, tplan, index_bars, all_days, cash0,
                      shares_map, tech=not tech_off)
        results.append((label, res))

    # ---- 控制台报告 ----
    n_bars = sum(len(vote.bars_of_day(bars_map[c], d))
                 for c in bars_map for d in all_days)
    print()
    print("══════════ 多策略量化引擎·历史回测 ══════════")
    print(f"窗口: {all_days[0]} ~ {all_days[-1]} ({len(all_days)}个交易日, "
          f"共{len(bars_map)}只/{n_bars}根1分钟bar) | "
          f"初始: 现金{account.fmt_amt(cash0)}, "
          f"底仓{sum(1 for s, _ in shares_map.values() if s > 0)}只(取自模拟盘)")
    print("口径声明: ①AI守门未模拟(实盘DeepSeek观望会拦下部分信号, 成交数偏乐观) "
          "②量比=增量法近似(当日累计均量/分钟÷前几日均量) "
          "③东财分时接口窗口≤5日 ④首日无前收/量比基准(pct近似, 首日vr按无数据) "
          "⑤止损已复刻, 手动操作未模拟")
    sig_all = []
    sum_rows = []
    for label, res in results:
        print(f"── 模式: {label} ──")
        for code in bars_map:
            name = tplan[code].get("name", code)
            d = res["by_code"].get(code) or {}
            rz, fl = d.get("realized", 0.0), d.get("floating", 0.0)
            pair, win = d.get("pair", 0), d.get("win", 0)
            wr = f"{win}/{pair}" if pair else "--"
            if not (rz or fl or pair):
                print(f"  {name}({code}): 无成交")
                continue
            print(f"  {name}({code}): 已回补{account.fmt_pnl(rz)} "
                  f"浮动{account.fmt_pnl(fl)} 胜率{wr} "
                  f"小计{account.fmt_pnl(round(rz + fl, 2))}")
            sum_rows.append([label, code, name, str(pair),
                             account.fmt_pnl(rz), account.fmt_pnl(fl),
                             account.fmt_pnl(round(rz + fl, 2)), wr])
        net = round(res["realized"] + res["floating"] + res["stop_pnl"], 2)
        print(f"  合计: 成交买{res['n_buy']}/卖{res['n_sell']}"
              + (f"/止损{res['n_stop']}" if res["n_stop"] else "")
              + f" | 已回补{account.fmt_pnl(res['realized'])} "
              f"浮动{account.fmt_pnl(res['floating'])}"
              + (f" 止损{account.fmt_pnl(res['stop_pnl'])}" if res["n_stop"] else "")
              + f" | 净{account.fmt_pnl(net)} | 期末现金{account.fmt_amt(res['cash'])} "
              f"(费用合计{account.fmt_amt(res['fees'])}, 已计入盈亏口径)")
        sig_all.extend([[label] + r for r in res["sig_rows"]])
    # ---- CSV输出 ----
    with open(sig_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(SIG_HEADERS)
        w.writerows(sig_all)
    with open(sum_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["模式", "代码", "名称", "已回补对子", "已回补盈亏",
                    "浮动", "净", "胜率"])
        w.writerows(sum_rows)
    print(f"信号明细: {os.path.basename(sig_csv)} ({len(sig_all)}行) | "
          f"汇总: {os.path.basename(sum_csv)}")
