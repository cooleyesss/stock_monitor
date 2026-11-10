# -*- coding: utf-8 -*-
"""自适应双模策略 v2.5 实盘信号版(回测固化策略的落地应用)
规则与 backtest/daily.py run_ps_stock 一致(共用 core.indicators 同一指标实现):
  趋势腿: 收盘>MA20 & MA5>MA10 & 收盘>MA60 & MA20上行 & 10日动量>2% -> 满仓买入
          阶梯移动止盈(v2.4): 最高收盘回撤达带宽清仓(浮盈<15%→6%/15~30%→10%/≥30%→15%)
          阶梯止损(v2.5): 止损线随最高浮盈上移只上不下(<25%→-12%/25~50%→+3%/≥50%→+10%),
          与止盈阶梯同时在场(止盈管正常出场, 止损兜闪崩+大额浮盈保底)
          收盘<MA10 -> 卖出(止盈/止损/MA10 先到先卖)
          分批建仓(v2.3): 已于v2.4关闭(量化池回测: 满仓+阶梯止盈更优), 代码留备查
  scalp腿(弱势市): 昨低破布林下轨且收回轨内 + RSI14<40 + 收阳 + 5日跌幅>-12%
                   + 次日开盘距信号低点≥0.3ATR -> 半仓买入
  冰点左侧(v2.2, ICE_LEFT_EN): 指数连跌3日或累跌≥3% + 个股RSI14<35且5日跌幅≥-10%
                   且当日收跌 + 开盘≥信号低点+0.3ATR -> 半仓左侧(出场同scalp, 受保险丝约束)
  突发保险丝: 昨日跌<=-7% 或 量>3倍5日均量且跌<=-4% -> 次日禁止scalp(不接飞刀)
实盘语义: 以最新一根日K收盘判定, 生成"次日开盘"操作建议; 收盘后(EOD)扫描推送
与回测的如实差异: scalp/冰点入场确认(0.3ATR空间)依赖次日开盘价, 以关键位形式给出;
  scalp冷却期/持仓腿型属账户内部状态, 实盘信号不跟踪(持仓一律按趋势腿判定破位);
  存量持仓切换v2.4时hmax自切换日起算(无法回溯历史高点, 保守方向不误触发)
"""
import config_quant as config
from common import notify, quotes
from core.indicators import build_ind

KLT_DAILY = 101     # 东财日K周期
BARS_LMT = 120      # 拉取日K根数(覆盖MA60+40日动量)

ACTION_LABEL = {
    "BUY_TREND": ("🟢趋势买入", config.C_RED),
    "BUY_SCALP": ("🟢超跌买入", config.C_RED),
    "BUY_ICE": ("🟢冰点买入", config.C_RED),
    "SELL": ("🔴趋势卖出", config.C_GREEN),
    "FUSE": ("🔥保险丝", config.C_ORANGE),
    "HOLD": ("⚪持有", config.C_GRAY),
    "WATCH": ("⚪观望", config.C_GRAY),
    "NO_DATA": ("➖无数据", config.C_GRAY),
}
_ACTION_ORDER = {"SELL": 0, "BUY_TREND": 1, "BUY_SCALP": 2, "BUY_ICE": 3,
                 "FUSE": 4, "HOLD": 5, "WATCH": 6, "NO_DATA": 7}


