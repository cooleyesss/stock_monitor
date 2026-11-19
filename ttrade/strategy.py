# -*- coding: utf-8 -*-
"""策略层: 做T信号判断 + 统一调度
信号触发(价格+量比确认) -> 一次DeepSeek分析 -> 一条微信推送(告警+AI结论+成交详情+当日T汇总)
AI建议观望且守门开启 -> 不成交只推送; AI失败 -> 按原策略执行; 止损纪律优先不走AI
"""
import datetime
from ttrade import config_ttrade as config
from common import notify
from common import quotes
from common import textparse
from ttrade import state as tstate
from ttrade import ai as ai_mod
from ttrade import account
from ttrade import vote


# ---------------- 防重复触发 ----------------
def _armed(state, code, kind):
    return f"{code}|{kind}" in state["armed"]


def _arm(state, code, kind):
    k = f"{code}|{kind}"
    if k not in state["armed"]:
        state["armed"].append(k)


def _disarm(state, code, kind):
    k = f"{code}|{kind}"
    if k in state["armed"]:
        state["armed"].remove(k)


def _vr_txt(vr):
    return f"量比{vr:.2f}" if vr is not None else "量比无数据"


# ---------------- 止损跳空豁免+收盘确认 ----------------
def _stop_confirm_time():
    """收盘确认时点 -> datetime.time"""
    try:
        hh, mm = str(config.ST_GAP_CONFIRM).split(":")
        return datetime.time(int(hh), int(mm))
    except (ValueError, TypeError):
        return datetime.time(14, 50)


def _gap_deep_through(code, line, today):
    """开盘价是否深穿止损线≥ST_GAP_PCT%; 返回(是否跳空深穿, 开盘价, 跳空幅度%)"""
    bars = quotes.get_bars_1m(code) or []
    d = vote.bars_of_day(bars, today)
    if not d:
        return False, None, None
    op = d[0]["open"]
    if not op:
        return False, None, None
    gap = (line - op) / line * 100.0
    return (gap >= config.ST_GAP_PCT), op, gap


def _push_gap_hold(name, line, op, gap, dry):
    """跳空豁免: 开盘深穿止损线不立即清仓, 挂起待收盘确认"""
    print(f"[跳空豁免] {name} 开盘{account.fmt_price(op)}深穿止损线"
          f"{account.fmt_price(line)}(跳空{gap:.2f}%) -> 挂起待收盘确认"
          f"({config.ST_GAP_CONFIRM})")
    if dry:
        return
    title = f"⏸ 止损跳空豁免·待收盘确认 {name}"
    content = "<br>".join([
        _signal_head("stop", name, op, 0),
        notify.colored(notify.bold(f"🚑 止损线 {account.fmt_price(line)} 开盘即破"),
                       config.C_ORANGE),
        notify.colored(f"📉 开盘跳空低开{gap:.2f}%(开盘{account.fmt_price(op)}), "
                       f"不按开盘价清仓(避免卖在最低)", config.C_ORANGE),
        notify.colored(f"⏳ 待 {config.ST_GAP_CONFIRM} 收盘确认: 仍破位才清仓, "
                       f"收复止损线则自动解除", config.C_GRAY),
    ])
    notify.send(title, content)


def _push_gap_cancel(name, line, p, dry):
    """跳空豁免解除: 已收复止损线, 判定为假破位"""
    print(f"[跳空豁免解除] {name} 已收复止损线{account.fmt_price(line)}"
          f"(现价{account.fmt_price(p)})")
    if dry:
        return
    title = f"✅ 跳空豁免解除 {name}"
    content = "<br>".join([
        _signal_head("buy", name, p, 0),
        notify.colored(notify.bold(f"✅ 已收复止损线 {account.fmt_price(line)}"),
                       config.C_RED),
        notify.colored("早盘跳空低开判定为假破位, 取消清仓, 恢复正常做T",
                       config.C_GRAY),
    ])
    notify.send(title, content)


# ---------------- 统一推送组装 ----------------
def _ai_html(ai):
    if not ai:
        return ""
    if not ai.get("ok"):
        return (f"<br>── 🤖AI节点分析 ──<br>"
                f"AI不可用({ai.get('text', '失败')}), 已按原策略执行")
    if ai["verdict"] == "execute":
        v = notify.colored(notify.bold("✅建议执行"), config.C_RED)
    else:
        v = notify.colored(notify.bold("⛔建议观望"), config.C_GREEN)
    return (f"<br>── 🤖AI节点分析 ──<br>结论: {v}<br>"
            + ai.get("text", "").replace("\n", "<br>"))


def _exec_html(rec):
    if not rec:
        return ""
    return (notify.sec("📋 成交详情") + notify.table(
        ["项目", "数值"],
        [["操作", notify.colored(notify.bold(
            f"{rec['side_txt']} {rec['qty']}股"), config.C_RED)],
         ["成交价格", account.fmt_price(rec["price"])],
         ["成交金额", account.fmt_amt(rec["amount"])],
         ["费用", account.fmt_amt(rec["fee"])],
         ["持股变动", f"{rec['shares_before']}股 → "
          + notify.bold(f"{rec['shares_after']}股")],
         ["现金变动", f"{account.fmt_amt(rec['cash_before'])} → "
          + notify.bold(account.fmt_amt(rec["cash_after"]))]]))


