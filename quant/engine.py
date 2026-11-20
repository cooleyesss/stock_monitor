# -*- coding: utf-8 -*-
"""量化独立账户日频引擎: 收盘后运行一次
流程(严格无未来函数, 与回测run_ps_stock语义一致):
  ①上一交易日收盘决策的pending -> 盘中双窗口择机执行(quant/runner.py: 早盘9:30~10:00/
     尾盘14:30~14:55, 14:55兜底; 协调标记state["day_plan"]), runner未跑时仍以今日开盘价
     兜底(scalp需开盘>=确认位, 否则取消记日志)
  ②以今日收盘decide -> 存pending待明日; 漏跑多天逐日补跑(成交用信号日+1的历史开盘价)
  ③调仓日(2026-10-30弹性机制: 常规每PICK_REBAL=5个交易日; 持股<3只提前补仓;
     在管达5只封顶不新建仓; 候选池Top动量>=15%的特别好机会可忽略调仓日临时建仓)
     归集"已清仓子账户"的现金到选股池, 并按全局选股规则补入新标的
幂等: 子账户last_date>=最新bar日期则跳过; 收盘前(15:00前)运行自动丢弃当日形成中bar
输出: quant_state.json状态(含逐日权益) + quant_trades.csv流水 + quant_trades.log日志
      + 微信复盘日报 + dashboard_quant.html(持仓标的日K与操作点, 每日收盘生成)"""
import copy
import datetime
import config_quant as config
from common import notify, quotes
from core import adaptive
from core.indicators import build_ind
from quant import picker
from quant import account, dashboard, estim

KLT_DAILY, BARS_LMT = 101, 120      # 日K, 120根(覆盖MA60+动量)
_ACT_LABEL = {"SELL": ("🔴卖出", config.C_GREEN),
              "BUY_TREND": ("🟢趋势买入", config.C_RED),
              "BUY_SCALP": ("🟢超跌买入", config.C_RED),
              "BUY_ADD": ("🟢分批加仓", config.C_RED)}


def _drop_forming_bar(bars, now):
    """当日bar在15:00前为形成中, 丢弃以保证只用已收盘数据"""
    if bars and bars[-1]["dt"][:10] == now.strftime("%Y-%m-%d") and now.hour < 15:
        return bars[:-1]
    return bars


def _cancel_reason(acct, p, o):
    if p["act"] == "SELL":
        return "持股不足一手, 卖出跳过"
    if p["act"] == "BUY_SCALP" and p.get("need") is not None and o < p["need"]:
        return f"开盘{o:.3f}<确认位{p['need']:.2f}, 超跌信号取消"
    return "资金不足/不足一手, 买入放弃"


def _is_st(name):
    """死规则(2026-10-21 用户): 一切ST股(含*ST/SST等, 名称含ST) 选股与买入均排除"""
    return "ST" in (name or "").upper()


def _day_plan_pick(state, acct, code, date, fills, dry):
    """双窗口协调(2026-10-26): 该日该票信号若已被盘中runner处理(filled/cancelled)
    则返回True(引擎跳过开盘价补成交); filled仅补展示与兜底登记(资金/费用簿记已在
    盘中由runner完成, 此处登记幂等); 无记录/仍pending返回False走原开盘价兜底"""
    dp = (state.get("day_plan") or {}).get(date, {}).get(code)
    if not dp or dp.get("status") == "pending":
        return False
    rec = dp.get("rec")
    if dp.get("status") == "filled" and rec:
        fills.append(rec)               # 仅日报展示
        if rec["side"] == "SELL" and acct["sh"] == 0:
            acct["cost"] = 0.0          # 兜底登记(runner已做, 幂等); 防picker冷却失效
            state.setdefault("last_sell", {})[code] = rec["date"]
        account.append_log(f"{date} 盘中已成交(双窗口): "
                           f"{'买入' if rec['side'] == 'BUY' else '卖出'} "
                           f"{acct['name']}({code}) {rec['qty']}股@{rec['price']:.3f} "
                           f"[{dp.get('note') or ''}]", dry=dry)
    else:
        account.append_log(f"{date} 盘中已取消(双窗口): {acct['name']}({code}) "
                           f"{dp.get('act') or ''} [{dp.get('note') or ''}]", dry=dry)
    acct["pending"] = None
    return True


def _prune_day_plan(state):
    """day_plan只留最近3个交易日(防状态文件膨胀)"""
    dp = state.get("day_plan") or {}
    if len(dp) > 3:
        for k in sorted(dp)[:-3]:
            dp.pop(k)


def _advance(state, acct, code, bars, fills, dry, mkt_cl=None):
    """推进一个子账户到最新bar; 返回新处理的最大日期(无推进返回None)
    mkt_cl=指数收盘序列(与bars位置对齐, v2.2冰点左侧判定; None=该维度停用)"""
    dates = [b["dt"][:10] for b in bars]
    last = (acct.get("last_date") or "")[:10]
    if last:
        if last not in dates:
            print(f"[量化] {acct['name']}({code}) last_date={last}超出K线窗口, "
                  f"跳过(连续缺跑过久需人工检查)")
            return None
        start = dates.index(last) + 1
    else:   # 初始化后首跑: 从快照日(含)收盘开始决策
        start = next((k for k, d in enumerate(dates) if d >= state["init_date"]),
                     len(bars))
    if start >= len(bars):
        return None                        # 已是最新, 幂等跳过
    ind = build_ind(bars)
    for i in range(max(start, 1), len(bars)):
        p = acct.get("pending")            # ①昨日收盘信号 -> 盘中双窗口/今日开盘价兜底
        if _day_plan_pick(state, acct, code, dates[i], fills, dry):
            p = None                        # 盘中runner已处理(filled/cancelled), 跳过补成交
        if p and p["act"].startswith("BUY") and _is_st(acct.get("name")):
            account.append_log(f"{dates[i]} {acct['name']}({code}): ST股买入硬拦截, "
                               f"{p['act']}信号取消(死规则)", dry=dry)
            p = None
            acct["pending"] = None
        if p:
            rec = adaptive.apply_fill(acct, code, acct["name"], dates[i],
                                      bars[i]["open"], p,
                                      check_open=not p.get("o_checked"))
            if rec:
                old_sh = acct["sh"] - rec["qty"] if rec["side"] == "BUY" else 0
                if rec["side"] == "BUY" and acct["sh"] > 0:
                    acct["cost"] = round((acct["cost"] * old_sh + rec["amount"]
                                          + rec["fee"]) / acct["sh"], 4)
                elif rec["side"] == "SELL" and acct["sh"] == 0:
                    acct["cost"] = 0.0
                    state.setdefault("last_sell", {})[code] = rec["date"]
                state["cum_fee"] = state.get("cum_fee", 0) + rec["fee"]
                fills.append(rec)
                account.append_trade(rec, acct["cash"], dry=dry)
            else:
                account.append_log(f"{dates[i]} 开盘 取消 {acct['name']}({code}) "
                                   f"{p['act']}信号: {_cancel_reason(acct, p, bars[i]['open'])}",
                                   dry=dry)
                print(f"[量化] {acct['name']}({code}) {p['act']}未成交: "
                      f"{_cancel_reason(acct, p, bars[i]['open'])}")
            acct["pending"] = None
        o_nxt = bars[i + 1]["open"] if i + 1 < len(bars) else None   # ②收盘决策
        d = adaptive.decide(acct, ind, bars, i, o=o_nxt, mkt_cl=mkt_cl)
        if d and d["act"].startswith("BUY") \
                and acct["cash"] < bars[i]["close"] * config.LOT_MIN * 1.01:
            # 资金已归集入选股池的空仓账户: decide重新发出的BUY信号无资金支撑,
            # 不挂单(避免日报出现"0持股+现金0+买入信号"与选股池资金脱节的假信号),
            # 待调仓日资金重新分配后再由信号入场
            account.append_log(
                f"{dates[i]} {acct['name']}({code}) {d['act']}信号挂起: "
                f"子账户现金{acct['cash']:,.2f}不足1手(资金在选股池, 调仓日分配)",
                dry=dry)
            print(f"[量化] {acct['name']}({code}) {d['act']}信号挂起: "
                  f"现金不足1手, 待调仓日分配资金")
            d = None
        if d:
            d["signal_date"] = dates[i]
            d["o_checked"] = o_nxt is not None
            acct["pending"] = d
        acct["last_date"] = dates[i]
    return acct["last_date"]


def _mkt_gate(idx_bars, rep_date):
    """v3大盘门控(与bt_quant_v3同口径): 上证收盘 vs SMA(PICK_V3_GATE_MA)
    收盘>均线=放行新开仓; 数据缺失放行(fail-open, 同回测gm=None语义);
    v3关闭或门控停用时恒放行(返回空说明文字)"""
    if not config.PICK_V3_EN or not config.PICK_V3_GATE_MA:
        return True, ""
    w = config.PICK_V3_GATE_MA
    bars = [b for b in (idx_bars or []) if b["dt"][:10] <= (rep_date or "9999-99-99")]
    if len(bars) < w:
        return True, f"指数日K不足{len(bars)}根, 门控放行"
    cl = [b["close"] for b in bars]
    ma = sum(cl[-w:]) / w
    ok = cl[-1] > ma
    return ok, f"上证收{cl[-1]:.2f}{'>' if ok else '≤'}MA{w}({ma:.2f})"


def _sweep_cash(state):
    """清仓子账户现金归集入选股池(守护: 带未成交BUY挂单的不归集,
    防止日K缺失当天买入资金被误归集导致次日买入取消)"""
    for code, a in state["accounts"].items():
        if a["sh"] == 0 and a["cash"] > 0 and not a.get("pending"):
            state["pool_cash"] = state.get("pool_cash", 0.0) + a["cash"]
            a["cash"] = 0.0


def _rebal_trig(state, rep_date, cal):
    """常规调仓触发预判(due/early, 无网络请求): run()用其决定扫描顺序——
    常规调仓日由_rebalance内部扫描, 否则先每日扫描再判"好机会"旁路"""
    if not config.PICK_ENABLED or not cal or rep_date not in cal:
        return ""
    lr = state.get("last_rebal")
    if lr not in cal or (cal.index(rep_date) - cal.index(lr)) >= config.PICK_REBAL:
        return "due"
    n_busy = sum(1 for a in state["accounts"].values()
                 if a["sh"] > 0 or a.get("pending") or a["cash"] > 0)
    if n_busy < config.PICK_MIN_HOLD:
        return "early"
    return ""


def _is_hot(e):
    """特别好机会判定(2026-11-10 用户规则: 非常好的机会可忽略调仓日和冷却期):
    v3=候选池多因子分>=PICK_HOT_V3_SCORE; v2.5=20日动量>=PICK_HOT_MOM"""
    if config.PICK_V3_EN:
        return e.get("score") is not None and e["score"] >= config.PICK_HOT_V3_SCORE
    return e.get("mom", 0.0) >= config.PICK_HOT_MOM


