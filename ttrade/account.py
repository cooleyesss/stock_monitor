# -*- coding: utf-8 -*-
"""模拟盘账户: 成交/做T台账/估值/收盘报告数据(费用模型在common.fees)"""
import datetime
from ttrade import config_ttrade as config
from common import textparse
from ttrade import state as tstate
from common import notify
from common.fees import is_etf, buy_fee, sell_fee
from ttrade.ledger import t_pnl, t_pnl_detail
from ttrade import vote

TRADE_HEADERS = ["日期", "时间", "代码", "名称", "方向", "股数",
                 "价格", "成交额", "费用", "现金余额", "备注", "策略"]

SIDE_LABEL = {
    "buy": "🚀低吸买入", "sell": "🎯高抛卖出",
    "stop_clear": "⚠️止损清仓", "stop_t": "⚠️止损卖出T仓",
    "manual_sell": "📋手动卖出(同步真实操作)",
}


def fmt_price(p):
    s = f"{p:.2f}"
    return s if float(s) == round(p, 6) else f"{p:.3f}"


def fmt_amt(v):
    return f"{v:,.2f}"


def fmt_pnl(v):
    return ("+" if v > 0 else "") + f"{v:,.2f}"


def _w(s, n):
    """按显示宽度补空格对齐(CJK字符占2列)"""
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    return str(s) + " " * max(n - w, 0)


# ---------------- 费用 ----------------
# is_etf/buy_fee/sell_fee 已统一迁移至 common.fees, 此处经import保持
# account.buy_fee/account.sell_fee 为 common.fees 的转发别名(费用口径唯一在 common/fees)


# ---------------- 做T台账 ----------------
def _led(state, code, cum=False):
    return state["cum_ledger" if cum else "today_ledger"].setdefault(
        code, {"bq": 0, "ba": 0.0, "sq": 0, "sa": 0.0})


# t_pnl / t_pnl_detail 已抽到 ttrade/ledger.py(纯函数, 供 state.py 共用), 顶部 import 重导出
# 以保持 account.t_pnl / account.t_pnl_detail 调用兼容(T 看板等历史调用点)


def _is_carry_trade(t):
    """隔日T成交判定: v2隔日T出场(昨买→今卖, 带昨买价yday_buy)"""
    return (t.get("strat") == "v2隔日T" and t.get("yday_buy")
            and t.get("side") == "sell")


def inject_carry_buy(state, code, qty, yday_price, dry=False):
    """隔日T的昨买腿注入今日台账(2026-11-10用户需求: 隔日T须计入今日做T总览)
    隔日T出场时今日台账只有卖出腿, 会被误当"多卖未回补"浮动, 隔日利润进不了今日金额,
    与账户概况的累计净T盈亏(cum口径, 昨买今卖正确配对)对不上。
    注入昨买腿后今日台账 bq/sq 配对 -> 已回补 = qty*(今卖-昨买), 两边口径一致。
    只注 today_ledger; cum_ledger 昨日已记买入腿, 再注会重复计算。"""
    led = _led(state, code)
    led["bq"] += qty
    led["ba"] = round(led["ba"] + qty * yday_price, 2)
    tstate.save_state(state, dry)


def fifo_pairs(trades):
    """当日做T交易FIFO配对: 同代码反向腿依次对冲(先买后卖/先卖后买都支持)
    每对盈亏 = 股数*(卖价-买价) - 双边费用(按配对股数分摊), 归因于卖腿策略tag
    返回 (pairs, opens):
      pairs=[{code,name,qty,buy_price,sell_price,pnl,tag,time}]  已回补对子
      opens=[{code,name,side,qty,price,fps,tag,time}]            未回补开仓腿(fps=每股费用)
    注: 止损/手动卖出(side非buy/sell)不入配对;
        隔日T(昨买→今卖)按昨买价独立闭合, 不进当日FIFO队列(与t_leg_pairs同口径),
        否则今日兜底买入会错误配对隔日T卖腿"""
    pairs, opens = [], []
    queues = {}   # code -> 未回补腿队列(按时间)
    for t in trades:
        if t.get("side") not in ("buy", "sell"):
            continue
        if _is_carry_trade(t):
            pairs.append({"code": t["code"], "name": t.get("name", t["code"]),
                          "qty": t["qty"], "buy_price": t["yday_buy"],
                          "sell_price": t["price"],
                          "pnl": round(t["qty"] * (t["price"] - t["yday_buy"])
                                       - (t.get("fee", 0.0) or 0.0), 2),
                          "tag": t["strat"], "time": t.get("time", "")})
            continue
        leg = {"code": t["code"], "name": t.get("name", t["code"]),
               "side": t["side"], "qty": t["qty"], "price": t["price"],
               "fps": (t.get("fee", 0.0) or 0.0) / t["qty"] if t["qty"] else 0.0,
               "tag": t.get("strat", ""), "time": t.get("time", "")}
        q = queues.setdefault(t["code"], [])
        while leg["qty"] > 0 and q and q[0]["side"] != leg["side"]:
            head = q[0]
            m = min(leg["qty"], head["qty"])
            buy_leg, sell_leg = (head, leg) if leg["side"] == "sell" else (leg, head)
            pnl = m * (sell_leg["price"] - buy_leg["price"]) \
                  - (buy_leg["fps"] + sell_leg["fps"]) * m
            pairs.append({"code": t["code"], "name": leg["name"], "qty": m,
                          "buy_price": buy_leg["price"],
                          "sell_price": sell_leg["price"],
                          "pnl": round(pnl, 2), "tag": sell_leg["tag"],
                          "time": sell_leg["time"]})
            head["qty"] -= m
            leg["qty"] -= m
            if head["qty"] <= 0:
                q.pop(0)
        if leg["qty"] > 0:
            q.append(leg)
    for q in queues.values():
        opens.extend(q)
    return pairs, opens