def evaluate(code, bars, holding=0, mkt_cl=None):
    """最新一根日K -> 次日开盘操作建议
    返回 {"action", "reason", "level", "snap"}; holding>0按趋势腿持仓处理
    mkt_cl=指数收盘序列(与bars位置对齐): v2.2冰点左侧判定用, None=该维度停用"""
    n = len(bars)
    if n < 65:
        return {"action": "NO_DATA", "reason": f"日K不足({n}根)",
                "level": "", "snap": {}}
    ind = build_ind(bars)
    closes = ind["close"]
    j = n - 1
    cl = closes[j]
    ma5, ma10, ma20, ma60 = (ind["ma5"][j], ind["ma10"][j],
                             ind["ma20"][j], ind["ma60"][j])
    lo, mid, rsi, atr = ind["lo"][j], ind["mid"][j], ind["rsi"][j], ind["atr"][j]
    if None in (ma5, ma10, ma20, ma60, lo, mid, rsi, atr) or atr <= 0:
        return {"action": "NO_DATA", "reason": "指标未就绪", "level": "", "snap": {}}
    mom10 = cl / closes[j - 10] - 1
    ma20_up = ind["ma20"][j - 5] is not None and ma20 > ind["ma20"][j - 5]
    up_reg = cl > ma20 and ma5 > ma10
    up_ok = up_reg and cl > ma60 and ma20_up and mom10 > 0.02
    # 突发保险丝(最新bar为"昨日")
    yret = cl / closes[j - 1] - 1
    v5a = sum(ind["vol"][j - 5:j]) / 5
    panic = yret <= -0.07 or (v5a > 0 and ind["vol"][j] > 3 * v5a
                              and yret <= -0.04)
    snap = {"close": cl, "ma10": ma10, "ma20": ma20, "ma60": ma60,
            "rsi": rsi, "mom10": mom10, "mid": mid, "panic": panic}
    if holding > 0:
        if cl < ma10:
            return {"action": "SELL",
                    "reason": f"收盘{cl:.2f}破MA10({ma10:.2f}), 趋势破位",
                    "level": f"MA10={ma10:.2f}", "snap": snap}
        return {"action": "HOLD", "reason": "趋势未破",
                "level": f"MA10={ma10:.2f}", "snap": snap}
    if up_ok:
        return {"action": "BUY_TREND",
                "reason": f"站上MA20/60且MA20上行, 10日动量{mom10 * 100:+.1f}%",
                "level": f"MA60={ma60:.2f}", "snap": snap}
    if panic:
        return {"action": "FUSE",
                "reason": f"恐慌日(昨{yret * 100:+.1f}%), 次日禁接飞刀",
                "level": "", "snap": snap}
    if not up_reg and j >= 20:
        deep = (cl > lo and bars[j]["low"] < lo and rsi < 40
                and cl > bars[j]["open"]
                and (cl / closes[j - 5] - 1) >= -0.12)
        if deep:
            need = bars[j]["low"] + 0.3 * atr
            return {"action": "BUY_SCALP",
                    "reason": f"破轨收回+RSI{rsi:.0f}+收阳, 半仓候选",
                    "level": (f"开盘≥{need:.2f}(信号低点+0.3ATR)方可入场; "
                              f"中轨{mid:.2f}止盈"),
                    "snap": snap}
        # v2.2规则A: 市场连续冰点+个股超卖未崩 -> 冰点左侧候选(受保险丝约束)
        if config.ICE_LEFT_EN and mkt_cl:
            k0 = j - config.ICE_MKT_DAYS
            mkt_ice = False
            if k0 >= 0:
                dn = sum(1 for k in range(k0 + 1, j + 1)
                         if mkt_cl[k] < mkt_cl[k - 1])
                cum = mkt_cl[j] / mkt_cl[k0] - 1
                mkt_ice = dn >= config.ICE_MKT_DAYS or cum <= config.ICE_MKT_DROP
            stk_ok = (rsi < config.ICE_RSI
                      and (cl / closes[j - 5] - 1) >= config.ICE_STK_DROP5
                      and cl < closes[j - 1])
            if mkt_ice and stk_ok:
                need = bars[j]["low"] + 0.3 * atr
                return {"action": "BUY_ICE",
                        "reason": f"指数{config.ICE_MKT_DAYS}日冰点+RSI{rsi:.0f}超卖, 左侧半仓候选",
                        "level": (f"开盘≥{need:.2f}(信号低点+0.3ATR)方可入场; "
                                  f"中轨{mid:.2f}止盈, 破{bars[j]['low']:.2f}止损"),
                        "snap": snap}
    return {"action": "WATCH", "reason": "无信号", "level": "", "snap": snap}


