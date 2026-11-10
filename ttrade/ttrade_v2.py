# -*- coding: utf-8 -*-
"""做T策略v2: 反T编排 + 网格回补 + 环境过滤 + 收盘兜底 (2026-10-30 用户需求, 回测验证)

背景(数据诊断): 旧策略15个交易日61笔成交仅3对当日回补(回补率≈6%), 三大根因:
  ① T+1死锁: "先买后卖"正T的买入腿当日制度性不可卖, 48条腿挂起
  ② 低吸区接飞刀: BOLL下轨+0.1ATR就接, 止损线仅低0.5ATR, 下跌中继反复触发
  ③ 止损清仓后无任何回补通道(兰石10-30低吸5.43踩止损线5.43即典型)

v2方案(市面常用做T技术整合, 与bt_ttrade.py回测口径一致):
  ① 反T为主: 高抛卖昨仓(高抛区 或 VWAP上乖离≥2%且收阳) -> 网格分档回补
     (卖价下方0.5/1.0/1.5×间距各接1/3) -> 14:45未回补部分现价兜底闭合
  ② 环境过滤: 竞价平开±1%弃做 / 单边下跌日(gap≤-2%或弱势股低开)禁正T / 14:40后不开新腿
  ③ 正T谨慎: 仅VWAP深乖离-2% + 近3根5分钟不创新低 + 收阳 才接, 且隔日开盘出场
  ④ 止损改告警: v2的网格回补+兜底即T腿风控, 底仓不动; 需清仓保护请关STRAT_V2_EN回退v1

回测(2026-10-22~10-30, 6只持仓股): 旧4对+1,696.47(止损2次) / 新40对+3,993.76
(回补率≈80%, 零止损, 费304<494); 已知偏差: 回测窗口偏空利于反T兜底, 单边上涨市兜底会小亏。

状态字段: state["v2_env"](当日环境) / state["v2_legs"](未回补卖出腿) /
         state["v2_carry"](正T隔日腿, 跨日保留) / state["v2_last_open"](开腿冷却)
回退: config.STRAT_V2_EN=False 即回退v1(区间+量化投票+AI守门), v2字段由rollover清理。
挂接: strategy.check_signal 顶部(STRAT_V2_EN=True时完全接管, 不与v1同日双跑)。
"""
import datetime
from ttrade import config_ttrade as config
from common import notify
from common import quotes
from ttrade import account
from ttrade import vote


# ---------------- 小工具 ----------------
def _hm(s):
    """'HH:MM' -> datetime.time(解析失败回退14:40)"""
    try:
        hh, mm = str(s).split(":")
        return datetime.time(int(hh), int(mm))
    except (ValueError, TypeError):
        return datetime.time(14, 40)