def _rebalance(state, rep_date, cal, dry, idx_bars=None, scan=None):
    """调仓日: ①清仓子账户现金归集入池 ②按全局选股规则补入新标的(次日开盘买入)
    触发条件(2026-10-30 弹性调仓): 常规到期due(每PICK_REBAL个交易日) / 持股低于
    下限early(提前补仓) / 候选池特别好机会hot(2026-11-10 用户规则: 忽略调仓日与
    冷却期临时建仓, v3按多因子分PICK_HOT_V3_SCORE判定; 大盘门控关闭时hot不触发);
    在管达上限封顶; v3模式加大盘门控(上证收盘<MA20暂停新开仓, 持仓退出不受影响)
    scan: 非调仓日前置扫描的picker.select结果(好机会旁路复用, 全天仅一次全市场扫描;
    其passing已由_daily_pool_scan入池, 本函数不再重复入池/扫描)
    返回选股结果dict或None(非调仓日)"""
    _sweep_cash(state)
    if not config.PICK_ENABLED or not cal or rep_date not in cal:
        return None
    held = {c for c, a in state["accounts"].items() if a["sh"] > 0}
    # 在管子账户数(持仓/挂单/已分配资金都占位): 弹性调仓的持股口径,
    # 防止挂单未成交时被当作0持股而超额建仓(2026-10-30 用户规则)
    n_busy = sum(1 for a in state["accounts"].values()
                 if a["sh"] > 0 or a.get("pending") or a["cash"] > 0)
    trig = _rebal_trig(state, rep_date, cal)
    if not trig and config.PICK_HOT_EN and n_busy < config.PICK_MAX_HOLD:
        # 好机会(2026-10-30用户规则; 2026-11-10扩展v3+豁免冷却期):
        # 候选池Top标的达标 -> 忽略调仓日临时建仓; 门控关闭时不触发
        # (门控优先于好机会, 避免空烧调仓周期; v2.5模式_mkt_gate恒放行)
        if _mkt_gate(idx_bars, rep_date)[0]:
            top, _zt = _pool_best(state, cal, rep_date)
            if top and _is_hot(top[0]):
                trig = "hot"
    if not trig:
        return None
    state["last_rebal"] = rep_date
    if scan is not None:                        # 好机会旁路: 复用前置扫描结果
        sel = scan
    else:
        try:
            sel = picker.select(held=held, cool_dates=state.get("last_sell", {}))
        except Exception as e:
            print("[量化] 选股失败:", e)
            account.append_log(f"{rep_date} 选股失败: {e}", dry=dry)
            return None
    sel["rep_date"] = rep_date
    sel["trig"] = trig
    sel["trig_txt"] = {"due": "常规调仓",
                       "early": f"持股{n_busy}只<{config.PICK_MIN_HOLD}提前补仓",
                       "hot": "好机会临时建仓"}[trig]
    sel["invested"] = []
    sel["new_closes"] = {p["code"]: p["close"] for p in sel["picks"]}
    # 建仓目标合并: ①手动观察池(用户在holdings_editor维护, 优先分配) ②智能选股picks
    # 已在管理中(持股/挂单/候选待信号有现金)的标的跳过不重复分配
    # 手动观察池合并: 2026-10-20 撤回"共振升级"时一并停用(功能代码与 watchlist.csv 保留,
    # 编辑器页签仍在; 重新启用只需把下面 watch 换回 account.load_watchlist())
    watch = []
    sel["watchlist"] = watch
    targets = []
    # 持仓封顶(2026-10-30 用户规则): 在管达PICK_MAX_HOLD后不再新建仓, 资金留选股池
    max_new = max(0, config.PICK_MAX_HOLD - n_busy)
    for w in watch:
        if _is_st(w.get("name")):
            account.append_log(f"{rep_date} 手动观察池{w.get('name')}为ST股, "
                               f"跳过建仓(死规则)", dry=dry)
            continue
        a = state["accounts"].get(w["code"])
        if a and (a["sh"] > 0 or a.get("pending") or a["cash"] > 0):
            continue
        targets.append({"code": w["code"], "name": w["name"], "src": "手动",
                        "reason": "手动观察池" + (f"·{w['note']}" if w["note"] else ""),
                        "close": None})
    # v3大盘门控: 上证收盘<MA_N -> 本轮暂停新开仓(持仓退出不受影响, 与回测同口径)
    gate_ok, gate_txt = _mkt_gate(idx_bars, rep_date)
    sel["gate_ok"], sel["gate_txt"] = gate_ok, gate_txt
    if not gate_ok:
        account.append_log(f"{rep_date} 大盘门控关闭: {gate_txt}, 本轮暂停新开仓"
                           f"(已持仓标的退出不受影响)", dry=dry)
    if config.PICK_POOL_REAL_EN:
        # 调仓日择优建仓(2026-10-21 用户规则②): 今日扫描先入池, 再从累积候选池择优
        # (好机会旁路复用前置扫描时passing已入池, 跳过防hits双计)
        if scan is None:
            _pool_upsert(state, sel.get("passing") or [], rep_date)
        best, zt_on = _pool_best(state, cal, rep_date)
        sel["zt_mkt"] = zt_on
        if gate_ok:
            for e in best[:max(0, max_new - len(targets))]:
                if _is_st(e.get("name")) or any(t["code"] == e["code"] for t in targets):
                    continue
                sel.setdefault("new_closes", {})[e["code"]] = e["close"]
                if config.PICK_V3_EN:
                    reason = (e.get("reason")
                              or f"多因子{e.get('score', 0):+.2f}"
                              f" | 中期动量{e.get('m120', 0) * 100:+.0f}%"
                              f" | 20日{e['mom'] * 100:+.1f}%"
                              + (" | ⚠连板极端" if e.get("extreme") else ""))
                    reason += f" | 在池上榜{e.get('hits', 1)}次"
                    if trig == "hot":
                        reason += f" | ⭐特别好机会(分≥{config.PICK_HOT_V3_SCORE:.1f}" \
                                  f"豁免调仓日/冷却期)"
                else:
                    reason = (f"{e.get('board_disp') or (e['board_kind_cn'] + '·' + e['board_name'])}"
                              f"{e['board_mom'] * 100:+.1f}% | 个股动量{e['mom'] * 100:+.1f}%"
                              f" | 在池上榜{e.get('hits', 1)}次"
                              + (" | ⚠连板极端" if e.get("extreme") else ""))
                targets.append({"code": e["code"], "name": e["name"], "src": "池选",
                                "reason": reason, "close": e["close"]})
    else:
        for p in sel["picks"]:
            if len(targets) >= max_new:
                break
            if any(t["code"] == p["code"] for t in targets):
                continue
            a = state["accounts"].get(p["code"])
            if a and (a["sh"] > 0 or a.get("pending") or a["cash"] > 0):
                continue
            targets.append({"code": p["code"], "name": p["name"], "src": "选股",
                            "reason": picker.brief_reason(p), "close": p["close"]})
    if config.PICK_LIVE and targets:
        alloc = state.get("pool_cash", 0.0) / len(targets)
        for t in targets:
            code = t["code"]
            ref_close = t["close"]
            if ref_close is None:                       # 手动池标的需取现价判断一手
                try:
                    ref_close = quotes.fetch_kline_bars(code, klt=101, lmt=5)[-1]["close"]
                except Exception as e:
                    account.append_log(f"{rep_date} 观察池 {t['name']}({code}) 取价失败: {e}, "
                                       f"本次分配跳过", dry=dry)
                    continue
                sel.setdefault("new_closes", {})[code] = ref_close
            if alloc < ref_close * config.LOT_MIN * 1.01:
                continue                                # 分摊后不足1手, 放弃
            a = state["accounts"].get(code)
            if a is None:
                a = adaptive.new_leg_state(cash=0.0, shares=0, leg="trend")
                a["name"] = t["name"]
                state["accounts"][code] = a
            a["cash"] = a["cash"] + alloc
            state["pool_cash"] = state.get("pool_cash", 0.0) - alloc
            a["last_date"] = rep_date
            a["pool"] = True
            a["src"] = t["src"]
            if config.PICK_RESON_EN:
                # 共振模式: 入选=入池等信号, 不直接挂单;
                # 之后每日收盘由decide按v2.2(趋势/超跌/冰点)择机, 退出=趋势腿破MA10
                a["pending"] = None
            else:
                a["pending"] = {"act": "BUY_TREND", "signal_date": rep_date,
                                "o_checked": True, "reason": "选股入选"}
            sel["invested"].append({"code": code, "name": t["name"], "src": t["src"],
                                    "alloc": alloc, "reason": t["reason"]})
            account.append_log(
                f"{rep_date} {t['src']}入池 {t['name']}({code}): {t['reason']}; "
                f"分配资金{alloc:,.2f}元, "
                + ("待v2.2信号择机入场" if config.PICK_RESON_EN else "次日开盘买入"), dry=dry)
    btxt = (f"行业板块{sel.get('board_n', 0)}个(全市场)" if sel.get("v3")
            else f"热门板块{len(sel['boards'])}个")
    gtxt = (f", 门控{'放行' if gate_ok else '关闭(' + gate_txt + ')'}"
            if config.PICK_V3_EN and config.PICK_V3_GATE_MA else "")
    account.append_log(
        f"{rep_date} 选股扫描({sel['trig_txt']}, 在管{n_busy}/{config.PICK_MAX_HOLD}): "
        f"{btxt}{gtxt}, 候选{sel['cand_n']}只, "
        f"过门槛{len(sel['passing'])}只, 入选{len(sel['picks'])}只, "
        f"观察池{len(watch)}只, 实际建仓{len(sel['invested'])}只, "
        f"选股池余额{state.get('pool_cash', 0.0):,.2f}",
        dry=dry)
    return sel


def _pool_entry(p, rep_date):
    """候选池条目(剥离bars大对象, 只留展示/排序字段; v4含多因子分/因子值/截面z分)"""
    return {"code": p["code"], "name": p["name"],
            "board_kind_cn": p.get("board_kind_cn", ""),
            "board_name": p.get("board_name", ""),
            "board_disp": p.get("board_disp", ""),
            "board_mom": p.get("board_mom", 0.0),
            "mom": p["mom"], "close": p["close"],
            "score": p.get("score"), "m120": p.get("m120"),
            "mom20": p.get("mom20"),
            "r5": p.get("r5"), "bias": p.get("bias"), "vol20": p.get("vol20"),
            "vr": p.get("vr"),
            "z_m120": p.get("z_m120"), "z_mom20": p.get("z_mom20"),
            "z_vol20": p.get("z_vol20"), "z_bias": p.get("z_bias"),
            "z_vr": p.get("z_vr"),
            "brank": p.get("brank"), "z_brank": p.get("z_brank"),
            "first_close": p["close"],          # v5: 入池首日收盘价(入池后涨跌基准)
            "res1": p.get("res1"), "res2": p.get("res2"),   # v5: 压力位/强压力
            "sup1": p.get("sup1"), "sup2": p.get("sup2"),   # v5: MA20支撑/强支撑
            "pct_chg": p.get("pct_chg"),        # v5: 当日涨跌
            "reason": p.get("reason", ""),
            "extreme": p.get("extreme", False),
            "first_date": rep_date, "last_date": rep_date, "hits": 1}