def basis_cost(pos):
    """整体累计成本基准(2026-11-03用户需求): 清仓再接回/做T摊薄都不重置此基准,
    浮动盈亏按它计算; pos["cost"]仍为模拟盘摊薄成本(随交易变动)"""
    return pos.get("orig_cost") or pos.get("cost") or 0.0


def t_leg_pairs(trades, px):
    """今日T腿配对(2026-11-03用户需求): 隔日T(strat=v2隔日T且带昨买价yday_buy)
    独立配对"昨买→今卖"; 其余按当日FIFO。
    返回 (info, rows):
      info: {id(成交记录): {"bp":[买价], "sp":[卖价]}} 每笔成交的配对价(可能多对)
      rows: [(名称, 类型, 买价, 卖价, 股数, 盈亏, 状态)] 状态附原因说明
    买/卖价已格式化(无该侧为'—'), 未平腿按最新价px浮动估值"""
    carry, rest = [], []
    for t in trades:
        if t.get("side") not in ("buy", "sell"):
            continue
        (carry if (t.get("strat") == "v2隔日T" and t.get("yday_buy"))
         else rest).append(t)
    info, rows = {}, []

    def _acc(t, key, val):
        info.setdefault(id(t), {"bp": [], "sp": []})[key].append(val)

    for t in carry:
        pnl = t["qty"] * (t["price"] - t["yday_buy"]) - (t.get("fee") or 0.0)
        rows.append((t["name"], "隔日T", fmt_price(t["yday_buy"]),
                     fmt_price(t["price"]), t["qty"], round(pnl, 2),
                     "已闭合(隔日T: 昨低吸→今开盘卖出)", t["time"][:5]))
        _acc(t, "bp", fmt_price(t["yday_buy"]))
        _acc(t, "sp", fmt_price(t["price"]))

    # 当日FIFO(同fifo_pairs逻辑, 但保留成交记录引用以便逐笔映射)
    queues = {}   # code -> 未回补腿队列
    for t in rest:
        leg = {"t": t, "side": t["side"], "qty": t["qty"], "price": t["price"],
               "fps": (t.get("fee", 0.0) or 0.0) / t["qty"] if t["qty"] else 0.0,
               "tag": t.get("strat", "")}
        q = queues.setdefault(t["code"], [])
        while leg["qty"] > 0 and q and q[0]["side"] != leg["side"]:
            head = q[0]
            m = min(leg["qty"], head["qty"])
            buy_leg, sell_leg = (head, leg) if leg["side"] == "sell" else (leg, head)
            pnl = m * (sell_leg["price"] - buy_leg["price"]) \
                  - (buy_leg["fps"] + sell_leg["fps"]) * m
            rows.append((leg["t"]["name"], buy_leg["tag"] or "日内T",
                         fmt_price(buy_leg["price"]), fmt_price(sell_leg["price"]),
                         m, round(pnl, 2), "已闭合(当日反向腿FIFO对冲平仓)",
                         sell_leg["t"]["time"][:5]))
            _acc(buy_leg["t"], "bp", fmt_price(buy_leg["price"]))
            _acc(buy_leg["t"], "sp", fmt_price(sell_leg["price"]))
            _acc(sell_leg["t"], "bp", fmt_price(buy_leg["price"]))
            _acc(sell_leg["t"], "sp", fmt_price(sell_leg["price"]))
            head["qty"] -= m
            leg["qty"] -= m
            if head["qty"] <= 0:
                q.pop(0)
        if leg["qty"] > 0:
            q.append(leg)
    for q in queues.values():
        for leg in q:
            last = px.get(leg["t"]["code"], leg["price"])
            if leg["side"] == "buy":
                pnl = leg["qty"] * (last - leg["price"]) - leg["fps"] * leg["qty"]
                rows.append((leg["t"]["name"], leg["tag"] or "开仓",
                             fmt_price(leg["price"]), "—", leg["qty"],
                             round(pnl, 2),
                             "未平(T仓): 只买未卖, 等高抛止盈, 按最新价浮动",
                             leg["t"]["time"][:5]))
                _acc(leg["t"], "bp", fmt_price(leg["price"]))
            else:
                pnl = leg["qty"] * (leg["price"] - last) - leg["fps"] * leg["qty"]
                rows.append((leg["t"]["name"], leg["tag"] or "开仓", "—",
                             fmt_price(leg["price"]), leg["qty"], round(pnl, 2),
                             "未回补: 只卖未买, 等低吸回补(14:45收盘兜底), 按最新价浮动",
                             leg["t"]["time"][:5]))
                _acc(leg["t"], "sp", fmt_price(leg["price"]))
    return info, rows