def _std_lot(qty):
    return max(int((qty or 0) // 100) * 100, 0)


def _cool(state, code, side, now):
    """同方向开腿冷却(分钟); state["v2_last_open"]由rollover每日清理"""
    if config.V2_COOLDOWN_MIN <= 0:
        return True
    last = state.get("v2_last_open", {}).get(f"{code}|{side}")
    if not last:
        return True
    try:
        t0 = datetime.datetime.strptime(f"{now:%Y-%m-%d} {last}", "%Y-%m-%d %H:%M:%S")
        return (now - t0).total_seconds() >= config.V2_COOLDOWN_MIN * 60
    except ValueError:
        return True


def _head(name, code, p, pct):
    chg = "🔴" if (pct or 0) > 0 else ("🟢" if (pct or 0) < 0 else "⚪")
    return (f"<b>{name}({code})</b> {account.fmt_price(p)} "
            f"{chg}{(pct or 0):+.2f}%")


def _do_buy(state, code, name, qty, price, note, tags, dry):
    """v2买入(自动按可用现金缩减); dry只打印; 返回rec或None"""
    qty = account.affordable_qty(state, code, price, qty)
    if qty < config.LOT_MIN:
        print(f"[v2] {name} 现金不足, 放弃买入"
              f"(需约{account.fmt_amt(qty * price)}, 有{account.fmt_amt(state['cash'])})")
        return None
    if dry:
        print(f"[DRY-v2] 买入 {name} {qty}股@{account.fmt_price(price)} ({note})")
        return None
    return account.execute_buy(state, code, name, qty, price, note=note,
                               strategy_tags=tags)


def _do_sell(state, code, name, qty, price, note, tags, dry):
    """v2卖出(T+1由execute_sell内部拦截昨仓口径); dry只打印"""
    if dry:
        print(f"[DRY-v2] 卖出 {name} {qty}股@{account.fmt_price(price)} ({note})")
        return None
    return account.execute_sell(state, code, name, qty, price, note=note,
                                strategy_tags=tags)


# ---------------- 当日环境判定(每日一次缓存) ----------------
def _env_of(state, code, plan, q, now):
    """竞价gap/平开弃做/单边下跌/网格间距(与bt_ttrade.daily_base+run_new同源)"""
    td = now.strftime("%Y-%m-%d")
    envs = state.setdefault("v2_env", {})
    env = envs.get(code)
    if env and env.get("date") == td:
        return env
    p = q["price"]
    prev_close = ma20 = atr = None
    try:
        bars = quotes.fetch_kline_bars(code, 101, 80) or []   # 日K(日级缓存)
        past = [b for b in bars if b["dt"][:10] < td]
        if len(past) >= 21:
            closes = [b["close"] for b in past[-20:]]
            ma20 = sum(closes) / 20
            prev_close = past[-1]["close"]
            trs = [max(past[i]["high"] - past[i]["low"],
                       abs(past[i]["high"] - past[i - 1]["close"]),
                       abs(past[i]["low"] - past[i - 1]["close"]))
                   for i in range(-14, 0)]
            atr = sum(trs) / 14
    except Exception as e:
        print(f"[v2环境] {code} 日K获取失败(间距/弱势判定降级):", e)
    op = None
    try:
        d1 = vote.bars_of_day(quotes.get_bars_1m(code) or [], td)
        if d1 and d1[0].get("open"):
            op = d1[0]["open"]
    except Exception:
        pass
    gap = (op / prev_close - 1) if (op and prev_close) else (q.get("pct") or 0) / 100.0
    weak = bool(prev_close and ma20 and prev_close < ma20)
    plan_sprd = plan.get("min_spread") or max(0.15, (atr or 0) * config.TP_SPRD_A)
    env = {
        "date": td, "gap": gap, "weak": weak,
        "flat": abs(gap) < config.V2_GAP_FLAT,
        "one_way_dn": gap <= config.V2_ONE_WAY_GAP or (weak and gap <= 0),
        "sprd": max(plan_sprd * config.V2_GRID_SPRD_A,
                    p * config.V2_GRID_MIN_PCT) if plan_sprd else 0.0,
        "prev_close": prev_close, "ma20": ma20, "atr": atr,
    }
    envs[code] = env
    tags = []
    if env["flat"]:
        tags.append("平开弃做")
    if env["one_way_dn"]:
        tags.append("单边下跌禁正T")
    print(f"[v2环境] {plan.get('name', code)} 竞价gap={gap * 100:+.2f}% "
          f"{' '.join(tags)} 网格间距{env['sprd']:.3f}")
    return env


# ---------------- 推送 ----------------
def _tail(state):
    return account.today_t_summary_html(state)


def _notify_sell_open(state, code, name, q, leg, env, dry):
    if dry:
        return
    p, pct = q["price"], q.get("pct", 0) or 0
    levels = " / ".join(account.fmt_price(leg["sell_price"] - k * env["sprd"])
                        for k in config.V2_GRID_K)
    notify.send(
        f"🎯 v2反T卖出·已成交 {name}",
        "<br>".join([
            _head(name, code, p, pct),
            notify.colored(notify.bold(f"📤 卖出{leg['qty0']}股@"
                                       f"{account.fmt_price(leg['sell_price'])}"),
                           config.C_GREEN),
            f"📐 网格回补计划(各1/3): {levels}",
            notify.colored(f"🌙 {config.V2_CLOSE_EOD}未回补部分现价兜底闭合",
                           config.C_GRAY),
            _tail(state)]))


def _notify_rebuy(state, code, name, q, leg, gi, rec, env, dry):
    if dry:
        return
    p, pct = q["price"], q.get("pct", 0) or 0
    done = sum(1 for f in leg["fills"] if f)
    dev = (leg["sell_price"] - rec["price"]) / leg["sell_price"] * 100
    notify.send(
        f"🔄 v2网格回补{gi + 1}档·已成交 {name}",
        "<br>".join([
            _head(name, code, p, pct),
            notify.colored(notify.bold(f"📥 买入{rec['qty']}股@"
                                       f"{account.fmt_price(rec['price'])} "
                                       f"(低于卖价{dev:.2f}%)"), config.C_RED),
            f"📊 本腿已回补{done}/{len(leg['fills'])}档, "
            f"剩余{leg['qty']}股未回补",
            (notify.colored(f"🌙 剩余部分{config.V2_CLOSE_EOD}兜底闭合",
                            config.C_ORANGE) if leg["qty"] > 0 else ""),
            _tail(state)]))


def _notify_pt(state, code, name, q, carry, dev, dry):
    if dry:
        return
    p, pct = q["price"], q.get("pct", 0) or 0
    notify.send(
        f"🚀 v2正T买入·已成交 {name}",
        "<br>".join([
            _head(name, code, p, pct),
            notify.colored(notify.bold(f"📥 买入{carry['qty']}股@"
                                       f"{account.fmt_price(carry['price'])}"),
                           config.C_RED),
            f"📉 VWAP乖离{dev * 100:+.1f}%(深于-{config.V2_VWAP_DEV * 100:.0f}%), "
            f"超跌企稳确认(3根5分钟不创新低+收阳)",
            notify.colored("🌅 T+1: 次日开盘出场(隔日T, 含隔夜风险)", config.C_ORANGE),
            _tail(state)]))


def _notify_carry_exit(state, code, name, q, rec, carry, dry):
    if dry:
        return
    p, pct = q["price"], q.get("pct", 0) or 0
    pnl = rec["qty"] * (rec["price"] - carry["price"])
    txt = (notify.colored(notify.bold(f"{'🟢盈利' if pnl > 0 else '🔴亏损'}"
                                      f"{account.fmt_pnl(pnl)}(未计费)"),
                          config.C_RED if pnl > 0 else config.C_GREEN))
    notify.send(
        f"🌅 v2隔日T出场·已卖出 {name}",
        "<br>".join([
            _head(name, code, p, pct),
            f"📤 卖出{rec['qty']}股@{account.fmt_price(rec['price'])} "
            f"(昨买{account.fmt_price(carry['price'])})",
            txt, _tail(state)]))


def _notify_fallback(state, code, name, q, rec, leg, dry):
    if dry:
        return
    p, pct = q["price"], q.get("pct", 0) or 0
    dev = (leg["sell_price"] - rec["price"]) / leg["sell_price"] * 100
    notify.send(
        f"🌙 v2收盘兜底回补·已闭合 {name}",
        "<br>".join([
            _head(name, code, p, pct),
            f"📥 兜底买入{rec['qty']}股@{account.fmt_price(rec['price'])} "
            f"(卖价{account.fmt_price(leg['sell_price'])}, "
            f"{'低于卖价' if dev > 0 else '高于卖价(冲高回落失败)'}{abs(dev):.2f}%)",
            notify.colored("✅ 反T腿已全部闭合, 持仓还原", config.C_RED),
            _tail(state)]))


def _notify_stop_alert(state, code, name, q, line, dry):
    if dry:
        return
    p, pct = q["price"], q.get("pct", 0) or 0
    notify.send(
        f"⚠️ v2止损告警(仅提醒) {name}",
        "<br>".join([
            _head(name, code, p, pct),
            notify.colored(notify.bold(f"🚑 已破止损线 {account.fmt_price(line)}"),
                           config.C_GREEN),
            notify.colored("v2策略不清仓: 反T网格回补+兜底即T腿风控, 底仓不动",
                           config.C_GRAY),
            notify.colored("(需恢复清仓保护请将STRAT_V2_EN置False)", config.C_GRAY),
            _tail(state)]))


# ---------------- 主入口(被strategy.check_signal在STRAT_V2_EN时调用) ----------------
def v2_check(state, code, plan, q, now, dry=False, regime=None):
    """v2策略单股单轮检查; 完全接管该股本轮T动作(v1逻辑不再执行)"""
    td = now.strftime("%Y-%m-%d")
    pos = state["positions"].setdefault(
        code, {"name": plan.get("name", code), "shares": 0, "cost": 0.0})
    name = plan.get("name") or pos.get("name", code)
    p = q["price"]
    shares = pos["shares"]
    lot = account.lot_of(state, code, shares, plan.get("t_qty"))
    if "sell_low" not in plan and "buy_low" not in plan:
        return True                                  # 无T区计划, 无事可做

    env = _env_of(state, code, plan, q, now)
    td_bars = vote.bars_of_day(quotes.get_bars_1m(code) or [], td)
    vwap, _ = vote.calc_vwap(td_bars) if td_bars else (None, None)
    bars5 = vote.agg_bars(td_bars, 5) if td_bars else []
    late = now.time() >= _hm(config.V2_LATE_GATE)
    eod = now.time() >= _hm(config.V2_CLOSE_EOD)

    # ── ⓪ 昨日正T腿: 开盘出场(T+1已解禁, rollover已清today_bought) ──
    carry = state.setdefault("v2_carry", {})
    leg_c = carry.get(code)
    if leg_c and leg_c.get("date") != td:
        carry.pop(code, None)                        # 先摘除防出场失败后重复进入
        qty = min(_std_lot(leg_c.get("qty", 0)), _std_lot(shares))
        if qty >= config.LOT_MIN:
            exit_px = (td_bars[0]["open"] if td_bars and td_bars[0].get("open") else p)
            rec = _do_sell(state, code, name, qty, exit_px,
                           note=f"隔日T出场(昨买{account.fmt_price(leg_c['price'])})",
                           tags="v2隔日T", dry=dry)
            if rec:
                rec["yday_buy"] = leg_c["price"]   # 昨买价: 日报T腿配对明细用(隔日独立配对)
                # 昨买腿注入今日台账(2026-11-10): 否则今日总览把隔日T当"多卖未回补",
                # 隔日利润进不了今日金额, 与累计净T口径对不上
                account.inject_carry_buy(state, code, rec["qty"], leg_c["price"], dry)
                _notify_carry_exit(state, code, name, q, rec, leg_c, dry)
        else:
            print(f"[v2] {name} 隔日T腿{leg_c.get('qty')}股可卖不足, 顺延至下轮")

    # ── 止损线告警(仅提醒不清仓, 防重: 破线告警一次, 收复1%解除) ──
    stops = plan.get("stop_loss") or []
    if stops:
        line = max(stops)
        skey = f"{code}|v2stop"
        if p <= line:
            if skey not in state["armed"]:
                state["armed"].append(skey)
                print(f"[v2止损告警] {name} {account.fmt_price(p)} 破止损线"
                      f"{account.fmt_price(line)}(仅提醒, 不清仓)")
                _notify_stop_alert(state, code, name, q, line, dry)
        elif p >= line * 1.01 and skey in state["armed"]:
            state["armed"].remove(skey)

    legs = state.setdefault("v2_legs", {})
    leg = legs.get(code)
    if leg and leg.get("date") != td:
        leg = None                                   # 隔日残留兜底(rollover应已清)
    n_legs = (leg or {}).get("n_legs", 0)
    avail = _std_lot(shares - (pos.get("today_bought", 0)
                               if config.T_PLUS_ONE else 0))

    # ── ② 网格回补(核心动作, 优先于开新腿): 未回补卖出腿分3档接回 ──
    if leg and leg.get("qty", 0) > 0 and env["sprd"] > 0:
        per = max(_std_lot(leg["qty0"] // 3), config.LOT_MIN) \
            if leg["qty0"] >= 300 else leg["qty0"]
        for gi, k in enumerate(config.V2_GRID_K):
            if leg["fills"][gi] or leg["qty"] <= 0:
                continue
            target = leg["sell_price"] - k * env["sprd"]
            if p <= target:
                want = min(per, leg["qty"])
                rec = _do_buy(state, code, name, want, p,
                              note=f"网格回补{gi + 1}档"
                                   f"(目标{account.fmt_price(target)})",
                              tags=f"v2回补{gi + 1}", dry=dry)
                if rec or dry:
                    got = rec["qty"] if rec else want
                    leg["fills"][gi] = True
                    leg["qty"] = max(leg["qty"] - got, 0)
                    print(f"[v2回补] {name} {gi + 1}档 {got}股@"
                          f"{account.fmt_price(p)} 剩余{leg['qty']}股未回补")
                    _notify_rebuy(state, code, name, q, leg, gi,
                                  rec or {"qty": got, "price": p}, env, dry)

    # ── ④ 收盘兜底(反T闭合纪律): 14:45未回补卖出腿按现价买回 ──
    if eod and leg and leg.get("qty", 0) > 0:
        rec = _do_buy(state, code, name, leg["qty"], p,
                      note=f"收盘兜底回补({config.V2_CLOSE_EOD}反T闭合纪律)",
                      tags="v2兜底", dry=dry)
        if rec:
            leg["qty"] = max(leg["qty"] - rec["qty"], 0)
            print(f"[v2兜底] {name} 买回{rec['qty']}股@"
                  f"{account.fmt_price(p)} 腿剩余{leg['qty']}股")
            _notify_fallback(state, code, name, q, rec, leg, dry)
        elif not dry:
            print(f"[v2兜底] {name} 现金不足买不回{leg['qty']}股, 下轮重试")

    # ── ① 开卖出腿(反T): 高抛区 或 VWAP上乖离≥2%且5分钟收阳 ──
    hot = ("sell_low" in plan and p >= plan["sell_low"])
    if not hot and vwap and bars5:
        hot = (p / vwap - 1 >= config.V2_VWAP_DEV
               and bars5[-1]["close"] > bars5[-1]["open"])
    if (hot and not late and not eod and not env["flat"]
            and (not leg or leg.get("qty", 0) <= 0)     # 单活跃腿: 未闭合不开新腿
            and n_legs < config.V2_MAX_SELL
            and avail >= config.LOT_MIN and lot >= config.LOT_MIN
            and _cool(state, code, "sell", now)):
        qty = min(lot, avail)
        levels = " / ".join(account.fmt_price(p - k * env["sprd"])
                            for k in config.V2_GRID_K)
        rec = _do_sell(state, code, name, qty, p,
                       note=f"反T卖出(网格回补计划: {levels})", tags="v2反T卖",
                       dry=dry)
        if rec or dry:                               # dry也建腿(便于观察后续回补计划)
            leg = {"date": td, "name": name, "sell_price": p, "qty": qty,
                   "qty0": qty, "fills": [False] * 3, "n_legs": n_legs + 1}
            legs[code] = leg
            state.setdefault("v2_last_open", {})[f"{code}|sell"] = \
                now.strftime("%H:%M:%S")
            print(f"[v2反T] {name} 卖出{qty}股@{account.fmt_price(p)} "
                  f"开腿(n_legs={leg['n_legs']}), 网格回补[{levels}]")
            _notify_sell_open(state, code, name, q, leg, env, dry)

    # ── ③ 正T(超跌反弹确认): 非单边下跌/平开日, VWAP深乖离+企稳收阳, 隔日出 ──
    pt_today = bool(carry.get(code) and carry[code].get("date") == td)
    dev = (p / vwap - 1) if vwap else 0.0
    stable = (len(bars5) < 6
              or min(b["low"] for b in bars5[-3:])
              >= min(b["low"] for b in bars5[-6:-3]))
    bull = bool(bars5 and bars5[-1]["close"] > bars5[-1]["open"])
    if ("buy_low" in plan and not late and not eod
            and not env["flat"] and not env["one_way_dn"] and not pt_today
            and vwap and dev <= -config.V2_VWAP_DEV and stable and bull
            and lot >= config.LOT_MIN and _cool(state, code, "buy", now)):
        rec = _do_buy(state, code, name, lot, p,
                      note=f"正T(超跌企稳: VWAP乖离{dev * 100:+.1f}%, 隔日开盘出场)",
                      tags="v2正T", dry=dry)
        if rec or dry:
            got = rec["qty"] if rec else lot
            carry[code] = {"date": td, "name": name, "price": p, "qty": got}
            state.setdefault("v2_last_open", {})[f"{code}|buy"] = \
                now.strftime("%H:%M:%S")
            print(f"[v2正T] {name} 买入{got}股@{account.fmt_price(p)} "
                  f"(乖离{dev * 100:+.1f}%), 次日开盘出场")
            _notify_pt(state, code, name, q, carry[code], dev, dry)
    return True