def _pool_upsert(state, passing, rep_date):
    """过门槛候选入池/刷新(动量/close/板块/extreme/多因子分/因子值/截面z分更新,
    hits累计上榜次数)"""
    pool = state.setdefault("cand_pool", {})
    for p in passing:
        c = p["code"]
        if c in pool:
            e = pool[c]
            e.update({"mom": p["mom"], "close": p["close"],
                      "board_mom": p.get("board_mom", e.get("board_mom", 0.0)),
                      "board_name": p.get("board_name", e.get("board_name", "")),
                      "board_kind_cn": p.get("board_kind_cn",
                                             e.get("board_kind_cn", "")),
                      "board_disp": p.get("board_disp", e.get("board_disp", "")),
                      "extreme": p.get("extreme", e.get("extreme", False)),
                      "score": p.get("score", e.get("score")),
                      "m120": p.get("m120", e.get("m120")),
                      "mom20": p.get("mom20", e.get("mom20")),
                      "r5": p.get("r5", e.get("r5")),
                      "bias": p.get("bias", e.get("bias")),
                      "vol20": p.get("vol20", e.get("vol20")),
                      "vr": p.get("vr", e.get("vr")),
                      "z_m120": p.get("z_m120", e.get("z_m120")),
                      "z_mom20": p.get("z_mom20", e.get("z_mom20")),
                      "z_vol20": p.get("z_vol20", e.get("z_vol20")),
                      "z_bias": p.get("z_bias", e.get("z_bias")),
                      "z_vr": p.get("z_vr", e.get("z_vr")),
                      "brank": p.get("brank", e.get("brank")),
                      "z_brank": p.get("z_brank", e.get("z_brank")),
                      "pct_chg": p.get("pct_chg", e.get("pct_chg")),
                      "res1": p.get("res1", e.get("res1")),
                      "res2": p.get("res2", e.get("res2")),
                      "sup1": p.get("sup1", e.get("sup1")),
                      "sup2": p.get("sup2", e.get("sup2")),
                      "reason": p.get("reason", e.get("reason", "")),
                      "last_date": rep_date})
            e["hits"] += 1
        else:
            pool[c] = _pool_entry(p, rep_date)


def _pool_cool(state, cd, cal, rep_date):
    """候选池标的冷却判定(卖出后PICK_COOLDOWN个交易日内禁再入选)"""
    sd = state.get("last_sell", {}).get(cd)
    return bool(sd and cal and sd in cal and rep_date in cal
                and (cal.index(rep_date) - cal.index(sd)) < config.PICK_COOLDOWN)


def _pool_best(state, cal, rep_date, extra_exclude=()):
    """候选池择优: 排除持仓/挂单/有现金/冷却/额外排除, 取前PICK_TOPK
    v3口径(与回测一致): 只认当日刷新的候选(全市场每日重扫, 无需跨日累积),
    按多因子分排序(极端沉底), 连板行情判定停用
    冷却豁免(2026-11-10 用户规则): 特别好机会(_is_hot达标)忽略冷却期参与择优"""
    v3 = config.PICK_V3_EN
    zt_on = (picker.limitup_market_on()
             if config.PICK_EXTREME_EN and not v3 else False)
    out = []
    for c, e in state.get("cand_pool", {}).items():
        if _is_st(e.get("name")):
            continue                            # 死规则: ST股不入建仓目标
        if v3 and e.get("last_date") != rep_date:
            continue                            # v3: 只用当日快照(同回测"当日cands")
        a = state["accounts"].get(c)
        if a and (a["sh"] > 0 or a.get("pending") or a["cash"] > 0):
            continue
        if c in extra_exclude:
            continue
        if _pool_cool(state, c, cal, rep_date) and not _is_hot(e):
            continue                            # 冷却中, 除非特别好机会豁免
        out.append(e)
    if v3:
        out.sort(key=lambda x: (x.get("extreme", False),
                                -(x["score"] if x.get("score") is not None
                                  else x["mom"])))
    else:
        ex_first = (not config.PICK_EXTREME_EN) or zt_on
        out.sort(key=lambda x: ((not x.get("extreme", False)) if ex_first
                                else (x.get("extreme", False)), -x["mom"]))
    return out[:config.PICK_TOPK], zt_on


def _daily_pool_scan(state, rep_date, cal, dry, sel=None):
    """每日候选观察池扫描(用户规则②: 随时选股累积入池, 调仓日池内择优建仓)
    ①超龄出池: last_date距今≥PICK_POOL_KEEP个交易日(无日历则清空重建)
    ②扫描: 调仓日复用_rebalance结果(择优建仓模式下其已入池, 不重复);
        非调仓日独立跑picker.select(失败容错跳过)
    ③入池/刷新: 过门槛候选更新动量与last_date, hits累计上榜次数
    ④每日计算池内择优(与建仓同口径): 调仓日按此实际建仓, 非调仓日日报展示
    返回 (snapshot, obs_best, zt_on, res); res=本轮picker.select原始结果
    (非调仓日好机会旁路时传给_rebalance复用, 全天仅一次全市场扫描), 未扫描为None;
    总开关关闭时返回 (None, None, None, None)"""
    if not (config.PICK_DAILY_EN or config.PICK_POOL_OBS):
        return None, None, None, None
    res = None
    pool = state.setdefault("cand_pool", {})
    for c in list(pool):                            # 死规则: ST股清除出候选池
        if _is_st(pool[c].get("name")):
            del pool[c]
    if cal and rep_date in cal:
        ri = cal.index(rep_date)
        for c in list(pool):
            ld = pool[c]["last_date"]
            if ld not in cal or ri - cal.index(ld) >= config.PICK_POOL_KEEP:
                del pool[c]                      # 超龄未再上榜, 出池
    else:
        pool.clear()                             # 无交易日历无法判保鲜, 清空重建
    if sel is not None:
        passing = [] if config.PICK_POOL_REAL_EN else (sel.get("passing") or [])
    elif config.PICK_DAILY_EN:
        held = {c for c, a in state["accounts"].items() if a["sh"] > 0}
        try:
            res = picker.select(held=held, cool_dates=state.get("last_sell", {}))
            passing = res.get("passing") or []
            if res and res.get("note"):
                print("[量化] 候选池扫描未完成:", res["note"])
                account.append_log(f"{rep_date} 候选池扫描未完成: "
                                   f"{res['note']}", dry=dry)
            elif res is not None and res.get("cand_n") == 0:
                # 盘前东财clist实时价格字段为空 -> 成分股全被过滤, 宇宙为0
                print("[量化] 候选池扫描宇宙为空(疑似盘前无实时行情), "
                      "本轮池未刷新")
                account.append_log(f"{rep_date} 候选池扫描宇宙为空"
                                   f"(疑似盘前无实时行情), 池未刷新", dry=dry)
        except Exception as e:
            print("[量化] 每日候选池扫描失败:", e)
            account.append_log(f"{rep_date} 候选池扫描失败: {e}", dry=dry)
            passing = []
    else:
        passing = []
    _pool_upsert(state, passing, rep_date)
    # 每日都计算池内择优(调仓日按此实际建仓; 非调仓日供日报展示当前Top)
    invested = {x["code"] for x in (sel or {}).get("invested", [])}
    obs_best, zt_on = _pool_best(state, cal, rep_date, extra_exclude=invested)
    if config.PICK_V3_EN:
        snap = sorted(pool.values(),
                      key=lambda x: -(x["score"] if x.get("score") is not None
                                      else -1e9))[:config.PICK_POOL_TOPN]
    else:
        snap = sorted(pool.values(), key=lambda x: -x["mom"])[:config.PICK_POOL_TOPN]
    account.append_log(
        f"{rep_date} 候选观察池: 在池{len(pool)}只, 今日过门槛{len(passing)}只"
        f", 池内择优{[e['name'] for e in obs_best]}"
        f"({'连板行情' if zt_on else '非连板行情'})", dry=dry)
    return snap, obs_best, zt_on, res


def _sig_cell(acct):
    p = acct.get("pending")
    if not p:
        return notify.colored("持有" if acct["sh"] > 0 else "观望", config.C_GRAY)
    label, color = _ACT_LABEL.get(p["act"], (p["act"], config.C_GRAY))
    if p["act"] == "BUY_SCALP" and p.get("reason") == "冰点左侧":
        label = "🟢冰点买入"                    # v2.2规则A(同为scalp腿, 标签区分)
    txt = label
    if p["act"] == "BUY_SCALP" and p.get("need") is not None and not p.get("o_checked"):
        txt += f"(开盘≥{p['need']:.2f}确认)"
    return notify.colored(txt, color) + f" {p.get('reason', '')}"


def _rebal_info(state, cal, rep_date):
    """智能选股调仓节奏信息(日报/看板非调仓日说明 + 持仓计划末尾复用)
    返回: pick_enabled / 调仓间隔 / 上次调仓日 / 距下次交易日数 / 选股池现金 / 冷却天数
          / 在管持股数与上下限(2026-10-30弹性调仓: 低于下限提前补仓, 达上限封顶)"""
    lr = state.get("last_rebal")
    left_rb = None
    if cal and lr in cal and rep_date in cal:
        left_rb = config.PICK_REBAL - (cal.index(rep_date) - cal.index(lr))
        if left_rb < 0:
            left_rb = 0
    n_hold = sum(1 for a in state["accounts"].values()
                 if a["sh"] > 0 or a.get("pending") or a["cash"] > 0)
    return {"pick_enabled": config.PICK_ENABLED,
            "rebal_interval": config.PICK_REBAL,
            "last_rebal": lr or "",
            "days_left": left_rb,
            "pool_cash": state.get("pool_cash", 0.0),
            "cooldown": config.PICK_COOLDOWN,
            "n_hold": n_hold,
            "min_hold": config.PICK_MIN_HOLD,
            "max_hold": config.PICK_MAX_HOLD}


def _money(x):
    return f"{x / 10000:.2f}万" if abs(x) >= 10000 else f"{x:,.0f}元"


def _br(*parts):
    """多段文本换行拼接(2026-11-21格式改造: 弃用';'堆叠, 每段独立成行)"""
    return "<br>".join(p for p in parts if p)


def _kind_badge(kind):
    """出场线图标徽章: 💰止盈(红) / 🛑止损·破位(绿), 邮件与dashboard通用"""
    if "止盈" in kind:
        return "💰 " + notify.colored(notify.bold(kind), config.C_RED)
    if "止损" in kind or "破位" in kind:
        return "🛑 " + notify.colored(notify.bold(kind), config.C_GREEN)
    return kind