def _signal_head(kind, name, p, pct):
    icon = {"buy": "🚀", "sell": "🎯", "stop": "⚠️"}[kind]
    arrow = "⬇️" if kind != "sell" else "⬆️"
    color = config.C_GREEN if kind == "stop" else config.C_RED
    return (notify.colored(notify.bold(f"{icon}{icon}{icon} {name} {icon}{icon}{icon}"),
                           color)
            + f"<br>{notify.colored(notify.bold(f'{arrow} 现价 {account.fmt_price(p)}'), color)}"
            + f" ({pct:+.2f}%)")


# ---------------- T频率控制(防横盘反复成交) ----------------
def _t_hist(state, code, side):
    return state.setdefault("t_hist", {}).setdefault(f"{code}|{side}", [])


def side_txt(side):
    return "低吸" if side == "buy" else "高抛"


def _freq_blocked(state, code, side, now):
    """返回(是否拦截, 原因): 每日次数上限 + 同向冷却"""
    hist = _t_hist(state, code, side)
    if config.T_MAX_PER_DAY and len(hist) >= config.T_MAX_PER_DAY:
        return True, (f"今日{side_txt(side)}已成交{len(hist)}次"
                      f"(上限{config.T_MAX_PER_DAY}次)")
    if config.T_COOLDOWN_MIN and hist:
        try:
            last = datetime.datetime.strptime(hist[-1], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            last = now
        gap = (now - last).total_seconds() / 60
        if gap < config.T_COOLDOWN_MIN:
            return True, (f"距上次{side_txt(side)}成交仅{gap:.0f}分钟"
                          f"(冷却{config.T_COOLDOWN_MIN}分钟)")
    return False, ""


def _record_t_hist(state, code, side, now):
    """成交后记录(用于次日次数/冷却判断); rollover每日重置"""
    _t_hist(state, code, side).append(now.strftime("%Y-%m-%d %H:%M:%S"))


# ---------------- 配对T(防只卖不买变空仓) ----------------
def net_sell(state, code):
    """未回补净卖出股数(日内口径, 每日rollover自动清零): 卖出总股数 - 买入总股数
    核心修复(2026-10-20): 原用 cum_ledger(累计台账) 导致"某日多卖未回补 -> 此后永远拦截"
        ，改 today_ledger 后每日自动清零，只拦日内连续多卖，历史欠账不影响次日"""
    led = state.get("today_ledger", {}).get(code) or {}
    return int(led.get("sq", 0) - led.get("bq", 0))


def _pair_blocked(state, code, plan, lot, max_mult=None):
    """配对拦截(两层保护):
      ① 持仓耗尽保护: 剩余持仓 < 单次T量 或 < 最低阈值 → 拦截(绝不能空仓)
      ② 日内净卖出上限: 未回补净卖出 ≥ max_mult×单次T量 → 拦截(强制回补)
    参数:
      max_mult: 阈值倍数, None=读 config.PAIR_MAX_MULT(默认2)
    注: 阈值不再用 cum_ledger 累积, 改为 today_ledger 每日自动清零"""
    if not config.PAIR_T:
        return False, ""
    mult = max_mult if max_mult is not None else getattr(config, "PAIR_MAX_MULT", 2)
    t_qty = plan.get("t_qty") or lot or config.LOT_MIN
    pos = state.get("positions", {}).get(code, {}).get("shares", 0)
    # 层① 硬底线: 剩余持仓必须 ≥ LOT_MIN(100股) 或至少够一次T
    min_pos = max(config.LOT_MIN, t_qty)
    if pos - t_qty < min_pos:
        return True, (f"持仓{pos}股, 单次T{t_qty}股, "
                      f"卖出后剩{pos - t_qty}股 < 安全底线{min_pos}股, 禁止高抛")
    ns = net_sell(state, code)
    # 层② 配对上限: max_mult 倍T量后强制回补
    threshold = mult * t_qty
    if ns >= threshold:
        return True, (f"日内未回补净卖出{ns}股≥{mult}次T量{threshold}股, "
                      f"需先低吸回补后再高抛(持仓{pos})")
    return False, ""


# ---------------- 量化投票辅助 ----------------
def _quant_eval(code, plan, q, dwell, regime, now):
    """组装投票引擎输入: 拉1分钟K线取当日切片(K线失败时技术票弃权, 退化为旧策略)"""
    bars = quotes.get_bars_1m(code)
    bars_today = vote.bars_of_day(bars, textparse.today_str(now)) if bars else None
    return vote.evaluate(plan, q, bars_today, dwell_min=dwell, regime=regime)


def _on_vote_fail(state, code, name, side, q, res, dry, now):
    """锚定到位但投票未共振: 冷却期内不重复推送 + 连续失败累加streak
    streak >= VOTE_FAIL_ESCALATE 时不再推送(等 check_signal 降级执行旧策略)"""
    _arm(state, code, "votefail")
    s = res[side]
    gate_txt = vote.GATE_LABEL.get(s["gate"], s["gate"])

    # ── 连续失败计数(用于升级判断) ──
    key = f"{code}|{side}"
    streak = state.setdefault("vote_fail_streak", {})
    streak[key] = streak.get(key, 0) + 1

    print(f"[投票未共振 #{streak[key]}] {name} {vote.SIDE_CN[side]} {gate_txt} "
          f"总分{s['score']:.2f}/{s['thr']:.2f} 共振{s['agree']}/{s['agree_need']}")

    if dry:
        print(vote.votes_text(res, side))
        return

    # ── 冷却期内 / 已达升级阈值 → 不推送 ──
    if streak[key] >= config.VOTE_FAIL_ESCALATE:
        print(f"  → 已达升级阈值({config.VOTE_FAIL_ESCALATE}次), "
              f"下轮将跳过量化投票降级执行")
        return
    last = state.setdefault("vote_fail_time", {}).get(key)
    if last and (now - last).total_seconds() < config.VOTE_FAIL_COOLDOWN_SEC:
        remain = int(config.VOTE_FAIL_COOLDOWN_SEC - (now - last).total_seconds())
        print(f"  → 冷却中(剩{remain}秒), 跳过推送")
        return
    state["vote_fail_time"][key] = now

    if not config.VOTE_FAIL_NOTIFY:
        return
    title = f"🗳 {vote.SIDE_CN[side]}信号·投票未共振 {name}"
    content = "<br>".join([
        _signal_head("buy" if side == "buy" else "sell", name,
                     q["price"], q.get("pct", 0) or 0),
        vote.votes_html(res, side),
        notify.colored(
            f"⏳ 锚定到位, 已连续失败{streak[key]}次"
            f"(阈值{config.VOTE_FAIL_ESCALATE}, 将降级执行)",
            config.C_ORANGE),
        account.today_t_summary_html(state),
    ])
    notify.send(title, content)


# ---------------- 信号处理核心 ----------------
def _handle_signal(state, code, name, side, q, plan, lot, ind, now, dry,
                   exempt_txt="", eval_res=None):
    """信号已确认(锚定+投票共振) -> AI分析 -> 成交检查 -> 统一推送"""
    p, vr, pct = q["price"], q["vr"], q.get("pct", 0) or 0
    if side == "buy":
        zone = (f"低吸区{account.fmt_price(plan['buy_low'])}~"
                f"{account.fmt_price(plan['buy_high'])}")
        vr_cond = (f"(要求≤{plan.get('buy_vr_max')}✅)"
                   if plan.get("buy_vr_max") is not None else "")
        head_kind = "buy"
    else:
        zone = (f"高抛区{account.fmt_price(plan['sell_low'])}~"
                f"{account.fmt_price(plan['sell_high'])}")
        vr_cond = (f"(要求≥{plan.get('sell_vr_min')}✅)"
                   if plan.get("sell_vr_min") is not None else "")
        head_kind = "sell"
    ind = ind or {}
    macd_line = quotes.macd_note(ind.get("macd"), side)
    rsi_line = quotes.rsi_note(ind, side)
    tags = vote.main_tag(eval_res, side) if eval_res else ""

    # ── AI守门(一次调用, prompt含量化投票明细) ──
    print(f"[AI分析] {name} {zone} {_vr_txt(vr)} → 询问DeepSeek...")
    ai = ai_mod.deepseek_analyze(state, code, plan, q, side, lot, ind, now,
                                 eval_res=eval_res)
    allowed = True
    ai_override = False     # 非对称守门: AI判观望但被放行(低吸侧)
    if ai and ai.get("ok") and ai["verdict"] == "wait" and config.DEEPSEEK_GATEKEEP:
        # 守门口径: GATEKEEP_SELL_ONLY=AI只拦高抛(防卖飞), 低吸侧仅提示不拦
        #   否则弱市里AI对低吸系统性"观望"会把买回通道完全堵死(实测27次中25次观望, 低吸零成交)
        if side == "sell" or not config.GATEKEEP_SELL_ONLY:
            allowed = False
        else:
            ai_override = True
    if ai and not ai.get("ok"):
        print(f"[AI失败] {name} {ai.get('text')}, 按原策略执行")
    ai_tag = ""
    if ai and ai.get("ok"):
        if ai["verdict"] == "execute":
            ai_tag = "AI建议执行"
        elif ai_override:
            ai_tag = "AI建议观望(非对称守门放行)"
            print(f"[非对称守门] {name} 低吸侧AI判观望, 按量化策略执行(仅提示不拦)")
        else:
            ai_tag = "AI建议观望"

    # ── 灰度观察模式: 量化投票通过仅推送不成交 ──
    obs = config.STRATEGY_DRY_RUN and allowed and bool(eval_res)
    if obs:
        skip_txt = "观察模式(STRATEGY_DRY_RUN): 量化共振通过, 仅提示不成交"
    else:
        skip_txt = ""

    # ── 成交执行 ──
    rec = None
    if allowed and not obs:
        if side == "buy":
            if lot < config.LOT_MIN:
                skip_txt = f"做T股数不足100股({lot})"
            else:
                qty = account.affordable_qty(state, code, p, lot)
                if qty < config.LOT_MIN:
                    amount = round(lot * p, 2)
                    need = amount + account.buy_fee(amount, code)
                    skip_txt = (f"现金不足(需{account.fmt_amt(need)}"
                                f"/有{account.fmt_amt(state['cash'])})")
                else:
                    note = zone
                    if qty < lot:
                        note += f" | 资金不足, T量{lot}→{qty}股"
                        print(f"[资金调整] {name} 计划T量{lot}股 → "
                              f"按可用现金{account.fmt_amt(state['cash'])}调整为{qty}股")
                    if dry:
                        amt2 = round(qty * p, 2)
                        print(f"[DRY] 将低吸买入 {name} {qty}股@"
                              f"{account.fmt_price(p)} 需资金"
                              f"{account.fmt_amt(amt2 + account.buy_fee(amt2, code))}")
                    else:
                        rec = account.execute_buy(state, code, name, qty, p,
                                              note=note, ai_txt=ai_tag,
                                              strategy_tags=tags)
                        if rec:
                            _record_t_hist(state, code, side, now)
        else:
            pos = state["positions"].get(code, {})
            shares = pos.get("shares", 0)
            tb = pos.get("today_bought", 0)
            avail = shares - (tb if config.T_PLUS_ONE else 0)
            qty = min(lot, avail // 100 * 100)
            if qty < config.LOT_MIN:
                skip_txt = (f"T+1可卖不足100股(持股{shares}/今买{tb}/可卖{avail})"
                            if config.T_PLUS_ONE else
                            f"可卖持股不足100股({shares}股)")
            else:
                if dry:
                    print(f"[DRY] 将高抛卖出 {name} {qty}股@{account.fmt_price(p)}"
                          f" (可卖{avail}股, 今买{tb}股T+1)")
                else:
                    rec = account.execute_sell(state, code, name, qty, p,
                                           note=zone, ai_txt=ai_tag,
                                           strategy_tags=tags)
                    if rec:
                        _record_t_hist(state, code, side, now)
    if skip_txt:
        state["today_skips"].append({
            "time": now.strftime("%H:%M:%S"), "code": code, "name": name,
            "side": side, "reason": skip_txt})

    # ── 埋点: 过线信号写入投票日志(供EOD策略统计) ──
    if eval_res and not dry:
        vote.record_vote_log(state, code, name, side, eval_res,
                              executed=bool(rec), ai=ai_tag, obs=obs,
                              qty=(rec or {}).get("qty", 0),
                              price=(rec or {}).get("price", p), now=now)

    if dry:
        if eval_res:
            print(vote.votes_text(eval_res, side))
        if ai and ai.get("ok"):
            print(f"[DRY-AI] 结论={ai['verdict']}\n{ai['text']}")
        print(f"[DRY] {name} {zone} AI={'观望(未成交)' if not allowed else ('执行' if not skip_txt else '跳过:' + skip_txt)}")
        return

    # ── 统一微信推送(告警+投票+AI+成交+当日T汇总 一条搞定) ──
    sig = "低吸" if side == "buy" else "高抛"
    prefix = "[观察] " if obs else ""
    if rec:
        title = f"{prefix}{'🚀🚀🚀' if side == 'buy' else '🎯🎯🎯'} {sig}买入·已成交 {name}" \
            if side == "buy" else f"{prefix}🎯🎯🎯 {sig}卖出·已成交 {name}"
    elif not allowed:
        title = f"{prefix}{'🚀' if side == 'buy' else '🎯'} {sig}信号·AI建议观望 {name}"
    else:
        title = f"{prefix}{'🚀' if side == 'buy' else '🎯'} {sig}信号·未成交({skip_txt[:12]}) {name}"
    content = "<br>".join([
        _signal_head(head_kind, name, p, pct),
        f"📐 {zone}",
        f"📊 {_vr_txt(vr)} {vr_cond}",
        (f"⏱ {exempt_txt}" if exempt_txt else ""),
        (vote.votes_html(eval_res, side) if eval_res else ""),
        macd_line,
        rsi_line,
        _ai_html(ai),
        _exec_html(rec),
        ("" if rec and not skip_txt else
         (f"<br>⛔ 未成交: {skip_txt}" if skip_txt else "")),
        account.today_t_summary_html(state),
    ])
    notify.send(title, content)


def _handle_stop(state, code, name, q, plan, line, now, dry):
    """止损: 纪律优先, 不走AI, 按STOP_LOSS_MODE执行"""
    p, vr, pct = q["price"], q["vr"], q.get("pct", 0) or 0
    shares = state["positions"].get(code, {}).get("shares", 0)
    line_txt = f"止损线{account.fmt_price(line)} {account.fmt_price(p)} {_vr_txt(vr)}"
    print(f"[止损触发] {name} {line_txt}")
    rec = None
    mode_txt = ""
    if config.STOP_LOSS_MODE == "clear":
        if shares >= config.LOT_MIN:
            if dry:
                print(f"[DRY] 将止损清仓 {name} {shares}股")
            else:
                rec = account.execute_sell(state, code, name, shares, p,
                                       side="stop_clear", in_ledger=False,
                                       note=line_txt)
                mode_txt = "已清仓"
        else:
            mode_txt = "已无持仓, 仅标记"
    elif config.STOP_LOSS_MODE == "t_only":
        led = account._led(state, code)
        extra = (led["bq"] - led["sq"]) // 100 * 100
        if extra >= config.LOT_MIN:
            if dry:
                print(f"[DRY] 将止损卖出T仓 {name} {extra}股")
            else:
                rec = account.execute_sell(state, code, name, extra, p,
                                       side="stop_t", in_ledger=True, note=line_txt)
                mode_txt = f"已卖出当日T仓{extra}股"
        else:
            mode_txt = "无当日T仓可卖"
    else:   # alert
        mode_txt = "alert模式: 仅提醒不模拟卖出"
    if dry:
        return
    title = f"⚠️⚠️⚠️ 止损警报 {name}"
    ind = quotes.get_indicators(code) or {}
    content = "<br>".join([
        _signal_head("stop", name, p, pct),
        notify.colored(notify.bold(f"🚑 止损线 {account.fmt_price(line)} ❌已破❌"),
                       config.C_GREEN),
        f"📊 {_vr_txt(vr)}",
        quotes.macd_note(ind.get("macd"), "sell"),
        quotes.rsi_note(ind, "sell"),
        (notify.colored(notify.bold("🛑 按纪律执行!🛑"), config.C_GREEN)
         if config.STOP_LOSS_MODE != "alert" else ""),
        _exec_html(rec),
        (f"<br>{mode_txt}" if mode_txt else ""),
        account.today_t_summary_html(state),
    ])
    notify.send(title, content)


def _handle_zone_touch(state, code, name, side, q, plan, lot, ind, now, dry):
    """价格到达T位但量比未达标: 推送到位提醒+AI研判(不做T, 量比达标后自动触发)
    AI结论与上次一致 -> 免打扰不重复推送(结论变了才再推)"""
    p, vr, pct = q["price"], q["vr"], q.get("pct", 0) or 0
    if side == "buy":
        zone = (f"低吸区{account.fmt_price(plan['buy_low'])}~"
                f"{account.fmt_price(plan['buy_high'])}")
        vmax = plan.get("buy_vr_max")
        vr_line = (f"📊 {_vr_txt(vr)} (做T要求≤{vmax} ❌未达标, "
                   f"放量下跌暂不接)")
        wait_txt = "量比回落达标后将自动低吸(AI守门)"
        sig = "低吸位"
        head_kind = "buy"
    else:
        zone = (f"高抛区{account.fmt_price(plan['sell_low'])}~"
                f"{account.fmt_price(plan['sell_high'])}")
        vmin = plan.get("sell_vr_min")
        vr_line = (f"📊 {_vr_txt(vr)} (做T要求≥{vmin} ❌未达标, "
                   f"缩量冲高暂不抛)")
        wait_txt = "量比放大达标后将自动高抛(AI守门)"
        sig = "高抛位"
        head_kind = "sell"
    ind = ind or {}
    print(f"[到达{sig}] {name} {zone} {_vr_txt(vr)} → 询问DeepSeek(仅研判)...")
    ai = ai_mod.deepseek_analyze(state, code, plan, q, side, lot, ind, now)
    # ── 去重: 本次AI结论与上次一致(或AI失败且已有过结论) -> 不再推送 ──
    cache = state.setdefault("touch_ai", {})
    key = f"{code}|{side}"
    verdict = ai.get("verdict") if (ai and ai.get("ok")) else None
    if key in cache and (verdict is None or cache[key] == verdict):
        v_txt = {"execute": "建议执行", "wait": "建议观望"}.get(
            verdict, "AI失败/无新结论")
        print(f"[免打扰] {name} 再次到达{sig}, AI结论与上次一致"
              f"({cache[key]}→{v_txt}), 不重复推送")
        return
    if verdict:
        cache[key] = verdict
    if dry:
        if ai and ai.get("ok"):
            print(f"[DRY-AI] 结论={ai['verdict']}\n{ai['text']}")
        print(f"[DRY] {name} 到达{sig} 量比未达标, 推送提醒(不成交)")
        return
    title = f"📍 到达{sig}·量比未达标 {name}"
    content = "<br>".join([
        _signal_head(head_kind, name, p, pct),
        f"📐 {zone}",
        vr_line,
        quotes.macd_note(ind.get("macd"), side),
        quotes.rsi_note(ind, side),
        _ai_html(ai),
        notify.colored(f"⏳ {wait_txt}", config.C_ORANGE),
        account.today_t_summary_html(state),
    ])
    notify.send(title, content)


# ---------------- 每轮信号检查 ----------------
def check_signal(state, code, plan, q, now, dry=False, regime=None):
    # ── v3策略(基础编排2026-10-30, 2026-11-13并入SVM方向指导): 反T编排+网格回补+环境过滤+收盘兜底, 完全接管本轮T动作 ──
    # 异常时不降级v1(防同日双策略动账), 只跳过本轮下轮重试
    if getattr(config, "STRAT_V3_EN", False):
        from ttrade import ttrade_v3
        try:
            ttrade_v3.v3_check(state, code, plan, q, now, dry, regime)
        except Exception as e:
            print(f"[v3异常] {plan.get('name', code)} 本轮跳过(不降级v1防双跑):", e)
        return
    p, vr = q["price"], q["vr"]
    pos = state["positions"].setdefault(
        code, {"name": plan.get("name", code), "shares": 0, "cost": 0.0})
    name = plan.get("name") or pos.get("name", code)
    shares = pos["shares"]
    lot = account.lot_of(state, code, shares, plan.get("t_qty"))

    # ── 止损挂起(跳空豁免中): 收复止损线->解除; 到收盘确认时点仍破位->清仓 ──
    pend_s = state.setdefault("stop_pending", {})
    if pend_s.get(code) == state["today"] and (plan.get("stop_loss") or []):
        sline = max(plan["stop_loss"])
        if p >= sline:
            pend_s.pop(code)
            _push_gap_cancel(name, sline, p, dry)
        else:
            if now.time() >= _stop_confirm_time():
                pend_s.pop(code)
                state["stop_done"][code] = state["today"]
                _disarm(state, code, "buy")
                _disarm(state, code, "sell")
                _handle_stop(state, code, name, q, plan, sline, now, dry)
                return
            return   # 挂起中: 本轮跳过其它T动作, 等收盘确认

    # ── 止损(优先) ──
    stops = plan.get("stop_loss") or []
    if stops:
        line = max(stops)
        if config.STOP_LOSS_MODE == "alert":
            # alert: 告警防重+涨回1%解除(纪律告警, 不模拟卖出)
            key = (code, "stop0")
            if p <= line:
                if key not in [k for k in state["armed"]]:
                    _arm(state, code, "stop0")
                    state["stop_done"][code] = state["today"]
                    _handle_stop(state, code, name, q, plan, line, now, dry)
            elif p >= line * 1.01:
                state["armed"] = [k for k in state["armed"] if k != f"{code}|stop0"]
        elif state["stop_done"].get(code) != state["today"] and p <= line:
            if config.ST_GAP_EXEMPT_EN:
                deep, op, gap = _gap_deep_through(code, line, state["today"])
                if deep:
                    state.setdefault("stop_pending", {})[code] = state["today"]
                    _push_gap_hold(name, line, op, gap, dry)
                    return
            state["stop_done"][code] = state["today"]
            _disarm(state, code, "buy")
            _disarm(state, code, "sell")
            _handle_stop(state, code, name, q, plan, line, now, dry)
            return
    if state["stop_done"].get(code) == state["today"]:
        return

    ind = quotes.get_indicators(code)

    # ── 低吸: 到T位必推送+AI研判; 量比达标(缩量回踩)或停留超时豁免才实际做T ──
    if "buy_low" in plan:
        if plan["buy_low"] <= p <= plan["buy_high"]:
            zkey = f"{code}|buy"
            ent = state.setdefault("zone_since", {}).get(zkey)
            if not ent:
                state["zone_since"][zkey] = now.strftime("%Y-%m-%d %H:%M:%S")
                dwell = 0.0
            else:
                try:
                    t0 = datetime.datetime.strptime(ent, "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    t0 = now
                dwell = (now - t0).total_seconds() / 60
            vmax = plan.get("buy_vr_max")
            vr_ok = vmax is None or vr is None or vr <= vmax
            exempt = ""
            if (not vr_ok and config.T_DWELL_MIN
                    and dwell >= config.T_DWELL_MIN):
                vr_ok = True
                exempt = (f"在低吸区已停留{dwell:.0f}分钟, "
                          f"视为缩量企稳, 量比条件豁免")
            if vr_ok and not _armed(state, code, "buy"):
                _arm(state, code, "buy")
                blocked, why = _freq_blocked(state, code, "buy", now)
                if blocked:
                    print(f"[限频] {name} 低吸信号被拦截: {why}")
                else:
                    # ── 连续失败升级: 达阈值后跳过量化投票, 直接按旧策略执行 ──
                    streak = state.get("vote_fail_streak", {}).get(f"{code}|buy", 0)
                    escalated = config.QUANT_ENABLED and streak >= config.VOTE_FAIL_ESCALATE
                    if escalated:
                        print(f"[降级执行] {name} 低吸: 连续投票失败{streak}次, "
                              f"跳过量化投票走旧策略")
                        escalate_txt = (f"降级执行(连续投票失败{streak}次, "
                                        f"阈值{config.VOTE_FAIL_ESCALATE})")
                        _handle_signal(state, code, name, "buy", q, plan,
                                       lot, ind, now, dry,
                                       exempt_txt=exempt + ("; " if exempt else "") + escalate_txt)
                        state["vote_fail_streak"][f"{code}|buy"] = 0   # 执行成功后reset
                    elif config.QUANT_ENABLED:
                        # 量化投票: 锚定到位后技术票共振才触发(未共振disarm留待下轮重评)
                        res = _quant_eval(code, plan, q, dwell, regime, now)
                        if res["buy"]["pass"]:
                            state["vote_fail_streak"][f"{code}|buy"] = 0   # 成功即reset
                            _handle_signal(state, code, name, "buy", q, plan,
                                           lot, ind, now, dry,
                                           exempt_txt=exempt, eval_res=res)
                        else:
                            _disarm(state, code, "buy")
                            _on_vote_fail(state, code, name, "buy", q,
                                          res, dry, now)
                    else:
                        _handle_signal(state, code, name, "buy", q, plan,
                                       lot, ind, now, dry, exempt_txt=exempt)
            elif not vr_ok and not _armed(state, code, "buytouch"):
                _arm(state, code, "buytouch")
                _handle_zone_touch(state, code, name, "buy", q, plan,
                                   lot, ind, now, dry)
        else:
            _disarm(state, code, "buy")
            _disarm(state, code, "buytouch")
            _disarm(state, code, "votefail")
            state.get("zone_since", {}).pop(f"{code}|buy", None)
            state.get("vote_fail_streak", {}).pop(f"{code}|buy", None)   # 离开区间=新周期, 重置失败计数

    # ── 高抛: 到T位必推送+AI研判; 量比达标(放量冲高)且配对允许才实际做T ──
    if "sell_low" in plan:
        if plan["sell_low"] <= p <= plan["sell_high"]:
            vmin = plan.get("sell_vr_min")
            vr_ok = vmin is None or vr is None or vr >= vmin
            if vr_ok and not _armed(state, code, "sell"):
                _arm(state, code, "sell")
                blocked, why = _freq_blocked(state, code, "sell", now)
                if blocked:
                    print(f"[限频] {name} 高抛信号被拦截: {why}")
                else:
                    pblocked, pwhy = _pair_blocked(state, code, plan, lot)
                    if pblocked:
                        print(f"[配对拦截] {name} {pwhy}")
                        state["today_skips"].append({
                            "time": now.strftime("%H:%M:%S"), "code": code,
                            "name": name, "side": "sell", "reason": pwhy})
                        if not dry:
                            notify.send(
                                f"⛔ 高抛·配对拦截 {name}",
                                "<br>".join([
                                    f"<b>{name}({code})</b> "
                                    f"{account.fmt_price(q['price'])}",
                                    f"📐 高抛区{account.fmt_price(plan['sell_low'])}"
                                    f"~{account.fmt_price(plan['sell_high'])}",
                                    f"⛔ {pwhy}",
                                    "💡 需先低吸回补, 才能继续高抛",
                                    account.today_t_summary_html(state)]))
                    elif config.QUANT_ENABLED:
                        # ── 连续失败升级: 达阈值后跳过量化投票, 直接按旧策略执行 ──
                        streak = state.get("vote_fail_streak", {}).get(f"{code}|sell", 0)
                        escalated = streak >= config.VOTE_FAIL_ESCALATE
                        if escalated:
                            print(f"[降级执行] {name} 高抛: 连续投票失败{streak}次, "
                                  f"跳过量化投票走旧策略")
                            _handle_signal(state, code, name, "sell", q, plan,
                                           lot, ind, now, dry,
                                           exempt_txt=f"降级执行(连续投票失败{streak}次)")
                            state["vote_fail_streak"][f"{code}|sell"] = 0
                        else:
                            # 量化投票: 锚定+配对通过后技术票共振才触发
                            res = _quant_eval(code, plan, q, 0.0, regime, now)
                            if res["sell"]["pass"]:
                                state["vote_fail_streak"][f"{code}|sell"] = 0   # 成功即reset
                                _handle_signal(state, code, name, "sell", q, plan,
                                               lot, ind, now, dry, eval_res=res)
                            else:
                                _disarm(state, code, "sell")
                                _on_vote_fail(state, code, name, "sell", q,
                                              res, dry, now)
                    else:
                        _handle_signal(state, code, name, "sell", q, plan,
                                       lot, ind, now, dry)
            elif not vr_ok and not _armed(state, code, "selltouch"):
                _arm(state, code, "selltouch")
                _handle_zone_touch(state, code, name, "sell", q, plan,
                                   lot, ind, now, dry)
        else:
            _disarm(state, code, "sell")
            _disarm(state, code, "selltouch")
            _disarm(state, code, "votefail")
            state.get("vote_fail_streak", {}).pop(f"{code}|sell", None)   # 离开区间=新周期


# ---------------- 手动同步真实卖出 ----------------
def manual_sell(state, key, price, qty):
    pos = state["positions"].get(key)
    if not pos:   # 按名称再找一次(如 ZZ1000)
        for k, v in state["positions"].items():
            if k == key or v.get("name") == key:
                key, pos = k, v
                break
    if not pos:
        print(f"未找到持仓: {key}  (现有: "
              + ", ".join(f"{k}:{v['name']}" for k, v in state["positions"].items()) + ")")
        return False
    if qty > pos["shares"]:
        print(f"持股不足: {pos['name']}只有{pos['shares']}股")
        return False
    rec = account.execute_sell(state, key, pos["name"], qty, price,
                           side="manual_sell", in_ledger=False,
                           note="手动同步: 真实账户已卖出")
    notify.send(f"📋 手动卖出·同步真实操作 {pos['name']}",
                "<br>".join([
                    f"<b>{pos['name']}({key})</b>",
                    notify.bold("📋 手动卖出(同步真实操作)"),
                    _exec_html(rec),
                    account.today_t_summary_html(state),
                ]))
    print(f"手动卖出完成, 当前现金{account.fmt_amt(state['cash'])}")
    return True


# ---------------- 收盘报告 ----------------
def eod_dashboard_only(state, now):
    """收盘战报幂等跳过时, 仍生成/归档当日看板(不重复推送战报)"""
    td = textparse.today_str(now)
    tplan = tstate.load_t_plan()
    from ttrade.runner import collect_codes   # 局部导入避免循环(该函数在 ttrade/runner.py)
    try:
        quotes_raw = quotes.fetch_quotes(collect_codes(state, tplan))
        prices = {c: q["price"] for c, q in quotes_raw.items()
                  if q.get("ts") and datetime.datetime.fromtimestamp(q["ts"]).date() == now.date()}
    except Exception as e:
        print("[看板补生成] 行情获取失败:", e)
        return
    if not prices:
        print(f"[看板补生成] {td} 无当日行情(休市?), 跳过")
        return
    try:
        from ttrade import dashboard
        dp = dashboard.build(state, prices=prices, rep_date=td, now=now)
        print(f"日内T看板 -> {dp}")
    except Exception as e:
        print("[日内T看板] 生成失败:", e)


def do_eod(state, now, dry=False):
    tplan = tstate.load_t_plan()
    from ttrade.runner import collect_codes   # 局部导入避免循环(该函数在 ttrade/runner.py)
    try:
        quotes_raw = quotes.fetch_quotes(collect_codes(state, tplan))
    except Exception as e:
        print("收盘行情获取失败, 5分钟后重试:", e)
        return False
    td = textparse.today_str(now)
    fresh = {c: q for c, q in quotes_raw.items()
             if q.get("ts") and datetime.datetime.fromtimestamp(q["ts"]).date() == now.date()}
    if not fresh:
        print(f"{td} 休市(节假日), 不发送收盘报告")
        state["eod_sent"] = td
        tstate.save_state(state, dry)
        return True
    prices = {c: q["price"] for c, q in fresh.items()}
    state["last_prices"].update(prices)
    if not state.get("baseline"):
        state["baseline"] = {"total_asset": round(account.total_asset(state, prices), 2),
                             "set_at": now.strftime("%Y-%m-%d %H:%M")}
    title, html, plain, summary = account.build_eod_summary(state, prices, now)
    if dry:
        print("=== 收盘报告预览(标题) ===")
        print(title)
        print("=== 报告正文 ===")
        print(plain)
        return True
    notify.send(title, html)
    state["daily_history"].append(summary)
    state["eod_sent"] = td
    tstate.save_state(state)
    # 收盘日终记录写入成交日志
    tstate.append_trade_log(account.build_eod_log(state, prices, summary), dry)
    print("── 收盘报告 ──\n" + plain)
    # 日内T独立看板(与量化 dashboard_quant.html 相互独立, 互不覆盖)
    try:
        from ttrade import dashboard
        dp = dashboard.build(state, prices=prices, rep_date=td, now=now)
        print(f"日内T看板 -> {dp}")
    except Exception as e:
        print("[日内T看板] 生成失败:", e)
    # v3(2026-11-16改日频): SVM方向预测已移至交易日9:00主循环重训(runner),
    # 收盘日跑不再训练; 8:45新闻雷达隔夜汇总 -> 9:00训练 -> 9:26 t_plan参考。
    return True


# ---------------- 快照/计划摘要 ----------------
def format_snapshot(quotes_raw):
    lines = []
    for c, q in quotes_raw.items():
        pct = q.get("pct", 0) or 0
        vr = q.get("vr")
        vr_txt = f" ｜📊{vr:.1f}" if vr is not None else ""
        ind = quotes.get_indicators(c) or {}
        rsi = ind.get("rsi")
        rsi_txt = f" ｜🌡️R{rsi:.0f}" if rsi is not None else ""
        m = ind.get("macd")
        mc_txt = ""
        if m and m.get("phist") is not None:
            if m["hist"] > m["phist"]:
                mc_txt = " ｜〰️MC" + notify.colored("↑", config.C_RED)
            elif m["hist"] < m["phist"]:
                mc_txt = " ｜〰️MC" + notify.colored("↓", config.C_GREEN)
            else:
                mc_txt = " ｜〰️MC→"
        if pct > 0:
            lines.append(notify.colored(
                f"🔴⬆️ {q['name']} {notify.bold(account.fmt_price(q['price']))} "
                f"(+{pct:.2f}%){vr_txt}{rsi_txt}{mc_txt}", config.C_RED))
        elif pct < 0:
            lines.append(notify.colored(
                f"🟢⬇️ {q['name']} {notify.bold(account.fmt_price(q['price']))} "
                f"({pct:.2f}%){vr_txt}{rsi_txt}{mc_txt}", config.C_GREEN))
        else:
            lines.append(f"⚪➡️ {q['name']} {account.fmt_price(q['price'])} "
                         f"(0.00%){vr_txt}{rsi_txt}{mc_txt}")
    return "<br>".join(lines) if lines else "无行情数据"


def format_t_plan(plan):
    lines = []
    for code, p in plan.items():
        seg = [notify.bold(p.get("name", code))]
        if "buy_low" in p:
            vmax = p.get("buy_vr_max")
            seg.append(f"🚀低吸 {notify.colored(notify.bold(account.fmt_price(p['buy_low']) + '~' + account.fmt_price(p['buy_high'])), config.C_RED)}"
                       + (f"(量比≤{vmax})" if vmax is not None else ""))
        if "sell_low" in p:
            vmin = p.get("sell_vr_min")
            seg.append(f"🎯高抛 {notify.colored(notify.bold(account.fmt_price(p['sell_low']) + '~' + account.fmt_price(p['sell_high'])), config.C_RED)}"
                       + (f"(量比≥{vmin})" if vmin is not None else ""))
        if p.get("stop_loss"):
            seg.append("🛑止损 " + notify.colored(
                notify.bold("/".join(account.fmt_price(s) for s in p["stop_loss"])),
                config.C_GREEN))
        lines.append(" ｜ ".join(seg))
    return "<br>".join(lines) if lines else "当前无做T计划"