def t_leg_details(trades, px):
    """今日T腿配对明细: 见t_leg_pairs; 返回rows供报告表格展示"""
    return t_leg_pairs(trades, px)[1]


def lot_of(state, code, shares, t_qty=None):
    """当日做T股数: t_plan的T量 > LOT_OVERRIDE > 持仓*LOT_RATIO, 百股取整, 当日缓存"""
    if code not in state["today_lot"]:
        lot = t_qty or config.LOT_OVERRIDE.get(code)
        if lot is None:
            lot = int(shares * config.LOT_RATIO // 100 * 100)
        state["today_lot"][code] = max(int(lot), 0)
    return state["today_lot"][code]


# ---------------- 估值 ----------------
def pos_price(state, key, prices):
    """某持仓现价: 实时价优先, ETF映射/静态价, 再用最近行情价兜底, 最后成本价"""
    mapped = config.ETF_CODE_MAP.get(key)
    if mapped and mapped in prices:
        return prices[mapped]
    if key in prices:
        return prices[key]
    pos = state["positions"][key]
    if pos.get("static_price"):
        return pos["static_price"]
    lp = state.get("last_prices", {}).get(key)
    if lp:
        return lp
    return pos.get("cost") or 0.0


def total_asset(state, prices):
    return state["cash"] + sum(
        pos["shares"] * pos_price(state, key, prices)
        for key, pos in state["positions"].items())


# ---------------- 成交执行(不推送, 由strategy组装统一推送) ----------------
def affordable_qty(state, code, price, want_qty):
    """按可用现金缩减买入股数(百股取整, 含费用); 连1手都买不起返回0"""
    qty = want_qty // config.LOT_MIN * config.LOT_MIN
    while qty >= config.LOT_MIN:
        amount = round(qty * price, 2)
        if state["cash"] >= amount + buy_fee(amount, code):
            return qty
        qty -= config.LOT_MIN
    return 0


def execute_buy(state, code, name, qty, price, note="", ai_txt="", dry=False,
                strategy_tags=""):
    amount = round(qty * price, 2)
    fee = buy_fee(amount, code)
    cash_before = state["cash"]
    state["cash"] = round(state["cash"] - amount - fee, 2)
    pos = state["positions"].setdefault(code, {"name": name, "shares": 0, "cost": 0.0})
    oc = (state.get("cleared_cost") or {}).pop(code, None)   # 清仓再接回: 恢复整体累计基准
    if oc and not pos.get("orig_cost"):
        pos["orig_cost"] = oc
        print(f"[基准成本] {name} 清仓后重新接回, 恢复累计基准成本{oc}")
    old = pos["shares"]
    pos["shares"] = old + qty
    pos["cost"] = round((pos["cost"] * old + amount + fee) / pos["shares"], 4)
    if not pos.get("orig_cost") and old == 0:
        pos["orig_cost"] = pos["cost"]       # 新品种: 首次买入均价为累计基准
        # 已有持仓缺基准时不在此赋值(防摊薄成本误标), 留给load_state迁移播种
    pos["today_bought"] = pos.get("today_bought", 0) + qty   # T+1: 当日买入不可卖
    led = _led(state, code)
    led["bq"] += qty
    led["ba"] = round(led["ba"] + amount, 2)
    cled = _led(state, code, cum=True)
    cled["bq"] += qty
    cled["ba"] = round(cled["ba"] + amount, 2)
    state["today_fee"] = round(state["today_fee"] + fee, 2)
    state["cum_fee"] = round(state["cum_fee"] + fee, 2)
    rec = _record(state, code, name, "buy", qty, price, amount, fee,
                  note, ai_txt, dry, strategy_tags)
    rec.update({"shares_before": old, "shares_after": pos["shares"],
                "cash_before": cash_before, "cash_after": state["cash"]})
    _write_trade_log(rec, dry)
    print(f"[成交] {SIDE_LABEL['buy']} {name} {qty}股@{fmt_price(price)} "
          f"金额{fmt_amt(amount)} 费{fmt_amt(fee)} "
          f"持股{old}→{pos['shares']} 现金{fmt_amt(cash_before)}→{fmt_amt(state['cash'])}")
    return rec


def execute_sell(state, code, name, qty, price, side="sell", note="",
                 in_ledger=True, ai_txt="", dry=False, enforce_t1=None,
                 strategy_tags=""):
    """enforce_t1: None=按config.T_PLUS_ONE; True/False强制开关
    (做T高抛受限; 止损清仓/手动同步真实操作由strategy层传False豁免)"""
    pos = state["positions"].get(code)
    if not pos:
        print(f"[卖出失败] {name} 无持仓")
        return None
    if enforce_t1 is None:
        enforce_t1 = config.T_PLUS_ONE
    if enforce_t1 and side in ("sell", "stop_t"):
        avail = pos["shares"] - pos.get("today_bought", 0)
        avail = avail // 100 * 100
        if avail < config.LOT_MIN:
            print(f"[T+1拦截] {name} 持股{pos['shares']}股, 当日买入"
                  f"{pos.get('today_bought', 0)}股不可卖, 可卖不足{config.LOT_MIN}股")
            return None
        if qty > avail:
            print(f"[T+1调整] {name} 计划卖{qty}股 → 可卖{avail}股"
                  f"(当日买入{pos.get('today_bought', 0)}股T+1不可卖)")
            qty = avail
    amount = round(qty * price, 2)
    fee = sell_fee(amount, code)
    cash_before = state["cash"]
    state["cash"] = round(state["cash"] + amount - fee, 2)
    old = pos["shares"]
    pos["shares"] -= qty
    if pos["shares"] == 0:
        # 清仓: 暂存整体累计成本基准(2026-11-03), 再接回时恢复, 浮动盈亏口径不断档
        if pos.get("orig_cost"):
            state.setdefault("cleared_cost", {})[code] = pos["orig_cost"]
        pos["cost"] = 0.0
        pos["today_bought"] = 0
        state["positions"].pop(code, None)   # 清仓删除持仓
    # 卖出的全是"昨仓"(T+1可卖部分), today_bought保持不变, 次日rollover清零
    if in_ledger:
        led = _led(state, code)
        led["sq"] += qty
        led["sa"] = round(led["sa"] + amount, 2)
        cled = _led(state, code, cum=True)
        cled["sq"] += qty
        cled["sa"] = round(cled["sa"] + amount, 2)
    state["today_fee"] = round(state["today_fee"] + fee, 2)
    state["cum_fee"] = round(state["cum_fee"] + fee, 2)
    rec = _record(state, code, name, side, qty, price, amount, fee,
                  note, ai_txt, dry, strategy_tags)
    rec.update({"shares_before": old, "shares_after": pos["shares"],
                "cash_before": cash_before, "cash_after": state["cash"]})
    _write_trade_log(rec, dry)
    print(f"[成交] {SIDE_LABEL[side]} {name} {qty}股@{fmt_price(price)} "
          f"金额{fmt_amt(amount)} 费{fmt_amt(fee)} "
          f"持股{old}→{pos['shares']} 现金{fmt_amt(cash_before)}→{fmt_amt(state['cash'])}")
    return rec


def _record(state, code, name, side, qty, price, amount, fee, note, ai_txt, dry,
            strat=""):
    now = datetime.datetime.now()
    rec = {"date": textparse.today_str(now), "time": now.strftime("%H:%M:%S"),
           "code": code, "name": name, "side": side,
           "side_txt": SIDE_LABEL[side], "qty": qty, "price": price,
           "amount": amount, "fee": fee, "cash_after": state["cash"],
           "note": note, "ai": ai_txt, "strat": strat}
    if not dry:
        state["today_trades"].append(rec)
        tstate.append_trades_csv(
            [rec["date"], rec["time"], code, name, SIDE_LABEL[side], qty,
             fmt_price(price), amount, fee, state["cash"],
             (note + (" | AI:" + ai_txt if ai_txt else "")), strat],
            TRADE_HEADERS, dry)
        tstate.save_state(state)
    return rec


def _write_trade_log(rec, dry=False):
    """成交写入可读日志 trades.log"""
    if dry:
        return
    ai = f" | {rec['ai']}" if rec.get("ai") else ""
    note = f" | {rec['note']}" if rec.get("note") else ""
    strat = f" | 票:{rec['strat']}" if rec.get("strat") else ""
    line = (f"[{rec['date']} {rec['time']}] {rec['side_txt']} "
            f"{rec['name']}({rec['code']}) {rec['qty']}股@{fmt_price(rec['price'])} "
            f"成交{fmt_amt(rec['amount'])} 费{fmt_amt(rec['fee'])}"
            f" | 持股{rec['shares_before']}→{rec['shares_after']} "
            f"现金{fmt_amt(rec['cash_after'])}{note}{ai}{strat}")
    tstate.append_trade_log(line)


# ---------------- 当日T汇总 ----------------
def today_t_summary(state):
    """返回 (笔数, 毛利, 费用, 净盈亏)"""
    n = sum(1 for t in state["today_trades"] if t["side"] in ("buy", "sell"))
    gross = 0.0
    for c, led in state["today_ledger"].items():
        if led["bq"] == 0 and led["sq"] == 0:
            continue
        gross += t_pnl(led, state.get("last_prices", {}).get(c, 0))
    fee = state["today_fee"]
    return n, gross, fee, gross - fee


def today_t_summary_html(state):
    n, gross, fee, net = today_t_summary(state)
    return (notify.sec("📈 今日T汇总") + notify.table(
        ["已T笔数", "做T毛利", "费用", "当日T净盈亏"],
        [[str(n), fmt_pnl(gross), fmt_amt(fee),
          notify.colored(notify.bold(fmt_pnl(net)), notify.pnl_color(net))]]))


# ---------------- 收盘报告 ----------------
def build_eod_summary(state, prices, now):
    """生成收盘报告, 返回 (title, html, plain, summary_dict)"""
    td = state["today"]
    trades = state["today_trades"]

    # ---- 数据收集(plain与html共用) ----
    gross_today = realized_today = floating_today = 0.0
    t_rows = []       # (名称, 已回补T, 未回补浮动, 合计, 未回补说明)
    t_map = {}        # code -> 同上行(总分结构用)
    for code, led in state["today_ledger"].items():
        if led["bq"] == 0 and led["sq"] == 0:
            continue
        price = prices.get(code, state.get("last_prices", {}).get(code, 0))
        pnl = t_pnl(led, price)
        realized, floating = t_pnl_detail(led, price)
        gross_today += pnl
        realized_today += realized
        floating_today += floating
        net = led["bq"] - led["sq"]
        extra = f"{'多买' if net > 0 else '多卖'}{abs(net)}股未回补" if net else ""
        name = state["positions"].get(code, {}).get("name", code)
        t_rows.append((name, realized, floating, pnl, extra))
        t_map[code] = t_rows[-1]
    # 个股分组顺序(总分): 按首笔成交先后; 台账有活动但无成交的股附后
    grp_order = []
    for t in trades:
        if t["code"] not in grp_order:
            grp_order.append(t["code"])
    for c in state["today_ledger"]:
        led = state["today_ledger"][c]
        if (led["bq"] or led["sq"]) and c not in grp_order:
            grp_order.append(c)
    fee_today = state["today_fee"]
    net_today = gross_today - fee_today

    a = state.get("today_ai", {})
    ai_line = ""
    if a.get("asked"):
        ai_line = (f"🤖AI研判: {a['asked']}次 (建议执行{a['execute']}/"
                   f"建议观望{a['wait']}/失败{a['fail']})")

    gross_cum = sum(t_pnl(l, prices.get(c, state.get("last_prices", {}).get(c, 0)))
                    for c, l in state["cum_ledger"].items())
    net_cum = gross_cum - state["cum_fee"]

    asset = total_asset(state, prices)
    base = state.get("baseline") or {}
    acct_pnl = asset - base["total_asset"] if base else 0.0
    # 昨日环比(2026-10-30用户需求): 取最近一个有总资产的往日收盘
    # (日报构建时daily_history尚无今日记录, summary由调用方事后append)
    prev_asset = None
    for d in reversed(state.get("daily_history") or []):
        if d.get("total_asset") and d.get("date", "") < td:
            prev_asset = d["total_asset"]
            break
    day_chg = (asset - prev_asset) if prev_asset else None

    pos_rows = []     # (名称, 持股, 现价, 成本, 今日T盈亏, 今日T率, 浮动盈亏, 备注)
    for key, pos in state["positions"].items():
        price = pos_price(state, key, prices)
        bc = basis_cost(pos)                          # 整体累计成本基准(清仓接回不重置)
        fl = pos["shares"] * (price - bc)
        tag = "静态价" if key not in prices and not config.ETF_CODE_MAP.get(key) else ""
        led = state["today_ledger"].get(key)          # 单股今日T盈亏(2026-10-30用户需求)
        if led and (led["bq"] or led["sq"]):
            t_v = t_pnl(led, price)
            t_base = led["ba"] if led["bq"] else led["sa"]
            t_pct = (t_v / t_base * 100) if t_base else None
        else:
            t_v, t_pct = None, None
        pos_rows.append((pos["name"], pos["shares"], price, bc,
                         t_v, t_pct, fl, tag))

    n_skip = len(state["today_skips"])

    # ---- 策略归因统计(多策略投票引擎, 启发式口径) ----
    vote_log = state.get("today_vote_log") or []
    s_pairs, s_opens = fifo_pairs(trades)
    lp_all = dict(prices)
    lp_all.update(state.get("last_prices", {}) or {})
    SKEYS = list(vote.VOTER_LABEL.keys()) + ["none"]   # 从投票器字典构建, 防新增投票器漏key致KeyError
    SLBL = dict(vote.VOTER_LABEL)
    SLBL["none"] = "无归因/旧策略"
    LBL2KEY = {lbl: k for k, lbl in SLBL.items()}
    st = {k: {"trig": 0, "exec": 0, "wait": 0, "realized": 0.0,
              "floating": 0.0, "win": 0, "pair": 0} for k in SKEYS}
    for v in vote_log:
        d = 1 if v.get("side") == "buy" else -1
        for lbl, sc in (v.get("votes") or {}).items():
            k = LBL2KEY.get(lbl)
            if k and sc * d >= config.QUANT_MIN_VOTE:
                st[k]["trig"] += 1
        k = vote.tag_key(v.get("tag", "")) or "none"
        if v.get("executed"):
            st[k]["exec"] += 1
        else:
            st[k]["wait"] += 1
    for pp in s_pairs:
        k = vote.tag_key(pp["tag"]) or "none"
        st[k]["realized"] += pp["pnl"]
        st[k]["pair"] += 1
        if pp["pnl"] > 0:
            st[k]["win"] += 1
    for o in s_opens:
        k = vote.tag_key(o["tag"]) or "none"
        px = lp_all.get(o["code"], o["price"])
        diff = (px - o["price"]) if o["side"] == "buy" else (o["price"] - px)
        st[k]["floating"] += o["qty"] * diff - o["fps"] * o["qty"]

    # 今日T腿配对明细(2026-11-03用户需求: 每笔T买卖价+持仓变化)
    leg_info, leg_rows = t_leg_pairs(trades, lp_all)

    def _st_active(s):
        return bool(s["trig"] or s["pair"] or s["exec"]
                    or s["wait"] or s["floating"])

    # ---- 纯文本版(日志/控制台) ----
    P = []
    P.append(f"── 今日做T总览 ({len(trades)}笔, 总分) ──")
    if not grp_order:
        P.append("今日无做T交易")
    for c in grp_order:
        nm, rz, fl2, pn, ex = t_map[c]
        P.append(f"{_w(nm, 9)}已回补T{fmt_pnl(rz)}  未回补浮动{fmt_pnl(fl2)}  "
                 f"小计{fmt_pnl(pn)}元" + (f"  ({ex})" if ex else ""))
        for t in [x for x in trades if x["code"] == c]:
            side_txt = t.get("side_txt") or SIDE_LABEL.get(t["side"], t["side"])
            sd = {"buy": "买入", "sell": "卖出"}.get(t["side"], side_txt)
            inf = leg_info.get(id(t))
            if inf:
                bp, sp = "/".join(inf["bp"]) or "—", "/".join(inf["sp"]) or "—"
            else:   # 非T腿成交(止损/手动同步): 显示自身成交价
                bp = fmt_price(t["price"]) if t["side"] == "buy" else "—"
                sp = fmt_price(t["price"]) if t["side"] != "buy" else "—"
            P.append(f"    {t['time'][:5]} {sd}{t['qty']}股 买{bp}→卖{sp} "
                     f"持仓{t.get('shares_before', '?')}→{t.get('shares_after', '?')}"
                     + (f" [{t['strat']}]" if t.get("strat") else ""))
    P.append(f"汇总: 已回补T{fmt_pnl(realized_today)} | 未回补浮动: "
             f"{fmt_pnl(floating_today)}元 | 费用: {fmt_amt(fee_today)}元 | "
             f"今日净盈亏: {fmt_pnl(net_today)}元")
    P.append("── 今日T腿配对(买价/卖价) ──")
    if leg_rows:
        for nm, tg, bp, sp, qy, pn, stt, _tm in leg_rows:
            P.append(f"{nm} [{tg}] 买{bp} 卖{sp} {qy}股 {fmt_pnl(pn)}元 ({stt})")
    else:
        P.append("无T腿")
    if ai_line:
        P.append(ai_line)
    P.append(f"── 累计做T盈亏(自{state['init_date']}起, 含未回补浮动) ──")
    P.append(f"累计净盈亏: {fmt_pnl(net_cum)}元 (累计费用{fmt_amt(state['cum_fee'])})")
    P.append("── 账户概况 ──")
    P.append(f"当前总资产: {fmt_amt(asset)}元 | 可用现金: {fmt_amt(state['cash'])}元")
    if day_chg is not None:
        P.append(f"较昨日收盘({prev_asset:,.2f}): {fmt_pnl(day_chg)}元 "
                 f"({day_chg / prev_asset * 100:+.2f}%)")
    if base:
        P.append(f"账户总盈亏(对比期初{fmt_amt(base['total_asset'])}): "
                 f"{fmt_pnl(acct_pnl)}元")
    P.append("── 持仓概览(浮动盈亏按基准成本·累计口径, 清仓接回不重置) ──")
    for nm, sh, pr, cs, tp, tpc, fl3, tg in pos_rows:
        t_txt = (f" 今日T{fmt_pnl(tp)}({tpc:+.2f}%)" if tp is not None else "")
        P.append(f"{_w(nm, 9)}{sh}股 现价{fmt_price(pr)} 市值{fmt_amt(sh * pr)} "
                 f"成本{fmt_price(cs)}{t_txt} 浮动{fmt_pnl(fl3)}元"
                 + (f" ({tg})" if tg else ""))
    P.append("── 策略统计(启发式口径) ──")
    any_st = False
    for k in SKEYS:
        s = st[k]
        if not _st_active(s):
            continue
        any_st = True
        wr = f"{s['win']}/{s['pair']}" if s["pair"] else "--"
        P.append(f"{SLBL[k]}: 触发{s['trig']} 执行{s['exec']} 观望{s['wait']} "
                 f"已回补{fmt_pnl(s['realized'])} 浮动{fmt_pnl(s['floating'])} 胜率{wr}")
    if not any_st:
        P.append("今日无量化信号")
    if n_skip:
        P.append(f"⚠️ 今日有{n_skip}次信号因资金/持股不足跳过")
    plain = "\n".join(P)

    # ---- HTML表格版(微信) ----
    H = []
    H.append(notify.sec(f"📋 今日做T总览 ({len(trades)}笔)"))
    if not grp_order:
        H.append("<div style='color:#bbbbbb;font-size:12px;'>今日无做T交易</div>")
    for c in grp_order:
        nm, rz, fl2, pn, ex = t_map[c]
        head = (f"<b>{nm}</b>　已回补T"
                f"{notify.colored(fmt_pnl(rz), notify.pnl_color(rz))} · "
                f"未回补浮动{notify.colored(fmt_pnl(fl2), notify.pnl_color(fl2))} · "
                f"合计{notify.colored(notify.bold(fmt_pnl(pn)), notify.pnl_color(pn))}"
                + (notify.colored(f" ({ex})", config.C_GRAY) if ex else ""))
        H.append(f'<div style="margin:8px 0 2px;font-size:13px;">{head}</div>')
        rows = []
        for t in [x for x in trades if x["code"] == c]:
            sd = notify.colored(notify.bold(
                {"buy": "买入", "sell": "卖出"}.get(
                    t["side"], t.get("side_txt") or t["side"])),
                config.C_GREEN if t["side"] in ("sell", "stop_clear",
                                                "stop_t", "manual_sell")
                else config.C_RED)
            inf = leg_info.get(id(t))
            if inf:
                bp = "/".join(inf["bp"]) or "—"
                sp = "/".join(inf["sp"]) or "—"
            else:   # 非T腿成交(止损/手动同步): 显示自身成交价
                bp = fmt_price(t["price"]) if t["side"] == "buy" else "—"
                sp = fmt_price(t["price"]) if t["side"] != "buy" else "—"
            rows.append([t["time"][:5], sd, str(t["qty"]), bp, sp,
                         f"{t.get('shares_before', '?')}→{t.get('shares_after', '?')}",
                         notify.colored(t.get("strat") or "", config.C_GRAY)])
        H.append(notify.table(
            ["时间", "方向", "股数", "买价", "卖价", "持仓变化", "T类"], rows))
    H.append(notify.table(
        ["已回补T合计", "未回补浮动", "费用", "今日净盈亏"],
        [[notify.colored(fmt_pnl(realized_today), notify.pnl_color(realized_today)),
          notify.colored(fmt_pnl(floating_today), notify.pnl_color(floating_today)),
          fmt_amt(fee_today),
          notify.colored(notify.bold(fmt_pnl(net_today)), notify.pnl_color(net_today))]]))
    H.append(notify.sec("🔗 今日T腿配对(买价/卖价)"))
    if leg_rows:
        H.append(notify.table(
            ["股票", "类型", "买价", "卖价", "股数", "盈亏", "状态"],
            [[nm, tg, bp, sp, str(qy),
              notify.colored(notify.bold(fmt_pnl(pn)), notify.pnl_color(pn)),
              notify.colored(stt, config.C_GRAY)]
             for nm, tg, bp, sp, qy, pn, stt, _tm in leg_rows]))
    else:
        H.append("<div style='color:#bbbbbb;font-size:12px;'>今日无T腿</div>")
    if ai_line:
        H.append(f"<div style='color:#bbbbbb;font-size:12px;'>{ai_line}</div>")
    H.append(notify.sec(f"累积做T盈亏(自{state['init_date']}起, 含未回补浮动)"))
    H.append(notify.table(
        ["累计净盈亏", "累计费用"],
        [[notify.colored(notify.bold(fmt_pnl(net_cum)), notify.pnl_color(net_cum)),
          fmt_amt(state["cum_fee"])]]))
    H.append(notify.sec("💰 账户概况"))
    day_txt = ""
    if day_chg is not None:
        day_txt = (notify.colored(notify.bold(fmt_pnl(day_chg)),
                                  notify.pnl_color(day_chg))
                   + f"<small>({day_chg / prev_asset * 100:+.2f}%)</small>")
    acct = [[fmt_amt(asset), fmt_amt(state["cash"]), day_txt or "--",
             notify.colored(notify.bold(fmt_pnl(acct_pnl)) if base else "--",
                            notify.pnl_color(acct_pnl))]]
    H.append(notify.table(["当前总资产", "可用现金", "较昨日收盘", "账户总盈亏(对期初)"],
                          acct))
    H.append(notify.sec("📦 持仓概览"))
    H.append(notify.table(
        ["股票", "持股", "现价", "市值", "成本", "今日T盈亏", "T收益率", "浮动盈亏", "备注"],
        [[nm, str(sh), fmt_price(pr) + (f"*" if tg else ""), fmt_amt(sh * pr),
          fmt_price(cs),
          notify.colored(notify.bold(fmt_pnl(tp)), notify.pnl_color(tp))
          if tp is not None else "—",
          notify.colored(f"{tpc:+.2f}%", notify.pnl_color(tpc))
          if tpc is not None else "—",
          notify.colored(notify.bold(fmt_pnl(fl3)), notify.pnl_color(fl3)),
          notify.colored(tg, config.C_GRAY) if tg else ""]
         for nm, sh, pr, cs, tp, tpc, fl3, tg in pos_rows]))
    H.append("<div style='color:#bbbbbb;font-size:12px;'>今日T盈亏=该股今日做T台账"
             "(已回补+未回补浮动, 未扣费); T收益率=今日T盈亏/今日T买入金额"
             "(纯卖出未回补按卖出金额)</div>")
    H.append(notify.sec("🗳 策略统计(启发式)"))
    srows = []
    for k in SKEYS:
        s = st[k]
        if not _st_active(s):
            continue
        wr = f"{s['win']}/{s['pair']}" if s["pair"] else "--"
        srows.append([SLBL[k], str(s["trig"]), str(s["exec"]), str(s["wait"]),
                      notify.colored(fmt_pnl(s["realized"]),
                                     notify.pnl_color(s["realized"])),
                      notify.colored(fmt_pnl(s["floating"]),
                                     notify.pnl_color(s["floating"])),
                      wr])
    H.append(notify.table(["策略", "触发", "执行", "观望",
                           "已回补盈亏", "浮动", "胜率"], srows))
    H.append("<div style='color:#bbbbbb;font-size:12px;'>口径: 触发=过线信号中该票"
             "同向且|分|≥0.3的次数; 已回补盈亏按FIFO配对扣双边费用, "
             "归因于卖腿主导票; 胜率=已回补对子中盈利占比</div>")
    if n_skip:
        H.append(f"<div style='color:#e67e22;font-size:12px;'>"
                 f"⚠️ 今日有{n_skip}次信号因资金/持股不足跳过(明细见成交记录)"
                 f"</div>")

    title = (f"📊做T模拟盘 {now.strftime('%m-%d')} | "
             f"今日净{fmt_pnl(net_today)} "
             f"累计净{fmt_pnl(net_cum)}")
    summary = {"date": td, "n_trades": len(trades),
               "t_pnl_gross": round(gross_today, 2), "fees": fee_today,
               "t_pnl_net": round(net_today, 2),
               "t_realized": round(realized_today, 2),
               "t_floating": round(floating_today, 2),
               "cum_net": round(net_cum, 2),
               "total_asset": round(asset, 2),
               "account_pnl": round(acct_pnl, 2),
               "by_strategy": {k: st[k] for k in SKEYS if _st_active(st[k])}}
    return title, "".join(H), plain, summary


def build_eod_log(state, prices, summary):
    """收盘日终记录(写入trades.log): 逐笔成交 + 日内T盈亏(已回补/未回补拆分) + 累计"""
    L = [f"════════════ {summary['date']} 收盘·日内T盈亏记录 ════════════"]
    trades = state["today_trades"]
    if trades:
        for t in trades:
            side_txt = t.get("side_txt") or SIDE_LABEL.get(t["side"], t["side"])
            ai = f" [{t['ai']}]" if t.get("ai") else ""
            L.append(f"  {t['time'][:5]} {side_txt} {t['name']} "
                     f"{t['qty']}股@{fmt_price(t['price'])} "
                     f"费{fmt_amt(t['fee'])}{ai}")
    else:
        L.append("  今日无做T交易")
    L.append("  ── 日内T盈亏 ──")
    any_led = False
    for code, led in state["today_ledger"].items():
        if led["bq"] == 0 and led["sq"] == 0:
            continue
        any_led = True
        price = prices.get(code, state.get("last_prices", {}).get(code, 0))
        realized, floating = t_pnl_detail(led, price)
        net = led["bq"] - led["sq"]
        extra = f" ({'多买' if net > 0 else '多卖'}{abs(net)}股未回补)" if net else ""
        name = state["positions"].get(code, {}).get("name", code)
        L.append(f"  {name}: 日内已回补T {fmt_pnl(realized)} | "
                 f"未回补浮动 {fmt_pnl(floating)} | 合计 {fmt_pnl(realized + floating)}{extra}")
    if not any_led:
        L.append("  无")
    L.append(f"  日内已回补T: {fmt_pnl(summary['t_realized'])} | "
             f"未回补浮动: {fmt_pnl(summary['t_floating'])} | "
             f"费用: {fmt_amt(summary['fees'])} | 今日净: {fmt_pnl(summary['t_pnl_net'])}")
    L.append(f"  累计净: {fmt_pnl(summary['cum_net'])} | "
             f"总资产: {fmt_amt(summary['total_asset'])} | "
             f"账户盈亏: {fmt_pnl(summary['account_pnl'])}")
    a = state.get("today_ai", {})
    if a.get("asked"):
        L.append(f"  AI研判: {a['asked']}次 (执行{a['execute']}/观望{a['wait']}/失败{a['fail']})")
    return "\n".join(L)