def _tier_table_html(ep):
    """阶梯止盈分档明细(嵌套表, 日报邮件与dashboard共用同一份plans行, 2026-11-21)"""
    sh = ep["sh"]
    heads = ["浮盈档", "回撤带宽", "升档条件(收盘)", "触发线", "卖出", "约回笼(费前)"]
    h = ("<table style='border-collapse:collapse;margin:2px 0;'><tr>"
         + "".join(f"<th style='border:1px solid #8a94a6;background:#3f4a5a;color:#fff;"
                   f"padding:2px 6px;font-size:11px;white-space:nowrap;'>{t}</th>"
                   for t in heads) + "</tr>")
    for t in ep["tiers"]:
        cells = ["💰 " + t["band"].replace("<", "&lt;") + ("·当前" if t["cur"] else ""),
                 f"{t['bw'] * 100:.0f}%",
                 (f"≥{t['enter']:.2f}" if t["enter"] else "默认档"),
                 f"{'≈' if t['est'] else ''}{t['line']:.2f}",
                 f"{sh}股(全清)", _money(sh * t["line"])]
        if t["cur"]:
            cells = [notify.colored(notify.bold(c), config.C_RED) for c in cells]
        h += ("<tr>" + "".join(
            f"<td style='border:1px solid #8a94a6;padding:2px 6px;font-size:11px;"
            f"white-space:nowrap;'>{c}</td>" for c in cells) + "</tr>")
    h += "</table>"
    h += notify.colored(_br("· 触发线随持仓最高收盘上移",
                            "· 非当前档按升档进入价估算(标≈)",
                            "· 升档带宽放宽 = 让利润奔跑",
                            "· 任一价触发均为一次性全清"), config.C_GRAY)
    return h


def _exit_plan_rows(nm, ep):
    """一只持仓股 -> 多行: 每条出场线一行(触发价/卖出股数/预计回笼) + 阶梯止盈分档明细行
    ep=adaptive.build_exit_plan, lines已按触发价降序(首行=最可能先触发)
    2026-11-21格式改造: 出场线带💰止盈(红)/🛑止损(绿)徽章; 触发条件逐行排列"""
    sh, rows = ep["sh"], []
    for i, l in enumerate(ep["lines"]):
        pr = l.get("price")
        rows.append([nm if i == 0 else "",
                     notify.colored("⚪持有", config.C_GRAY) if i == 0 else "",
                     _kind_badge(l["kind"]),
                     notify.colored(notify.bold(f"{pr:.2f}"), config.C_GREEN) if pr else "-",
                     f"{sh}股(全清)" if pr else "-",
                     _money(sh * pr) if pr else "-",
                     _br(f"收盘跌破 → 次日开盘离场",
                         f"· {l['note']}",
                         "· 三线同时在场, 先到先卖" if i == 0 else "",
                         "· 未破线继续持有" if i == 0 else "")])
    if ep["tiers"]:
        rows.append(["", "", notify.colored("阶梯止盈分档", config.C_GRAY), "-", "-", "-",
                     _tier_table_html(ep)])
    return rows


def _build_plans(state, bars_map, cal, rep_date):
    """每只股票日后操作计划(日报"本次成交"节之后展示)
    2026-11-21结构化改造(用户要求"加列+分档列出+触发条件更有格式"):
      7列=标的/明日动作/出场线事项/触发价/触发卖出/预计回笼/触发条件与分档明细
      持仓无挂单-> adaptive.build_exit_plan 每条出场线一行(吊灯/阶梯止盈/阶梯止损),
      追加阶梯止盈三档分位明细嵌套表(每档触发线+卖出股数+约回笼); 超跌腿规则分行
    空仓: 冷却中的已清仓标的合并一行; 末尾附选股池现金与调仓倒计时"""
    rows, cool_names = [], []
    last_sell = state.get("last_sell", {})
    for code in sorted(state["accounts"]):
        a = state["accounts"][code]
        nm = f"{a['name']}({code})"
        if a.get("src") == "手动":
            nm += " [手动]"
        elif a.get("pool"):
            nm += " [选股]"
        p = a.get("pending")
        if a["sh"] > 0:
            if p and p["act"] == "SELL":
                rows.append([nm, notify.colored("🔴卖出", config.C_GREEN),
                             "已挂卖出单", "明日开盘价", f"{a['sh']}股(全部)", "按开盘价成交",
                             _br(p.get('reason', ''),
                                 "回款留存本子账户",
                                 "调仓日归集入选股池")])
                continue
            if p and p["act"] == "BUY_ADD":
                rows.append([nm, notify.colored("🟢加仓", config.C_RED),
                             "补足仓位", "明日开盘价", "按待加仓额度",
                             f"约{a.get('add', 0):,.0f}元",
                             _br("首笔后收盘站上MA5确认",
                                 "跌破MA10仍按趋势腿离场")])
                continue
            bars = bars_map.get(code)
            ep = adaptive.build_exit_plan(a, bars)
            if ep:
                rows.extend(_exit_plan_rows(nm, ep))
                continue
            if not a.get("sh_tr", True):
                cond = _br("💰 反弹至布林中轨 → 止盈全清",
                           f"🛑 收盘跌破信号低点{a.get('sig_low', 0):.2f} → 止损全清",
                           "⏰ 持仓满8日 → 超时离场")
                rows.append([nm, notify.colored("⚪持有", config.C_GRAY),
                             "超跌腿出场", "-", f"{a['sh']}股(全清)", "-", cond])
                continue
            miss = ("吊灯线(22日最高收盘-2×ATR)"
                    if config.PICK_V3_EN and config.PICK_V3_EXIT == "chand" else "MA10")
            rows.append([nm, notify.colored("⚪持有", config.C_GRAY), "🛑 趋势破位离场",
                         "-", f"{a['sh']}股(全清)", "-",
                         _br(f"收盘跌破{miss} → 次日开盘离场",
                             "· 行情缺失, 未能计算临界价")])
        elif p and p["act"].startswith("BUY"):
            if p["act"] == "BUY_SCALP" and p.get("need") and not p.get("o_checked"):
                txt = f"明日开盘≥{p['need']:.2f}确认才买入，否则信号取消"
            else:
                txt = "明日开盘按子账户资金买入"
            rows.append([nm, notify.colored("🟢买入", config.C_RED), "开仓",
                         "明日开盘价", "-", f"可用{a['cash']:,.0f}元",
                         f"{txt}（{p.get('reason', '')}）"])
        elif a.get("pool") and a["cash"] > 0:
            # 共振模式候选: 已分配资金等v2.2信号择机入场
            rows.append([nm, notify.colored("⏳候选", config.C_GRAY), "等待入场信号",
                         "-", "-", f"已分配{a['cash']:,.2f}元",
                         _br("等待v2.2信号（趋势站上/超跌/冰点左侧）择机入场",
                             "下个调仓日仍无信号则资金回收重选")])
        elif code in last_sell:
            sd = last_sell[code]
            left = (config.PICK_COOLDOWN - (cal.index(rep_date) - cal.index(sd))
                    if cal and sd in cal and rep_date in cal else None)
            cool_names.append(a["name"] + (f"(剩{left}日)" if left and left > 0 else ""))
    if cool_names:
        rows.append([notify.colored("已清仓标的", config.C_GRAY),
                     notify.colored("⏸冷却", config.C_GRAY), "禁止买回", "-", "-", "-",
                     _br("、".join(cool_names),
                         f"卖出后{config.PICK_COOLDOWN}个交易日内禁止买回(特别好机会可豁免)",
                         "回款已在调仓日归集或待归集")])
    lr = state.get("last_rebal")
    n_hold = sum(1 for a in state["accounts"].values()
                 if a["sh"] > 0 or a.get("pending") or a["cash"] > 0)
    if cal and lr in cal and rep_date in cal:
        left_rb = config.PICK_REBAL - (cal.index(rep_date) - cal.index(lr))
        if n_hold < config.PICK_MIN_HOLD:
            rb_txt = (f"持股{n_hold}只低于下限{config.PICK_MIN_HOLD}, "
                      f"下个交易日收盘后提前调仓补足(不等满{config.PICK_REBAL}日)")
        else:
            rb_txt = f"距下次调仓约{max(left_rb, 0)}个交易日"
    else:
        rb_txt = "调仓日期待定"
    pool_notes = [rb_txt,
                  f"常规每{config.PICK_REBAL}个交易日一次 · 持股目标"
                  f"{config.PICK_MIN_HOLD}~{config.PICK_MAX_HOLD}只 · 达上限封顶"]
    if config.PICK_HOT_EN:
        th = (f"多因子分≥{config.PICK_HOT_V3_SCORE:.1f}" if config.PICK_V3_EN
              else f"20日动量≥{config.PICK_HOT_MOM * 100:.0f}%")
        pool_notes.append(f"🔥 候选池特别好机会({th})可忽略调仓日与冷却期临时建仓")
    if config.PICK_V3_EN:
        pool_notes.append("🚦 大盘门控: 上证收盘<MA20时暂停新开仓")
    rows.append([notify.colored("选股池", config.C_GRAY),
                 notify.colored("💰资金", config.C_GRAY), "调仓与建仓", "-", "-",
                 f"现金{state.get('pool_cash', 0.0):,.2f}元",
                 _br(*pool_notes)])
    return rows


def _sel_section_v3(sel, state):
    """日报"智能选股·v3多因子"节(2026-11-04回测重构): 门控状态+候选因子表+剔除统计"""
    body = notify.sec(f"智能选股·v3多因子({sel['rep_date']} 调仓"
                      + (f"·{sel['trig_txt']}" if sel.get("trig_txt") else "") + ")")
    if sel.get("gate_txt"):
        ok = sel.get("gate_ok", True)
        body += notify.colored(
            f"大盘门控: {sel['gate_txt']} → "
            + ("✅放行新开仓" if ok else "⛔暂停新开仓(持仓退出不受影响)"),
            config.C_RED if ok else config.C_GREEN)
    if sel.get("note"):
        body += " " + notify.colored(sel["note"], config.C_GRAY)
    rows = []
    for p in sel["passing"][:6]:
        inv = next((x for x in sel["invested"] if x["code"] == p["code"]), None)
        sc = (f"{p['score']:+.2f}" if p.get("score") is not None else "-")
        rows.append([f"{p['name']}({p['code']})",
                     notify.colored(sc, config.C_RED),
                     f"{p.get('m120', 0) * 100:+.0f}%",
                     f"{p['mom'] * 100:+.1f}%",
                     f"{p.get('bias', 0) * 100:+.1f}%",
                     f"{p.get('vol20', 0) * 100:.1f}%",
                     f"{p['close']:.3f}",
                     notify.colored(f"入池 {inv['alloc']:,.0f}元", config.C_RED)
                     if inv else notify.colored("候选", config.C_GRAY)])
    if rows:
        body += notify.table(["候选股票", "多因子分", "中期动量120日", "20日涨幅",
                              "乖离MA20", "波动率", "现价", "处理"], rows)
    else:
        body += notify.colored("无通过v3门槛的候选", config.C_GRAY)
    for p in sel["passing"][:6]:
        if p.get("reason"):
            body += notify.colored(f"{p['name']}: {p['reason']}", config.C_GRAY)
    sk = sel["skip"]
    body += notify.colored(
        f"候选{sel['cand_n']}只({sel.get('board_n', 0)}个行业板成分股) | "
        f"无权限{sk.get(config.PICK_EXCLUDE_NAME, 0)} ST{sk.get('ST', 0)} "
        f"日K不足{sk.get('日K不足', 0)} 低价{sk.get('低价', 0)} "
        f"流动性不足{sk.get('流动性不足', 0)} 行情失败{sk.get('行情失败', 0)} "
        f"门槛未达{sk.get('门槛未达', 0)} 冷却中{sk.get('冷却中', 0)} | "
        f"门槛=趋势内回调(收盘>MA20/60且MA20上行, 120日跳段动量≥0, 乖离≤15%, "
        f"5日≤6%, 20日≤45%) | 打分=0.5×中期动量+1×短期反转+1×低波+0.5×回踩乖离 | "
        f"冷却{config.PICK_COOLDOWN}日禁再买(特别好机会"
        f"(分≥{config.PICK_HOT_V3_SCORE:.1f})可豁免) | "
        f"选股池现金{state.get('pool_cash', 0.0):,.2f}元", config.C_GRAY)
    return body