def scan(state, tplan=None):
    """扫描持仓股(ETF映射真实代码, 持股>0为持有)+做T计划股 -> 信号行list"""
    watch = {}
    for c, p in state["positions"].items():
        rc = config.ETF_CODE_MAP.get(c, c)
        if len(rc) == 6 and rc.isdigit():
            watch[rc] = {"name": p.get("name", rc),
                         "holding": int(p.get("shares", 0))}
    for c, plan in (tplan or {}).items():
        if len(c) == 6 and c.isdigit():
            watch.setdefault(c, {"name": plan.get("name", c), "holding": 0})
    # 指数收盘序列(v2.2冰点左侧): 与个股bars按"长度一致且末日同日期"判定对齐, 否则停用
    idx_cl, idx_last = [], ""
    try:
        _ib = quotes.fetch_kline_bars(config.REGIME_INDEX_ALIAS,
                                      klt=KLT_DAILY, lmt=BARS_LMT)
        idx_cl = [b["close"] for b in _ib]
        idx_last = _ib[-1]["dt"][:10] if _ib else ""
    except Exception:
        pass
    rows = []
    for code in sorted(watch):
        w = watch[code]
        try:
            bars = quotes.fetch_kline_bars(code, klt=KLT_DAILY, lmt=BARS_LMT)
            mc = idx_cl if (idx_cl and len(idx_cl) == len(bars)
                            and idx_last == bars[-1]["dt"][:10]) else None
            r = evaluate(code, bars, w["holding"], mkt_cl=mc)
        except Exception as e:
            r = {"action": "NO_DATA", "reason": f"行情失败:{e}", "level": ""}
        rows.append({"code": code, "name": w["name"], "holding": w["holding"],
                     "action": r["action"], "reason": r["reason"],
                     "level": r["level"]})
    rows.sort(key=lambda x: _ACTION_ORDER.get(x["action"], 9))
    return rows


def eod_scan_and_notify(state, tplan=None, dry=False):
    """收盘战报后追加: 双模策略次日信号表(一条推送)"""
    rows = scan(state, tplan)
    if not rows:
        return
    body = notify.table(
        ["股票", "持仓", "信号", "依据", "关键位"],
        [[f"{r['name']}({r['code']})",
          str(r["holding"]) if r["holding"] else "-",
          notify.colored(ACTION_LABEL[r["action"]][0],
                         ACTION_LABEL[r["action"]][1]),
          r["reason"], r["level"] or "-"] for r in rows])
    body += (notify.sec("口径") +
             notify.colored("v2.1双模策略(回测固化), 以最新日K收盘判定, 次日开盘参考; "
                            "scalp入场需次日开盘≥信号低点+0.3ATR确认", config.C_GRAY))
    title = "📐 双模策略信号(次日参考)"
    if dry:
        print(f"[DRY-推送] {title} ({sum(1 for r in rows if r['action'] not in ('HOLD', 'WATCH', 'NO_DATA'))}条可操作)")
        for r in rows:
            print(f"  {r['name']}({r['code']}): {r['action']} | {r['reason']} | {r['level']}")
        return
    notify.send(title, body)


# ============================== v2.1 有状态机(量化独立账户) ==============================
# 与 backtest/daily.py run_ps_stock 逐语句等价: decide()对应"j=i-1收盘决策",
# apply_fill()对应"次日开盘价成交"; 资金/股数/费用规则完全一致(现金不做中间四舍五入,
# 与回测float累加口径一致, 仅展示层round)。经 bt_daily_cache.json 全量replay验证,
# 6只个股收益/操作序列/权益曲线与run_ps_stock完全一致(见quanttrack说明)。
from common import fees as _fees


def new_leg_state(cash=0.0, shares=0, leg="trend"):
    """每股独立子账户的策略+资金状态(字段与run_ps_stock局部变量一一对应)
    cash: 子账户可用资金(存量持仓账户初始0, 后续来自本账户卖出回款)
    leg: trend=趋势腿(存量持仓一律按趋势腿) / scalp=超跌腿"""
    return {"cash": float(cash), "sh": int(shares), "cost": 0.0,
            "sh_tr": (leg != "scalp"), "sig_low": 0.0,
            "hold_d": 0, "cool": 0, "add": 0.0,
            "hmax": 0.0, "entry_px": 0.0}   # v2.4: 移动止盈基准/不含费入场价(与回测entry同口径)