def _sel_section(sel, state):
    """日报"智能选股"节(兼容旧动量模式/短线共振模式/v3多因子模式)"""
    if not sel:
        return notify.sec("智能选股") + notify.colored(
            "本轮非调仓日或选股未启用(PICK_ENABLED)", config.C_GRAY)
    if sel.get("v3"):
        return _sel_section_v3(sel, state)
    reson = sel.get("reson")
    title = "智能选股·短线共振" if reson else "智能选股"
    body = notify.sec(f"{title}({sel['rep_date']} 调仓"
                      + (f"·{sel['trig_txt']}" if sel.get("trig_txt") else "") + ")")
    if reson and sel.get("mkt"):
        m = sel["mkt"]
        regime = (f"大盘: 收盘{m['close']:.2f} {'>' if m['ok'] else '≤'} "
                  f"MA20({m['ma']:.2f}), 近5日{m['r5'] * 100:+.2f}% → "
                  + ("✅可做多环境" if m["ok"] else "⛔弱势暂停"))
        body += notify.colored(regime, config.C_RED if m["ok"] else config.C_GREEN)
    if sel.get("note"):
        body += " " + notify.colored(sel["note"], config.C_GRAY)
    if sel["boards"]:
        mom_head = "板块5日涨幅" if reson else "20日动量"
        body += notify.table(
            ["类别", "热门板块", "当日涨幅", mom_head, "板块成交额"],
            [[b["kind_cn"], b["name"], f"{b['pct']:+.2f}%",
              notify.colored(f"{b['mom'] * 100:+.1f}%", config.C_RED),
              f"{b['amount'] / 1e8:,.1f}亿"] for b in sel["boards"]])
    elif reson:
        body += notify.colored("无共振板块(板块5日涨幅>0且跑赢大盘)", config.C_GRAY)
    rows = []
    for p in sel["passing"][:6]:
        inv = next((x for x in sel["invested"] if x["code"] == p["code"]), None)
        r = [f"{p['name']}({p['code']})",
             f"{p['board_kind_cn']}·{p['board_name']}",
             f"{p['board_mom'] * 100:+.1f}%",
             notify.colored(f"{p['mom'] * 100:+.1f}%", config.C_RED)]
        if reson:
            r.append(f"{p.get('amp', 0) * 100:.0f}%")
        r.append(f"{p['close']:.3f}")
        r.append(notify.colored(f"入池 {inv['alloc']:,.0f}元", config.C_RED)
                 if inv else notify.colored("候选", config.C_GRAY))
        rows.append(r)
    if rows:
        heads = ["候选股票", "所属板块", "板块涨幅", "个股20日动量"]
        if reson:
            heads.append("20日振幅")
        heads += ["现价", "处理"]
        body += notify.table(heads, rows)
        if reson:
            body += notify.colored("入池≠买入: 每日收盘按v2.2信号(趋势/超跌/冰点)择机入场, "
                                   "退出=趋势腿破MA10", config.C_GRAY)
    else:
        body += notify.colored("无通过门槛的候选", config.C_GRAY)
    # 手动观察池一节(有内容才展示)
    wl = sel.get("watchlist") or []
    if wl:
        body += notify.table(
            ["手动观察池", "备注", "处理"],
            [[f"{w['name']}({w['code']})", w["note"] or "-",
              notify.colored(
                  next((f"入池 {x['alloc']:,.0f}元" for x in sel["invested"]
                        if x["code"] == w["code"]), "已在管理中"), config.C_RED)]
             for w in wl])
    sk = sel["skip"]
    stat = (f"候选{sel['cand_n']}只(已剔除无权限板块) | "
            f"无权限(科创/北交){sk.get(config.PICK_EXCLUDE_NAME, 0)} "
            f"ST{sk.get('ST', 0)} 日K不足{sk.get('日K不足', 0)} "
            f"低价{sk.get('低价', 0)} 流动性不足{sk.get('流动性不足', 0)} "
            f"行情失败{sk.get('行情失败', 0)} ")
    if reson:
        stat += (f"换手不足{sk.get('换手不足', 0)} 振幅不足{sk.get('振幅不足', 0)} "
                 f"动量不足{sk.get('动量不足', 0)} ")
    else:
        stat += f"趋势未达标{sk.get('趋势未达标', 0)} "
    stat += (f"冷却中{sk.get('冷却中', 0)} | "
             f"选股池现金{state.get('pool_cash', 0.0):,.2f}元 "
             f"(常规每{config.PICK_REBAL}个交易日调仓, 持股目标"
             f"{config.PICK_MIN_HOLD}~{config.PICK_MAX_HOLD}只, "
             f"卖出标的冷却{config.PICK_COOLDOWN}日禁再买, 特别好机会可豁免)")
    body += notify.colored(stat, config.C_GRAY)
    return body


# v4因子展示定义: (字段key, 展示名, 权重key, 原始值格式)
_V4_FACTS = (("m120", "中期动量", "mom", "{:+.0%}"),
             ("mom20", "短期反转", "rev", "{:+.0%}"),
             ("vol20", "低波动", "vol", "{:.1%}"),
             ("bias", "回踩位置", "pull", "{:+.1%}"),
             ("vr", "量比", "vr", "{:.2f}"))
# v5六因子展示(2026-11-19 brank采纳): v4内核五因子原样 + 行业分位
_V5_FACTS = _V4_FACTS + (("brank", "行业分位", "brank", "{:.0%}"),)


def _zbar_html(z, half=30, vmax=2.5, h=9):
    """z-score双侧图形条(邮件HTML安全, 仿图片'候选评分总览'):
    中心线左负(绿)右正(红), |z|超vmax截断; z缺失画空条"""
    z = 0.0 if z is None else max(-vmax, min(vmax, z))
    pct = z / vmax
    neg = int(round(half * -pct)) if pct < 0 else 0
    pos = int(round(half * pct)) if pct > 0 else 0

    def _td(bg, w):
        return (f"<td bgcolor='{bg}' width='{w}' height='{h}' "
                f"style='font-size:0;line-height:{h}px'></td>")

    return ("<table cellspacing='0' cellpadding='0' "
            "style='border-collapse:collapse;display:inline-block;"
            "vertical-align:middle'><tr>"
            + _td("#eaf3ea", half - neg) + _td("#00a800", neg)
            + _td("#666666", 1) + _td("#e60000", pos)
            + _td("#f7e8e8", half - pos) + "</tr></table>")


def _zcell(z):
    """主表z分单元格: 红=正贡献 绿=负贡献 灰=中性/缺失"""
    if z is None:
        return notify.colored("-", config.C_GRAY)
    c = (config.C_RED if z > 0.05 else
         config.C_GREEN if z < -0.05 else config.C_GRAY)
    return notify.colored(f"{z:+.2f}", c)


def _v4_pos_tag(e):
    """位置档位标签(仿图片): 按收盘距MA20乖离分档"""
    b = e.get("bias")
    if b is None:
        return ""
    if b <= 0.05:
        return "[回踩确认]"
    return "[中位]" if b <= 0.10 else "[近新高]"


def _scorecard(e):
    """单候选逐因子打分明细表(仿图片'候选评分总览'):
    原始值 + z分图形条 + 截面z分 + 权重 + 贡献分, 合计=综合分
    v5打分开启时六因子(+行业分位brank), 否则v4五因子(归档口径)"""
    v5s = config.PICK_V5_EN and config.PICK_V5_SCORE_EN
    w = config.PICK_V5_W if v5s else config.PICK_V3_W
    rows = []
    for fk, fn, wk, fmt in (_V5_FACTS if v5s else _V4_FACTS):
        raw, z = e.get(fk), e.get("z_" + fk)
        raw_txt = fmt.format(raw) if raw is not None else "-"
        wt = w.get(wk, 0.0)
        if z is None:
            zc, cc = notify.colored("-", config.C_GRAY), "-"
        else:
            zc = _zcell(z)
            cc = notify.colored(f"{wt * z:+.2f}",
                                config.C_RED if wt * z > 0 else config.C_GREEN)
        rows.append([fn, raw_txt, _zbar_html(z), zc, f"{wt:g}", cc])
    total = e.get("score")
    rows.append([notify.bold("综合分"), "", "", "", "",
                 notify.bold(f"{total:+.2f}") if total is not None else "-"])
    return notify.table(["因子", "原始值", "评分图形", "截面z分", "权重", "贡献分"],
                        rows)


def _pool_section(obs, state, cal, rep_date, menv=None):
    """日报"候选观察池"节(用户规则②③: 每日扫描累积入池, 每日给出池内择优,
    调仓日按池内择优实际建仓; 排序方向看连板行情)
    v5估算层(2026-11-19): 主表新增当日情况/支撑压力/入池涨跌列, 择优Top3附入仓时机;
    PICK_V5_EN=False时回纯v4展示口径(位置标签挂名称, 无价位/时机)"""
    snap, obs_best, zt_on = obs[0], obs[1], obs[2]
    v5 = config.PICK_V5_EN
    v5s = v5 and config.PICK_V5_SCORE_EN   # v5打分(brank第六因子)开启
    gate_open = True                       # v5门控状态(从menv大盘快照推, 缺失fail-open)
    if v5 and config.PICK_V3_EN and config.PICK_V3_GATE_MA and menv:
        ix = menv.get("idx") or {}
        if ix.get("ma20") and ix.get("close"):
            gate_open = ix["close"] > ix["ma20"]
    body = notify.sec("候选观察池(每日累积 · 调仓日择优建仓)")
    if not snap:
        return body + notify.colored(
            "今日扫描无过门槛候选(池子为空); 保鲜期%d个交易日, 超龄自动出池"
            % config.PICK_POOL_KEEP, config.C_GRAY)
    held = {c for c, a in state["accounts"].items() if a["sh"] > 0}
    v3 = config.PICK_V3_EN
    rows = []
    for e in snap[:12]:
        nm = f"{e['name']}({e['code']})"
        if v3 and e.get("score") is not None and not v5:
            nm += notify.colored(_v4_pos_tag(e), config.C_GRAY)   # v4归档口径
        if e["code"] in held:
            nm += notify.colored(" [已持仓]", config.C_GRAY)
        if _pool_cool(state, e["code"], cal, rep_date):
            hot_tag = "⭐豁免" if _is_hot(e) else "冷却"
            nm += notify.colored(f" [{hot_tag}]", config.C_GRAY)
        if e.get("extreme"):
            nm += notify.colored(" ⚠连板极端", config.C_ORANGE)
        days = (cal.index(rep_date) - cal.index(e["first_date"])
                if cal and e["first_date"] in cal and rep_date in cal else None)
        sc = (notify.colored(f"{e['score']:+.2f}", config.C_RED)
              if v3 and e.get("score") is not None
              else notify.colored(f"{e['mom'] * 100:+.1f}%", config.C_RED))
        r = [nm, e.get("board_disp")
             or f"{e['board_kind_cn']}·{e['board_name']}", sc]
        if v3:
            r += [_zcell(e.get("z_" + k)) for k in
                  (("m120", "mom20", "vol20", "bias", "vr", "brank") if v5s
                   else ("m120", "mom20", "vol20", "bias", "vr"))]
        r += [f"{e['close']:.3f}"]
        if v5:
            # 当日情况列(仿图片位置列): 位置档位+当日涨跌+量能状态
            bits = []
            st = estim.pos_state(e)
            if st:
                bits.append(f"[{st}]")
            pc = e.get("pct_chg")
            if pc is not None:
                bits.append(notify.colored(
                    f"{pc:+.1%}", config.C_RED if pc > 0 else config.C_GREEN))
            vs = estim.vol_state(e)
            if vs:
                bits.append(vs)
            r.append(" ".join(bits) or "-")
            # 支撑/压力列: 绿支撑(MA20) 红压力(20日高)
            sup1, res1 = e.get("sup1"), e.get("res1")
            r.append((notify.colored(f"{sup1:.2f}", config.C_GREEN) if sup1
                      else notify.colored("-", config.C_GRAY))
                     + "/"
                     + (notify.colored(f"{res1:.2f}", config.C_RED) if res1
                        else notify.colored("-", config.C_GRAY)))
            # 入池涨跌列: 入池首日收盘 -> 最近刷新日收盘(旧条目无first_close显示-)
            fc = e.get("first_close")
            cum = (e["close"] / fc - 1) if fc else None
            cum_txt = (notify.colored(f"{cum:+.1%}",
                                      config.C_RED if cum > 0 else config.C_GREEN)
                       if cum is not None else "-")
            r.append(f"{days}日 {cum_txt}" if days is not None else cum_txt)
        else:
            r += [f"{days}日" if days is not None else "-"]
        r += [str(e["hits"])]
        rows.append(r)
    heads = (["候选(按多因子分)", "所属板块", "综合分", "动量z", "反转z", "低波z",
              "回踩z", "量比z", "行业z", "现价", "当日情况", "支撑/压力", "入池涨跌",
              "上榜"] if v3 and v5 and v5s else
             ["候选(按多因子分)", "所属板块", "综合分", "动量z", "反转z", "低波z",
              "回踩z", "量比z", "现价", "当日情况", "支撑/压力", "入池涨跌", "上榜"]
             if v3 and v5 else
             ["候选(按多因子分)", "所属板块", "综合分", "动量z", "反转z", "低波z",
              "回踩z", "量比z", "现价", "入池", "上榜"] if v3 else
             ["候选(按动量)", "所属板块", "动量", "现价", "入池", "上榜"])
    body += notify.table(heads, rows)
    if len(snap) > 12:
        body += notify.colored(f"(仅展示池内Top12, 另有{len(snap) - 12}只候选)",
                               config.C_GRAY)
    if obs_best:
        txt = "、".join(f"{e['name']}({e['score']:+.2f})" if v3 and e.get("score") is not None
                        else f"{e['name']}({e['mom'] * 100:+.1f}%)" for e in obs_best)
    else:
        txt = "无(池内候选均在持仓/冷却中)"
    body += notify.colored(f"当前池内择优(调仓日按此实际建仓): {txt}", config.C_RED)
    if v3:
        for i, e in enumerate(obs_best[:3], 1):
            if e.get("score") is None or e.get("z_vr") is None:
                if e.get("reason"):
                    body += notify.colored(f"{e['name']}: {e['reason']}",
                                           config.C_GRAY)
                continue
            body += (notify.colored(f"#{i} {e['name']}({e['code']}) ",
                                    config.C_RED)
                     + notify.colored(f"综合分{e['score']:+.2f}", config.C_GRAY)
                     + notify.colored(_v4_pos_tag(e) or "-", config.C_GRAY)
                     + notify.colored(f" · {e.get('reason', '')}",
                                      config.C_GRAY))
            body += _scorecard(e)
            if v5:
                plan = estim.entry_plan(e, gate_open=gate_open)
                if plan:
                    body += notify.colored(f"入仓时机(v5·信息层): {plan}",
                                           config.C_ORANGE)
        if v5s:
            _w = config.PICK_V5_W
            body += notify.colored(
                "评分口径: v5六因子分=Σ(权重×截面z分)=v4内核五因子+行业分位, "
                f"权重 中期动量{_w['mom']:g}/短期反转{_w['rev']:g}/"
                f"低波动{_w['vol']:g}/回踩位置{_w['pull']:g}/量比{_w['vr']:g}/"
                f"行业分位{_w['brank']:g}"
                "(brank=板块20日动量均值在全市场板块中的分位, 2026-11-19采纳)"
                "; 评分图形=截面z分(中心线左负右红正, ±2.5截断); "
                "极端标的沉底, 只认当日扫描快照; "
                f"保鲜期{config.PICK_POOL_KEEP}个交易日, 超龄自动出池"
                + ("; v5估算层: 当日情况=位置档位+涨跌+量能(vr≥1.1放量/<0.85缩量), "
                   "支撑=MA20/压力=20日高, 入池涨跌=入池首日收盘→最近刷新日收盘, "
                   "入仓时机为信息层建议·不参与自动决策"
                   if v5 else ""), config.C_GRAY)
        else:
            _w = config.PICK_V3_W
            body += notify.colored(
                "评分口径: v4多因子分=Σ(权重×截面z分), 权重 中期动量"
                f"{_w['mom']:g}/短期反转{_w['rev']:g}/低波动{_w['vol']:g}/"
                f"回踩位置{_w['pull']:g}/量比{_w.get('vr', 0):g}"
                "(2026-11-18图片策略融合定版); 评分图形=截面z分"
                "(中心线左负右红正, ±2.5截断); 极端标的沉底, 只认当日扫描快照; "
                f"保鲜期{config.PICK_POOL_KEEP}个交易日, 超龄自动出池"
                + ("; v5估算层: 当日情况=位置档位+涨跌+量能(vr≥1.1放量/<0.85缩量), "
                   "支撑=MA20/压力=20日高, 入池涨跌=入池首日收盘→最近刷新日收盘, "
                   "入仓时机为信息层建议·不参与自动决策"
                   if v5 else ""), config.C_GRAY)
    else:
        body += notify.colored(
            f"排序口径: {'连板行情(妖股/连板股优先)' if zt_on else '非连板行情(极端标的沉底)'}, "
            f"趋势股规则不受影响; 保鲜期{config.PICK_POOL_KEEP}个交易日, 超龄自动出池",
            config.C_GRAY)
    return body


def _record_equity(state, rep_date, total):
    """逐日权益记录(同一日期覆盖), 供dashboard权益曲线; 自2026-10-14起逐日累计"""
    pool = state.get("pool_cash", 0.0)
    pt = {"date": rep_date, "total": round(total, 2),
          "quant": round(total - pool, 2), "pool": round(pool, 2)}
    eq = state.setdefault("equity", [])
    if eq and eq[-1]["date"] == rep_date:
        eq[-1] = pt
    else:
        eq.append(pt)


# ============================== 市场环境(信息层, 2026-10-21用户确认) ==============================
# 大盘走势+持仓所属板块走势 仅进日报/看板辅助人工决策, 不参与引擎任何自动买卖判定(v2.5不动)
# 背景调研与14组门控回测结论已留档Readme: 大盘MA门/板块MA门/弱市半仓等无一跑赢基准

def _norm_ind(n):
    """行业/板块名归一化: 去罗马层级后缀(Ⅰ/Ⅱ/Ⅲ), 用于f100行业名与板块榜名称对齐"""
    return "".join(ch for ch in (n or "") if ch not in "ⅠⅡⅢ").strip()


def _bk_lookup(industry, bkmap):
    """行业名 -> BK代码: 归一化精确匹配优先, 前缀容错取最长命中(减少歧义)"""
    if not industry or not bkmap:
        return None
    ni = _norm_ind(industry)
    for name, bk in bkmap.items():
        if _norm_ind(name) == ni:
            return bk
    cands = sorted(((len(_norm_ind(nm)), bk) for nm, bk in bkmap.items()
                    if _norm_ind(nm) and (_norm_ind(nm).startswith(ni)
                                          or ni.startswith(_norm_ind(nm)))), reverse=True)
    return cands[0][1] if cands else None


def _sma_last(vals, n):
    """最近n根的简单均线(不足n根返回None)"""
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _mkt_env(state, idx_bars, rep_date):
    """市场环境快照: 大盘均线状态 + 持仓/挂单标的所属板块状态
    板块链路: f100行业名(永久缓存) -> 行业板块全量映射(7天缓存) -> 板块指数日K(MA20+5日动量)
    全程try/except静默降级, 任何一环失败只影响展示不影响主流程"""
    env = {"idx": None, "rows": [], "miss": []}
    bars = [b for b in (idx_bars or []) if b["dt"][:10] <= (rep_date or "9999-99-99")]
    if len(bars) >= 10:
        cl = [b["close"] for b in bars]
        env["idx"] = {"date": bars[-1]["dt"][:10], "close": cl[-1],
                      "ma10": _sma_last(cl, 10), "ma20": _sma_last(cl, 20),
                      "ma60": _sma_last(cl, 60)}
    codes = [c for c, a in sorted(state["accounts"].items())
             if a["sh"] > 0 or a.get("pending")]
    if not codes:
        return env
    try:
        bkmap = quotes.fetch_industry_bk_map() or {}
    except Exception as e:
        print("[市场环境] 行业映射获取失败:", repr(e)[:80])
        bkmap = {}
    for code in codes:
        a = state["accounts"][code]
        row = {"code": code, "name": a["name"], "industry": "", "bk": "",
               "close": None, "ma20": None, "mom5": None, "above": None}
        try:
            ind = quotes.fetch_stock_industry(code)
            if ind:
                row["industry"] = ind
                bk = _bk_lookup(ind, bkmap) or quotes.fetch_board_bk_by_name(ind)
                if bk:
                    row["bk"] = bk
                    bb = [b for b in quotes.fetch_board_bars(bk, lmt=25)
                          if b["dt"][:10] <= (rep_date or "9999-99-99")]
                    if len(bb) >= 6:
                        cl = [b["close"] for b in bb]
                        row["close"] = cl[-1]
                        row["ma20"] = _sma_last(cl, 20)
                        row["mom5"] = cl[-1] / cl[-6] - 1
                        if row["ma20"] is not None:
                            row["above"] = cl[-1] >= row["ma20"]
        except Exception as e:
            print(f"[市场环境] {a['name']}({code}) 板块链路失败:", repr(e)[:80])
        if row["above"] is None:
            env["miss"].append(a["name"] + ("(行业缺失)" if not row["industry"]
                                            else "(板块未匹配)" if not row["bk"] else "(板块日K缺失)"))
        env["rows"].append(row)
    return env