def decide(st, ind, bars, j, o=None, mkt_cl=None):
    """以第j根日K收盘决策(j>=1), 返回次日开盘动作或None
    o=成交日(第j+1根)开盘价: replay/补跑时传入以做scalp入场空间确认(与回测一致);
    实盘EOD调用时o=None, scalp返回候选信号(带need确认位), 次日fill时校验
    mkt_cl=指数(上证指数)收盘序列(与bars位置对齐, 长度一致才传入): v2.2冰点左侧判定用;
    None=该维度停用(与回测run_ps_stock的idx_cl=None行为一致)
    状态推进(hold_d/cool)在本函数内: 每根收盘恰好调用一次, 不得跳日/重复
      持仓: hold_d先+1再判退出(趋势腿破MA10 / scalp止盈·止损(cool=3)·8日超时)
      空仓: up_ok->BUY_TREND; 非up_reg且j>=20: cool递减, 否则保险丝+超跌/冰点左侧->BUY_SCALP
    v3(2026-11-04回测重构, PICK_V3_EN=True): 趋势破位退出改吊灯止损(PICK_V3_EXIT="chand"
    时: 22日最高收盘-k×ATR14, 与bt_quant_v3逐句同口径); 空仓不再由decide自动重入
    (入场只走调仓日选股挂单, 卖出回款当日归集选股池)"""
    closes = ind["close"]
    ma20, ma5, ma10, ma60 = (ind["ma20"][j], ind["ma5"][j],
                             ind["ma10"][j], ind["ma60"][j])
    lo, mid, rsi, atr = ind["lo"][j], ind["mid"][j], ind["rsi"][j], ind["atr"][j]
    cl, pc = closes[j], bars[j]["close"]
    if None in (ma20, ma5, ma10, lo, mid, rsi, atr) or atr <= 0 or j < 10:
        return None                      # 指标未就绪: 当日无动作(hold_d也不推进)
    up_reg = cl > ma20 and ma5 > ma10
    ma20_up = ind["ma20"][j - 5] is not None and ma20 > ind["ma20"][j - 5]
    mom10 = cl / closes[j - 10] - 1
    up_ok = up_reg and ma60 is not None and cl > ma60 and ma20_up and mom10 > 0.02
    if st["sh"] > 0:
        st["hold_d"] += 1
        if st["sh_tr"]:
            # v2.4 阶梯移动止盈: 先吸收当日收盘入hmax, 再按浮盈档位带宽判定回撤清仓
            # (与回测run_ps_stock dyn实现逐句同构: hmax=max(hmax,pc)先于trail判定)
            if config.DYN_TRAIL_EN:
                st["hmax"] = max(st.get("hmax", 0.0), pc)
                ent = st.get("entry_px") or st.get("cost") or 0.0   # 不含费入场价优先
                pf = (pc / ent - 1) if ent > 0 else 0.0
                for _th, _t in config.DYN_TRAIL_TIERS:
                    if pf >= _th:
                        if st["hmax"] > 0 and pc <= st["hmax"] * (1 - _t):
                            return {"act": "SELL",
                                    "reason": "移动止盈%.0f%%" % (_t * 100),
                                    "level": round(st["hmax"] * (1 - _t), 4)}
                        break
                # v2.5 阶梯止损: 按持仓最高浮盈取档(只上不下), 收盘破线清仓
                # (与回测run_ps_stock dyn.stop_tiers逐句同构: pfh=hmax/ent-1, 取档后破线即卖)
                if config.DYN_STOP_EN and ent > 0:
                    pfh = st["hmax"] / ent - 1
                    for _th, _ln in config.DYN_STOP_TIERS:
                        if pfh >= _th:
                            if pc < ent * _ln:
                                return {"act": "SELL", "reason": "阶梯止损",
                                        "level": round(ent * _ln, 4)}
                            break
            if config.PICK_V3_EN and config.PICK_V3_EXIT == "chand":
                # v3吊灯止损(LeBeau, 与bt_quant_v3同口径): N日最高收盘-k×ATR14,
                # 替代MA10紧退出(回测: MA10退出-6.5%~-11%/MDD-40%+, 吊灯+126%/MDD-11.6%);
                # level供runner 14:30纪律重估(现价收回吊灯线上方则取消卖出)
                n, k = config.PICK_V3_CHAND_N, config.PICK_V3_CHAND_K
                if j >= n - 1 and atr:
                    stop = max(closes[j - n + 1:j + 1]) - k * atr
                    if cl < stop:
                        return {"act": "SELL", "reason": "吊灯止损",
                                "level": round(stop, 4)}
            elif cl < ma10:
                return {"act": "SELL", "reason": "趋势破位",
                        "level": round(ma10, 4)}
            # v2.3 分批建仓: 首笔后站上MA5 -> 次日开盘加仓剩余额度
            if config.SCALE_IN_EN and st.get("add", 0) > 0 and cl > ma5:
                return {"act": "BUY_ADD", "reason": "分批加仓"}
        else:
            if pc >= mid:
                return {"act": "SELL", "reason": "止盈", "level": round(mid, 4)}
            if pc <= st["sig_low"]:
                st["cool"] = 3
                return {"act": "SELL", "reason": "止损",
                        "level": round(st["sig_low"], 4) if st["sig_low"] > 0 else None}
            if st["hold_d"] >= 8:
                return {"act": "SELL", "reason": "超时"}
        return None
    if config.PICK_V3_EN:
        # v3: 空仓不自动重入 —— 入场只走调仓日选股挂单(与回测口径一致),
        # 卖出回款当日由engine归集入选股池, 待调仓日重新分配
        return None
    if up_ok:
        return {"act": "BUY_TREND", "reason": "趋势站上"}
    if not up_reg and j >= 20:
        if st["cool"] > 0:
            st["cool"] -= 1
            return None
        # 突发保险丝: 恐慌日(暴跌/放量长阴)次日不接飞刀
        yret = pc / bars[j - 1]["close"] - 1
        v5a = sum(ind["vol"][j - 5:j]) / 5
        panic = yret <= -0.07 or (v5a > 0 and ind["vol"][j] > 3 * v5a
                                  and yret <= -0.04)
        deep, why, sig = False, "", 0.0
        # v2.2候选B: 日内恐慌V反(默认关: 回测网格全部负增量, 已否决留备查)
        if config.PANIC_BUY_EN:
            loj, pjc = bars[j]["low"], bars[j - 1]["close"]
            if loj < pjc and loj / pjc - 1 <= config.PANIC_DROP:
                rec = (pc - loj) / (pjc - loj)
                if rec >= config.PANIC_RECOVER and v5a > 0 \
                        and ind["vol"][j] >= config.PANIC_VOLX * v5a \
                        and (o is None or (o - loj) >= 0.3 * atr):
                    deep, why, sig = True, "恐慌V反", loj
        if not deep and not panic:
            deep = cl > lo and bars[j]["low"] < lo and rsi < 40
            if not (pc > bars[j]["open"]):
                deep = False
            if (cl / closes[j - 5] - 1) < -0.12:
                deep = False
            if deep and o is not None and (o - bars[j]["low"]) < 0.3 * atr:
                deep = False                 # 开盘距信号低点不足0.3ATR, 放弃(与回测同式)
            if deep:
                why, sig = "超跌", bars[j]["low"]
            # v2.2规则A: 市场连续冰点+个股超卖未崩 -> 左侧半仓(受保险丝约束)
            if not deep and config.ICE_LEFT_EN and mkt_cl:
                k0 = j - config.ICE_MKT_DAYS
                mkt_ice = False
                if k0 >= 0:
                    dn = sum(1 for k in range(k0 + 1, j + 1)
                             if mkt_cl[k] < mkt_cl[k - 1])
                    cum = mkt_cl[j] / mkt_cl[k0] - 1
                    mkt_ice = dn >= config.ICE_MKT_DAYS \
                        or cum <= config.ICE_MKT_DROP
                stk_ok = (rsi < config.ICE_RSI
                          and (cl / closes[j - 5] - 1) >= config.ICE_STK_DROP5
                          and pc < bars[j - 1]["close"])
                if mkt_ice and stk_ok \
                        and (o is None or (o - bars[j]["low"]) >= 0.3 * atr):
                    deep, why, sig = True, "冰点左侧", bars[j]["low"]
        if deep:
            return {"act": "BUY_SCALP", "reason": why, "sig_low": sig,
                    "need": round(sig + 0.3 * atr, 4)}
    return None