def _mkt_env_section(env):
    """市场环境节(微信日报): 大盘均线位置 + 持仓板块状态; ▲红=上方/强势 ▼绿=下方/风险"""
    s = notify.sec("市场环境 (信息层 · 不参与自动决策)")
    idx = env.get("idx")
    if idx:
        def _cmp(ma):
            if ma is None:
                return "-"
            d = idx["close"] - ma
            return notify.colored(f"{'▲' if d >= 0 else '▼'}{d:+.1f}",
                                  config.C_RED if d >= 0 else config.C_GREEN)
        s += notify.table(
            ["指数", "收盘", "MA10", "MA20", "MA60"],
            [[f"上证指数({idx['date']})", f"{idx['close']:.2f}",
              _cmp(idx["ma10"]), _cmp(idx["ma20"]), _cmp(idx["ma60"])]])
    else:
        s += notify.colored("大盘日K获取失败, 本节降级", config.C_GRAY)
    rows = env.get("rows") or []
    if rows:
        tbl = []
        for r in rows:
            if r["above"] is None:
                st = notify.colored("-", config.C_GRAY)
            else:
                st = notify.colored("▲板块强势" if r["above"] else "▼板块走弱",
                                    config.C_RED if r["above"] else config.C_GREEN)
            tbl.append([f"{r['name']}({r['code']})", r["industry"] or "-",
                        f"{r['close']:.1f}" if r["close"] is not None else "-",
                        f"{r['ma20']:.1f}" if r["ma20"] is not None else "-",
                        notify.colored(f"{r['mom5'] * 100:+.1f}%",
                                       notify.pnl_color(r["mom5"] * 100))
                        if r["mom5"] is not None else "-", st])
        s += notify.table(["股票", "所属行业", "板块收盘", "板块MA20", "板块5日动量", "板块状态"], tbl)
    if env.get("miss"):
        s += notify.colored("⚠ 板块数据缺失: " + ", ".join(env["miss"]), config.C_GRAY)
    s += notify.colored(
        "信息层(2026-10-21决策): 大盘/板块走势仅辅助人工决策, 引擎不接入该维度",
        config.C_GRAY)
    return s


def _prev_equity(state, rep_date):
    """上一交易日权益(总资产, 供日报昨日环比): 权益曲线自新向旧取第一条非当日
    记录; 当日已记录(日内二次运行)则跳过当日取前一条; 无记录返回None"""
    for e in reversed(state.get("equity") or []):
        if e.get("date") != rep_date:
            return e
    return None


def _idle_cash_reason(state, cal, rep_date, sel=None, menv=None):
    """选股池现金未入场的当前原因(2026-11-10 用户需求: 总览展示空仓资金理由)
    优先级: 选股停用/观察模式 > 大盘门控关闭 > 持仓封顶 > 非调仓日 > 无过门槛候选
    (大盘门控: 调仓日取sel结果; 非调仓日由市场环境快照menv的上证MA20同口径推算)"""
    if not config.PICK_ENABLED:
        return "智能选股已停用(PICK_ENABLED=False), 资金留池待人工安排"
    if not config.PICK_LIVE:
        return "选股处于观察模式(PICK_LIVE=False), 候选仅展示不实际建仓"
    if config.PICK_V3_EN and config.PICK_V3_GATE_MA:
        gate_bad = None
        mi = (menv or {}).get("idx") or {}
        if sel is not None and sel.get("gate_ok") is False:
            gate_bad, gate_txt = True, sel.get("gate_txt") or ""
        elif sel is None and mi and mi.get("ma20") is not None:
            gate_bad = mi["close"] < mi["ma20"]
            gate_txt = (f"上证收{mi['close']:.2f}≤MA{config.PICK_V3_GATE_MA}"
                        f"({mi['ma20']:.2f})")
        if gate_bad:
            return (f"大盘门控关闭({gate_txt}), 暂停新开仓(持仓退出不受影响)")
    n_busy = sum(1 for a in state["accounts"].values()
                 if a["sh"] > 0 or a.get("pending") or a["cash"] > 0)
    if n_busy >= config.PICK_MAX_HOLD:
        return (f"在管持仓{n_busy}只已达上限{config.PICK_MAX_HOLD}, "
                f"资金留池待调仓日/减仓后分配")
    pool_n = len(state.get("cand_pool") or {})
    lr = state.get("last_rebal")
    left = None
    if cal and lr in cal and rep_date in cal:
        left = max(config.PICK_REBAL - (cal.index(rep_date) - cal.index(lr)), 0)
    if left:                                     # 距下次调仓>0个交易日
        if config.PICK_HOT_EN and pool_n:
            return (f"非调仓日(距下次约{left}个交易日), 候选池{pool_n}只中无达标"
                    f"特别好机会(多因子分≥{config.PICK_HOT_V3_SCORE:.1f}"
                    f"/20日动量≥{config.PICK_HOT_MOM * 100:.0f}%), 按纪律等待")
        return (f"非调仓日(距下次约{left}个交易日), "
                f"常规每{config.PICK_REBAL}个交易日调仓一次")
    if not pool_n:
        return "今日无过门槛候选(选股门槛未达标), 资金待下轮扫描择优"
    return "待调仓日从候选池择优分配"


def build_report(state, fills, last_closes, rep_date, sel=None, stale=None, plans=None,
                obs=None, cal=None, menv=None):
    """复盘日报HTML: 账户概况(含昨日环比与空仓资金理由) + 持仓明细(含明日信号)
    + 本次成交 + 日后操作计划 + 智能选股
    stale: 本次未取到日K的持仓代码(估值回退到最近一次收盘价, 需显式标注)
    plans: _build_plans生成的日后操作计划行
    obs: _daily_pool_scan返回的(快照, 择优模拟, 连板行情, 原始扫描), 候选观察池展示节
    menv: _mkt_env返回的市场环境快照(信息层展示节)"""
    px = lambda a, c: (last_closes.get(c) if last_closes.get(c) is not None
                       else (a.get("px") or 0))
    quant_total = sum(a["cash"] + a["sh"] * px(a, c)
                      for c, a in state["accounts"].items())
    pool = state.get("pool_cash", 0.0)
    total = quant_total + pool
    base = state.get("baseline_total")
    chg = (total - base) if base else None
    # 昨日环比(2026-11-10 用户需求): 总资产 vs 上一交易日权益曲线收盘值
    prev = _prev_equity(state, rep_date)
    day_chg = day_pct = None
    if prev is not None and prev.get("total"):
        day_chg = total - prev["total"]
        day_pct = day_chg / prev["total"] * 100
    body = notify.sec("账户概况") + notify.table(
        ["策略总市值", "选股池现金", "账户总资产", "昨日总资产", "昨日环比", "累计费用"],
        [[f"{quant_total:,.2f}", f"{pool:,.2f}",
          notify.bold(f"{total:,.2f}"),
          f"{prev['total']:,.2f}" if prev is not None and prev.get("total") else "-",
          (notify.colored(f"{day_chg:+,.2f} ({day_pct:+.2f}%)",
                          notify.pnl_color(day_chg))
           if day_chg is not None else notify.colored("首日无对比", config.C_GRAY)),
          f"{state.get('cum_fee', 0):,.2f}"]])
    # 空仓资金理由(2026-11-10 用户需求): 现金留池超过一手门槛才解释(零头不占篇幅)
    if pool >= config.PICK_MIN_PRICE * config.LOT_MIN:
        body += notify.colored(
            f"💰 选股池现金{pool:,.2f}元未入场原因: "
            + _idle_cash_reason(state, cal, rep_date, sel=sel, menv=menv),
            config.C_GRAY)
    pos_rows = []
    for code in sorted(state["accounts"]):
        a = state["accounts"][code]
        if a["sh"] <= 0 and not a.get("pending"):
            continue                            # 已清仓且无挂单: 不占篇幅(流水可查)
        cl = px(a, code)
        if a["sh"] > 0 and a["cost"] > 0 and cl:
            pnl_pct = (cl / a["cost"] - 1) * 100
            pnl_txt = notify.colored(f"{pnl_pct:+.2f}%", notify.pnl_color(pnl_pct))
        else:
            pnl_txt = notify.colored("-", config.C_GRAY)
        leg = f"{'趋势' if a['sh_tr'] else '超跌'}腿" if a["sh"] > 0 else "-"
        if a["sh"] > 0 and a.get("hold_d"):
            leg += f"({a['hold_d']}日)"
        nm = f"{a['name']}({code})"
        if a.get("src") == "手动":
            nm += " [手动]"
        elif a.get("pool"):
            nm += " [选股]"
        if code in (stale or []):
            nm += notify.colored("(行情缺失)", config.C_GRAY)
        pos_rows.append([nm, str(a["sh"]) if a["sh"] > 0 else "0",
                         f"{a['cost']:.3f}" if a["cost"] > 0 else "-",
                         f"{cl:.3f}" if cl else "-", pnl_txt,
                         f"{a['cash']:,.2f}", leg, _sig_cell(a)])
    if pos_rows:
        body += notify.sec("持仓明细与明日信号") + notify.table(
            ["股票", "持股", "成本", "现价", "浮动盈亏", "子账户现金", "腿", "明日信号"],
            pos_rows)
    if stale:
        body += notify.colored(
            f"⚠ 本次未取到日K: {len(stale)}只({', '.join(state['accounts'][c]['name'] for c in stale)}), "
            f"未做决策且总资产按最近一次收盘价估值", config.C_GRAY)
    if fills:
        body += notify.sec("本次成交") + notify.table(
            ["日期", "股票", "方向", "数量", "价格", "金额", "费用", "原因"],
            [[r["date"], f"{r['name']}({r['code']})",
              notify.colored("买入" if r["side"] == "BUY" else "卖出",
                             config.C_RED if r["side"] == "BUY" else config.C_GREEN),
              str(r["qty"]), f"{r['price']:.3f}", f"{r['amount']:,.2f}",
              f"{r['fee']:.2f}", r["reason"]] for r in fills])
    if plans:
        body += notify.sec("日后操作计划") + notify.table(dashboard.PLAN_HEADERS, plans)
    body += _sel_section(sel, state)
    if obs is not None:
        body += _pool_section(obs, state, cal, rep_date, menv=menv)
    if menv is not None:
        body += _mkt_env_section(menv)
    body += (notify.sec("口径")
             + notify.colored(
                 ((config.PICK_V5_EN and config.PICK_V5_SCORE_EN)
                  and "v5六因子机构路线(2026-11-19 brank采纳: v4内核五因子+0.5行业分位, "
                      "半年窗口brank全档稳健+27~32%/MDD改善; 归档v4管线零改动) | "
                  or "v4多因子机构路线(2026-11-18图片策略融合定版, bt_quant_v3.py三轮回测: "
                      "全窗口286日+169%/MDD-11.6%/胜率46.7%, 半年熊市窗口+20.7%不败) | ")
                 + ("选股=全市场行业板成分股, 门槛'趋势内回调'(收盘>MA20/60且MA20上行, 120日跳段动量≥0, "
                     "乖离≤15%, 5日≤6%, 20日≤45%), "
                    + (config.PICK_V5_EN and config.PICK_V5_SCORE_EN
                       and "六因子z-score打分(0.5中期动量+1短期反转+1低波+0.5回踩"
                           "乖离+0.5量比+0.5行业分位brank)取TopK | "
                       or "五因子z-score打分(0.5中期动量+1短期反转+1低波+0.5回踩"
                           "乖离+0.5量比)取TopK | ")
                    + "退出=阶梯止盈+阶梯止损+吊灯止损(22日最高收盘-2×ATR14)先到先卖, "
                    "空仓不自动重入(入场只走调仓日选股挂单; 特别好机会可忽略调仓日与冷却期临时建仓) | "
                    "大盘门控: 上证收盘<MA20暂停新开仓 | "
                    + ("v5估算层(2026-11-19): 压力/支撑位+回踩确认档+入仓时机为信息层, "
                       "不参与自动决策, 时机=支撑MA20上方企稳低吸/放量突破压力跟随 | "
                       if config.PICK_V5_EN else ""))
                 if config.PICK_V3_EN else
                 ("v2.5双模策略 | 每股独立子账户, 趋势腿满仓买入, 阶梯止盈(浮盈<15%带宽6%/"
                  "15~30%带10%/≥30%带15%)+阶梯止损(浮盈<25%线-12%/25~50%线+3%/≥50%线+10%)+MA10破位"
                  "三退出先到先卖/超跌·冰点腿半仓, 买入资金来自本账户卖出回款; 已清仓子账户现金在每个调仓日"
                  "归集入选股池, 由智能选股补入新标的 | "), config.C_GRAY)
             + notify.colored(
                 "收盘判定信号, 次日双窗口择机执行(早盘9:30~10:00/尾盘14:30~14:55, 14:55兜底; "
                 "SELL反抽分时VWAP或深跳空即卖, BUY回踩分时VWAP且守确认位, scalp需价≥信号低点"
                 "+0.3ATR), 盘中runner未跑时按开盘价兜底 | "
                 "每天收盘复盘, 策略调整请直接提出", config.C_GRAY))
    return body, total, chg


def run(dry=False, push=True, now=None, dash=True):
    now = now or datetime.datetime.now()
    state = account.load_state()
    if not state:
        print("量化账户未初始化, 请先运行: python quant_daily.py --init")
        return False
    state.setdefault("pool_cash", state.get("free_cash", 0.0))   # 旧状态迁移: 自由现金入池
    _prune_day_plan(state)             # 双窗口day_plan只留最近3日(防膨胀)
    before = copy.deepcopy(state)      # 推进前快照(昨收): 供dashboard"当日持仓变化"对比
    sel = None
    fills, last_closes, rep_dates, bars_map = [], {}, [], {}
    # 指数日K一次取回: ①交易日历(调仓判定) ②v2.2冰点左侧的市场收盘序列 ③市场环境信息层
    idx_cl, idx_last, cal, _ib = [], "", [], []
    try:
        _ib = _drop_forming_bar(
            quotes.fetch_kline_bars(config.REGIME_INDEX_ALIAS,
                                    klt=KLT_DAILY, lmt=BARS_LMT), now)
        idx_cl = [b["close"] for b in _ib]
        idx_last = _ib[-1]["dt"][:10] if _ib else ""
        cal = [b["dt"][:10] for b in _ib]
    except Exception as e:
        print("[量化] 指数日K获取失败(交易日历/冰点左侧停用):", e)
    for code in sorted(list(state["accounts"])):
        acct = state["accounts"][code]
        try:
            bars = quotes.fetch_kline_bars(code, klt=KLT_DAILY, lmt=BARS_LMT)
        except Exception as e:
            print(f"[量化] {acct['name']}({code}) 日K获取失败: {e}")
            continue
        bars = _drop_forming_bar(bars, now)
        if not bars:
            continue
        bars_map[code] = bars
        last_closes[code] = bars[-1]["close"]
        acct["px"] = bars[-1]["close"]          # 最近一次收盘价(节点故障时估值回退用)
        mkt_cl = idx_cl if (idx_cl and len(idx_cl) == len(bars)
                            and idx_last == bars[-1]["dt"][:10]) else None
        d = _advance(state, acct, code, bars, fills, dry, mkt_cl=mkt_cl)
        rep_dates.append(d or acct.get("last_date") or bars[-1]["dt"][:10])
    if not last_closes:
        print("[量化] 无有效行情, 本次运行取消")
        return False
    rep_date = max(d for d in rep_dates if d)
    stale = [c for c, a in state["accounts"].items()
             if a["sh"] > 0 and c not in last_closes]      # 节点拒连导致未取到日K
    if stale:
        print(f"[量化] 持仓行情缺失{len(stale)}只: {stale} (按最近一次收盘价估值)")
    # ── 调仓与候选扫描(2026-11-10 重构扫描顺序, 全天仅一次全市场扫描) ──
    # 常规调仓日(due/early): _rebalance内部扫描 -> _daily_pool_scan复用(旧行为);
    # 非常规调仓日: 先归集+每日扫描刷新候选池(供好机会旁路用当日快照, 池内择优按
    # 归集后口径), 再由_rebalance判定"特别好机会"是否临时建仓(复用扫描结果)
    obs = None
    if _rebal_trig(state, rep_date, cal):
        sel = _rebalance(state, rep_date, cal, dry, idx_bars=_ib)
        if sel:
            last_closes.update(sel.get("new_closes") or {})
        obs = _daily_pool_scan(state, rep_date, cal, dry, sel=sel)
    else:
        _sweep_cash(state)
        obs = _daily_pool_scan(state, rep_date, cal, dry, sel=None)
        sel = _rebalance(state, rep_date, cal, dry, idx_bars=_ib,
                         scan=obs[3] if obs else None)
        if sel:
            last_closes.update(sel.get("new_closes") or {})
            if obs and sel.get("invested"):
                # 好机会建仓后重算池内择优展示(剔除已建仓, 与调仓日口径一致)
                inv = {x["code"] for x in sel["invested"]}
                obs_best, _zt = _pool_best(state, cal, rep_date, extra_exclude=inv)
                obs = (obs[0], obs_best, obs[2], obs[3])
    plans = _build_plans(state, bars_map, cal, rep_date)
    rebal_info = _rebal_info(state, cal, rep_date)
    menv = None                                   # 市场环境(信息层): 失败不阻断主流程
    try:
        menv = _mkt_env(state, _ib, rep_date)
    except Exception as e:
        print("[量化] 市场环境(信息层)生成失败:", e)
    idle_reason = _idle_cash_reason(state, cal, rep_date, sel=sel, menv=menv)
    body, total, chg = build_report(state, fills, last_closes, rep_date, sel, stale, plans,
                                    obs=obs, cal=cal, menv=menv)
    _record_equity(state, rep_date, total)          # 逐日权益(供dashboard画曲线)
    account.append_log(
        f"===== 量化日报 {rep_date} ===== 总资产{total:,.2f} "
        f"本次成交{len(fills)}笔 选股池{state.get('pool_cash', 0.0):,.2f}", dry=dry)
    account.save_state(state, dry=dry)
    dash_path = None
    if dash:                                        # dashboard是产物不是状态, 干跑也生成便于预览
        try:
            dash_path = dashboard.build(state, bars_map=bars_map, fills=fills,
                                        rep_date=rep_date, sel=sel, now=now,
                                        before=before, plans=plans,
                                        rebal_info=rebal_info, obs=obs, menv=menv,
                                        idle_reason=idle_reason)
        except Exception as e:
            print("[量化] dashboard生成失败:", e)
    title = f"📊 量化独立账户日报 {rep_date}"
    if dry or not push:
        print(f"[{'DRY' if dry else 'NO-PUSH'}] {title}")
        for code in sorted(state["accounts"]):
            a = state["accounts"][code]
            p = a.get("pending")
            print(f"  {a['name']}({code}): 持股{a['sh']} 现金{a['cash']:,.2f} "
                  f"信号:{(p['act'] + ' ' + p.get('reason', '')) if p else ('持有' if a['sh'] else '观望')}")
        if fills:
            for r in fills:
                print(f"  成交 {r['date']} {r['side']} {r['name']} {r['qty']}@{r['price']:.3f} {r['reason']}")
        if sel:
            print(f"  [选股] 板块: " + (f"{sel.get('board_n', 0)}个行业板(全市场)"
                  if sel.get("v3")
                  else str([(b['kind_cn'], b['name']) for b in sel['boards']])))
            print(f"  [选股] 候选{sel['cand_n']} 过门槛{len(sel['passing'])} "
                  f"入选{[(p['name'], p['code']) for p in sel['picks']]} "
                  f"建仓{[(x['name'], round(x['alloc'])) for x in sel['invested']]}")
            if sel.get("gate_txt"):
                print(f"  [选股] 大盘门控: {sel['gate_txt']}"
                      f"{'(放行)' if sel.get('gate_ok', True) else '(闭门暂停新开仓)'}")
            print(f"  [选股] 剔除 {sel['skip']} 选股池 {state.get('pool_cash', 0.0):,.2f}")
        else:
            print("  [选股] 本轮非调仓日")
        if obs and obs[0]:
            pool_txt = ", ".join(f"{e['name']}{e['mom'] * 100:+.1f}%" for e in obs[0])
            print(f"  [观察池] 展示前{len(obs[0])}只: {pool_txt}")
            if obs[1] is not None:
                print(f"  [观察池] 调仓日择优模拟: {[e['name'] for e in obs[1]]}")
        if menv and menv.get("idx"):
            _mi = menv["idx"]
            _up = sum(1 for r in menv["rows"] if r["above"])
            print(f"  [市场环境] 上证收{_mi['close']:.0f} "
                  f"(MA20{'上▲' if _mi['ma20'] and _mi['close'] >= _mi['ma20'] else '下▼'}); "
                  f"持仓板块MA20上方 {_up}/{len(menv['rows'])}")
        print(f"  总资产 {total:,.2f} (策略市值 "
              f"{total - state.get('pool_cash', 0.0):,.2f} + 选股池 "
              f"{state.get('pool_cash', 0.0):,.2f})")
        if dash_path:
            print(f"  dashboard -> {dash_path}")
        return True
    notify.send(title, body)
    if dash_path:
        account.append_log(f"{rep_date} dashboard -> {dash_path}", dry=dry)
    return True