def apply_fill(st, code, name, date, o, act, check_open=False, fee_fn=None):
    """以开盘价o执行decide动作, 更新子账户资金/持股, 返回成交记录dict或None(放弃)
    数量/费用规则与run_ps_stock一致: 趋势腿首笔=SCALE_FRAC1比例(v2.3分批, 关闭则满仓),
    scalp半仓(cash*0.5), 卖出全部整百; 不足一手或现金不足(含费用)则放弃, 不改变任何状态
    check_open=True时(实盘次日fill): scalp需开盘价>=need确认位, 否则取消"""
    fees = fee_fn or _fees
    a = act["act"] if isinstance(act, dict) else act
    reason = act.get("reason", "") if isinstance(act, dict) else ""
    if a == "SELL":
        q = st["sh"] // config.LOT_MIN * config.LOT_MIN
        if q < config.LOT_MIN:
            return None
        amt = round(q * o, 2)
        f = fees.sell_fee(amt, code)
        st["cash"] = st["cash"] + amt - f
        st["sh"] -= q
        st["hold_d"] = 0
        st["add"] = 0.0                 # v2.3: 清仓回收未加仓额度
        st["hmax"] = 0.0                # v2.4: 清仓重置移动止盈基准
        st["entry_px"] = 0.0
        return {"date": date, "code": code, "name": name, "side": "SELL",
                "qty": q, "price": round(o, 3), "amount": amt, "fee": f,
                "reason": reason, "leg": "trend" if st["sh_tr"] else "scalp"}
    if a == "BUY_ADD":                  # v2.3 分批建仓: 用剩余额度加仓
        q = int(st["add"] / o // 100 * 100)
        st["add"] = 0.0
        if q < config.LOT_MIN:
            return None
        amt = round(q * o, 2)
        f = fees.buy_fee(amt, code)
        if st["cash"] < amt + f:
            return None
        st["cash"] = st["cash"] - amt - f
        st["sh"] += q
        return {"date": date, "code": code, "name": name, "side": "BUY",
                "qty": q, "price": round(o, 3), "amount": amt, "fee": f,
                "reason": "分批加仓", "leg": "trend"}
    if a == "BUY_SCALP" and check_open and act.get("need") is not None \
            and o < act["need"]:
        return None                      # scalp开盘空间不足0.3ATR, 取消
    if a == "BUY_TREND":
        frac = config.SCALE_FRAC1 if config.SCALE_IN_EN else 1.0
    else:
        frac = 0.5                        # scalp/冰点腿: 半仓
    q = int(st["cash"] * frac / o // 100 * 100)
    if q < config.LOT_MIN:
        return None
    amt = round(q * o, 2)
    f = fees.buy_fee(amt, code)
    if st["cash"] < amt + f:
        return None
    st["cash"] = st["cash"] - amt - f
    st["sh"] += q                        # 回测语义为空仓买入(=q); +=兼容极端复用
    st["hold_d"] = 0
    st["hmax"] = o                       # v2.4: 买入日起算移动止盈基准(=成交日开盘)
    st["entry_px"] = o                   # v2.4: 不含费入场价(移动止盈浮盈档位用, 同回测entry)
    if a == "BUY_TREND":
        st["sh_tr"] = True
        if config.SCALE_IN_EN:
            st["add"] = st["cash"]     # v2.3: 剩余现金全部作为待加仓额度
    else:
        st["sh_tr"] = False
        st["sig_low"] = act.get("sig_low", 0.0)
    return {"date": date, "code": code, "name": name, "side": "BUY",
            "qty": q, "price": round(o, 3), "amount": amt, "fee": f,
            "reason": reason, "leg": "trend" if a == "BUY_TREND" else "scalp"}
