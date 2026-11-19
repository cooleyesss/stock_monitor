# -*- coding: utf-8 -*-
"""智能选股(量化账户选股池)
思路(与用户确认): 短期热门板块/题材 -> 取成分股 -> 硬过滤 + v2.1趋势门槛 -> 动量排序取TopK
  - 热点判定: 板块指数N日动量(实盘可点内时间计算); 当日涨幅榜仅用于"预筛"(实况, 回测不可复现)
  - 候选: 概念题材 + 行业板块各自取动量最高的前M个热门板块的成分股(按成交额取前N只)
  - 硬过滤: 排除ST/日K不足(近似上市未满60交易日)/低价(<3元)/流动性不足(N日均额<1亿)
  - 个股门槛: 复用v2.1趋势入场(收盘>MA20 & MA5>MA10 & 收盘>MA60 & MA20上行 & N日动量>门槛)
  - 排序: 按N日动量从高到低取TopK(默认2)
  - 冷却: 卖出后同一标的PICK_COOLDOWN个交易日内禁止再入选(用该标的自身日K定位日期, 与市场日历一致)
无未来函数: 全部指标只用截至最新一根已收盘日K; 实盘EOD扫描 -> 次日开盘成交(由engine负责)
"""
import datetime
import time
import config_quant as config
from common import quotes
from core.indicators import build_ind
from quant import estim


def hot_boards(kind, lookback=None, scan=None, m=None, mom_min=None, deadline=None):
    """热门板块: 先取当日涨幅榜前scan个, 再按板块指数N日动量复筛取前m个
    返回 list[{bk,name,pct,amount,mom}]"""
    lookback = lookback or config.PICK_LOOKBACK
    scan = scan or config.PICK_BOARD_SCAN
    m = m or config.PICK_BOARD_M
    mom_min = config.PICK_BOARD_MOM_MIN if mom_min is None else mom_min
    boards = quotes.fetch_board_list(kind, n=scan)
    scored = []
    for b in boards:
        if deadline and time.time() > deadline:
            print("[选股] 板块动量扫描超时, 提前结束")
            break
        try:
            bars = quotes.fetch_board_bars(b["bk"], lmt=lookback + 60)
        except Exception as e:
            print(f"[选股] 板块K线获取失败 {b['bk']} {b['name']}: {e}")
            continue
        closes = [x["close"] for x in bars]
        if len(closes) < lookback + 1 or closes[-1 - lookback] <= 0:
            continue
        mom = closes[-1] / closes[-1 - lookback] - 1
        if mom < mom_min:
            continue
        scored.append({**b, "mom": mom})
    scored.sort(key=lambda x: -x["mom"])
    return scored[:m]


def _board_kind_name(kind):
    return "题材" if kind == "concept" else "行业"


def _no_permission(code):
    """无交易权限板块: 科创板688/689 + 北交所(4/8开头, 920新号段)"""
    return code.startswith(tuple(config.PICK_EXCLUDE_PREFIX))


def _pass_hard(bars, name):
    """硬过滤: 返回 None=通过, 否则返回剔除原因"""
    if "ST" in (name or "").upper():
        return "ST"
    if len(bars) < config.PICK_MIN_BARS:
        return "日K不足"
    if bars[-1]["close"] < config.PICK_MIN_PRICE:
        return "低价"
    amt = sum(b["amount"] for b in bars[-20:]) / 20
    if amt < config.PICK_MIN_AMOUNT:
        return "流动性不足"
    return None


def _trend_ok(bars, lookback):
    """v2.1趋势入场门槛(与adaptive.decide的up_ok同口径) + 动量
    返回 (up_ok, mom, close) 或 (False, None, None)"""
    if len(bars) < max(config.PICK_MIN_BARS, lookback + 10):
        return False, None, None
    ind = build_ind(bars)
    j = len(bars) - 1
    closes = ind["close"]
    ma20, ma5, ma10, ma60 = (ind["ma20"][j], ind["ma5"][j],
                             ind["ma10"][j], ind["ma60"][j])
    if None in (ma20, ma5, ma10) or ma60 is None or ma20 == 0:
        return False, None, None
    cl = closes[j]
    up_reg = cl > ma20 and ma5 > ma10
    ma20_up = ind["ma20"][j - 5] is not None and ma20 > ind["ma20"][j - 5]
    mom = cl / closes[j - lookback] - 1
    up_ok = up_reg and cl > ma60 and ma20_up and mom > config.PICK_MOM_MIN
    return up_ok, mom, cl


def _cooling(bars, last_sell, cooldown):
    """按标的自身日K判断"距上次卖出不足cooldown交易日" """
    if not last_sell:
        return False
    dates = [b["dt"][:10] for b in bars]
    if last_sell not in dates:
        return False        # 卖出日不在窗口内 -> 视为已过冷却期
    return (len(dates) - 1 - dates.index(last_sell)) < cooldown


def select(held=None, cool_dates=None, lookback=None, topk=None, deadline=None):
    """完整选股扫描
    held: 当前持股代码集合(排除); cool_dates: {code: 上次卖出日}
    返回 dict: {boards, cand_n, passing, picks, skip, note}
      boards  热门板块 [{bk,name,kind,kind_cn,pct,amount,mom}]
      passing 通过硬过滤+趋势门槛的候选(未做持仓/冷却排除)
      picks   最终入选(已排除持仓/冷却, 按动量排序取TopK)
    PICK_RESON_EN=True 时走短线共振模式(见文件末尾): 大盘regime+板块跑赢大盘
    +个股短线适配过滤, 入选只代表入池, 入场交给adaptive.decide每日择机
    """
    lookback = lookback or config.PICK_LOOKBACK
    topk = topk or config.PICK_TOPK
    held = set(held or ())
    cool_dates = cool_dates or {}
    if deadline is None:
        deadline = time.time() + (config.PICK_V3_TIMEOUT if config.PICK_V3_EN
                                  else config.PICK_TIMEOUT)
    if config.PICK_V3_EN:
        return _select_v3(held, cool_dates, topk, deadline)
    if config.PICK_RESON_EN:
        return _select_reson(held, cool_dates, lookback, topk, deadline)
    boards = []
    for kind in ("concept", "industry"):
        for b in hot_boards(kind, lookback=lookback, deadline=deadline):
            boards.append({**b, "kind": kind, "kind_cn": _board_kind_name(kind)})
    boards.sort(key=lambda x: -x["mom"])

    cands, seen = [], set()
    skip = {config.PICK_EXCLUDE_NAME: 0, "ST": 0, "日K不足": 0, "低价": 0,
            "流动性不足": 0, "行情失败": 0, "趋势未达标": 0, "冷却中": 0}
    for b in boards:
        if time.time() > deadline:
            print("[选股] 成分股扫描超时, 提前结束")
            break
        try:
            members = quotes.fetch_board_members(b["bk"], n=config.PICK_BOARD_TOPN)
        except Exception as e:
            print(f"[选股] 成分股获取失败 {b['bk']}: {e}")
            continue
        for mb in members:
            if mb["code"] in seen:
                continue
            seen.add(mb["code"])
            if _no_permission(mb["code"]):
                # 无权限(科创板/北交所): 拉K线前直接剔除, 省请求
                skip[config.PICK_EXCLUDE_NAME] += 1
                continue
            cands.append({**mb, "board_bk": b["bk"], "board_name": b["name"],
                          "board_kind": b["kind"], "board_kind_cn": b["kind_cn"],
                          "board_mom": b["mom"]})

    passing = []
    for c in cands:
        if time.time() > deadline:
            print("[选股] 个股扫描超时, 提前结束")
            break
        try:
            bars = quotes.fetch_kline_bars_cached(
                c["code"], klt=101, lmt=max(config.PICK_MIN_BARS + 10, lookback + 40))
        except Exception as e:
            skip["行情失败"] += 1
            continue
        why = _pass_hard(bars, c["name"])
        if why:
            skip[why] += 1
            continue
        up_ok, mom, cl = _trend_ok(bars, lookback)
        if not up_ok:
            skip["趋势未达标"] += 1
            continue
        passing.append({**c, "mom": mom, "close": cl, "bars": bars})

    for p in passing:      # 极端连板标记(新华传媒/九阳式妖股): 排序方向看连板行情
        p["extreme"] = _is_extreme(p["code"], p["bars"], p["mom"])
        _attach_disp(p)
    zt_on = limitup_market_on() if config.PICK_EXTREME_EN else False
    _sort_passing(passing, zt_on)
    picks = []
    for p in passing:
        if p["code"] in held:
            continue
        if _cooling(p["bars"], cool_dates.get(p["code"]), config.PICK_COOLDOWN):
            skip["冷却中"] += 1
            continue
        picks.append(p)
        if len(picks) >= topk:
            break
    return {"boards": boards, "cand_n": len(cands), "passing": passing,
            "picks": picks, "skip": skip, "lookback": lookback}


def _limit_pct(code):
    """涨停幅度: 创业板/科创板20%, 其余10%(北交所/无权限标的已在前面剔除)"""
    return 0.198 if code[:3] in ("300", "301", "688", "689") else 0.098


def _is_extreme(code, bars, mom):
    """极端连板/妖股判定(2026-10-21 用户规则: 可入候选池但建仓不优先,
    排序沉底; 优先近期趋势题材/行业标的)。满足任一即极端:
      近PICK_EXTREME_LU_DAYS日涨停>=PICK_EXTREME_LU_CNT 或
      N日动量>=PICK_EXTREME_MOM(短期垂直暴涨) 或 近5日累计涨幅>=PICK_EXTREME_R5"""
    if not getattr(config, "PICK_EXTREME_EN", False):
        return False
    lp = _limit_pct(code)
    w = bars[-(config.PICK_EXTREME_LU_DAYS + 1):]
    lu = sum(1 for i in range(1, len(w))
             if w[i]["close"] / w[i - 1]["close"] - 1 >= lp)
    if lu >= config.PICK_EXTREME_LU_CNT:
        return True
    if mom is not None and mom >= config.PICK_EXTREME_MOM:
        return True
    if len(bars) >= 6 and bars[-1]["close"] / bars[-6]["close"] - 1 >= config.PICK_EXTREME_R5:
        return True
    return False


def limitup_market_on():
    """连板行情判定(2026-10-21 用户规则: 选股先看近期整体行情是否连板行情,
    是则妖股/连板股优先, 否则极端沉底; 趋势股规则不受影响):
    近PICK_ZTM_DAYS个交易日(按 weekday 近似)涨停池, 有效日>=3且
    日均涨停家数>=PICK_ZTM_CNT 且 日均连板(>=2板)家数>=PICK_ZTM_LB -> True
    接口失败/有效日不足 -> False(保守: 极端沉底)"""
    days = []
    d = datetime.date.today()
    while len(days) < config.PICK_ZTM_DAYS:
        if d.weekday() < 5:
            days.append(d)
        d -= datetime.timedelta(days=1)
    stats = [quotes.fetch_zt_pool(x.strftime("%Y%m%d")) for x in days]
    stats = [s for s in stats if s]
    if len(stats) < 3:
        return False
    ac = sum(s["cnt"] for s in stats) / len(stats)
    al = sum(s["lb"] for s in stats) / len(stats)
    return ac >= config.PICK_ZTM_CNT and al >= config.PICK_ZTM_LB


def _sort_passing(passing, zt_on):
    """候选排序: 连板行情=妖股/连板优先(趋势股规则不变排其后); 非连板行情=极端沉底"""
    if zt_on:
        passing.sort(key=lambda x: (not x.get("extreme", False), -x["mom"]))
    else:
        passing.sort(key=lambda x: (x.get("extreme", False), -x["mom"]))


_STYLE_BOARD_NAMES = ("ST股", "破净股", "低价股", "微盘股", "高送转", "次新股",
                      "壳资源", "摘帽", "亏损股", "百元股")


def _style_board(name):
    """风格/情绪分类(昨日涨停_含一字/昨日连板/ST股等)不是真正的行业题材板块"""
    return name.startswith("昨日") or name in _STYLE_BOARD_NAMES


def _attach_disp(p):
    """风格板来源的候选: 补真实行业标签用于展示(2026-10-21 用户: 昨日涨停不叫所属板块)"""
    if _style_board(p.get("board_name", "")):
        ind = quotes.fetch_stock_industry(p["code"])
        if ind:
            p["board_disp"] = f"{ind}({p['board_name']})"


def _disp_board(p):
    """展示用板块标签: 风格板候选优先用真实行业(含风格来源括注)"""
    return p.get("board_disp") or f"{p.get('board_kind_cn', '')}·{p.get('board_name', '')}"


def brief_reason(p):
    if p.get("score") is not None:      # v3多因子: 分数+关键因子展示
        return (f"多因子{p['score']:+.2f}"
                f" | 中期动量{p.get('m120', 0) * 100:+.0f}%"
                f" | 20日{p['mom'] * 100:+.1f}%"
                f" | 乖离{p.get('bias', 0) * 100:+.1f}%"
                + (" | ⚠连板极端" if p.get("extreme") else ""))
    return (f"{_disp_board(p)}{p['board_mom'] * 100:+.1f}%"
            f" | 个股动量{p['mom'] * 100:+.1f}%"
            + (" | ⚠连板极端" if p.get("extreme") else ""))


# ---------------- 短线共振模式(2026-10-20 用户决策, PICK_RESON_EN=True 启用) ----------------
# 与旧模式差异:
#   板块复筛: 20日动量 -> 共振规则(板块近5日涨幅>0 且 跑赢上证同期 且 上证收盘>MA20)
#   个股门槛: 硬过滤 + 短线适配(20日振幅/当日换手率) + 20日动量排序;
#             不再要求个股已站上趋势(up_ok), 入选=入池, 入场交decide每日v2.2信号择机
# 大盘弱势(收盘≤MA20)时整轮选股暂停, 资金留在池内等环境转好

def market_regime():
    """大盘regime(昨日收盘判定, 无未来函数): 上证收盘>MA20为可做多环境
    返回 {"ok","r5","close","ma"} 或 None(行情失败)"""
    w, n = config.PICK_RESON_MKT_MA, config.PICK_RESON_BK_DAYS
    try:
        bars = quotes.fetch_kline_bars(config.REGIME_INDEX_ALIAS, klt=101,
                                       lmt=w + n + 10)
    except Exception as e:
        print(f"[选股] 指数日K获取失败: {e}")
        return None
    closes = [b["close"] for b in bars]
    if len(closes) < max(n + 1, w):
        return None
    ma = sum(closes[-w:]) / w
    return {"ok": closes[-1] > ma, "r5": closes[-1] / closes[-1 - n] - 1,
            "close": closes[-1], "ma": ma}


def resonant_boards(kind, mkt_r5, scan=None, m=None, deadline=None):
    """共振板块: 当日涨幅榜前scan预筛 -> 板块指数近N日涨幅>下限 且 跑赢大盘同期
    返回 list[{bk,name,pct,amount,r5,mom}] (mom=r5, 复用排序/展示字段)"""
    scan = scan or config.PICK_BOARD_SCAN
    m = m or config.PICK_BOARD_M
    n = config.PICK_RESON_BK_DAYS
    out = []
    for b in quotes.fetch_board_list(kind, n=scan):
        if deadline and time.time() > deadline:
            print("[选股] 板块共振扫描超时, 提前结束")
            break
        try:
            bars = quotes.fetch_board_bars(b["bk"], lmt=n + 5)
        except Exception as e:
            print(f"[选股] 板块K线获取失败 {b['bk']} {b['name']}: {e}")
            continue
        closes = [x["close"] for x in bars]
        if len(closes) < n + 1 or closes[-1 - n] <= 0:
            continue
        r5 = closes[-1] / closes[-1 - n] - 1
        if r5 <= config.PICK_RESON_BK_MIN or r5 <= mkt_r5:
            continue
        out.append({**b, "r5": r5, "mom": r5})
    out.sort(key=lambda x: -x["r5"])
    return out[:m]


def _swing_ok(bars):
    """短线适配: 近20日振幅(最高-最低)/最低 >= PICK_SWING_AMP_MIN(有波动才有短线空间)"""
    w = bars[-20:]
    hi = max(b["high"] for b in w)
    lo = min(b["low"] for b in w)
    if lo <= 0:
        return False, 0.0
    amp = (hi - lo) / lo
    return amp >= config.PICK_SWING_AMP_MIN, amp


def _select_reson(held, cool_dates, lookback, topk, deadline):
    """短线共振选股主流程(返回结构与旧模式一致, 增mkt/reson字段)"""
    skip = {config.PICK_EXCLUDE_NAME: 0, "ST": 0, "日K不足": 0, "低价": 0,
            "流动性不足": 0, "行情失败": 0, "振幅不足": 0, "换手不足": 0,
            "动量不足": 0, "冷却中": 0}
    res = {"boards": [], "cand_n": 0, "passing": [], "picks": [], "skip": skip,
           "lookback": lookback, "reson": True, "mkt": None}
    mkt = market_regime()
    res["mkt"] = mkt
    if mkt is None:
        res["note"] = "指数行情失败, 本轮选股取消"
        return res
    if not mkt["ok"]:
        res["note"] = (f"大盘弱势(收盘{mkt['close']:.2f} ≤ MA{config.PICK_RESON_MKT_MA}"
                       f"{mkt['ma']:.2f}), 共振选股暂停, 资金留池等待环境转好")
        return res
    boards = []
    for kind in ("concept", "industry"):
        for b in resonant_boards(kind, mkt["r5"], deadline=deadline):
            boards.append({**b, "kind": kind, "kind_cn": _board_kind_name(kind)})
    boards.sort(key=lambda x: -x["r5"])
    res["boards"] = boards

    cands, seen = [], set()
    for b in boards:
        if time.time() > deadline:
            print("[选股] 成分股扫描超时, 提前结束")
            break
        try:
            members = quotes.fetch_board_members(b["bk"], n=config.PICK_BOARD_TOPN)
        except Exception as e:
            print(f"[选股] 成分股获取失败 {b['bk']}: {e}")
            continue
        for mb in members:
            if mb["code"] in seen:
                continue
            seen.add(mb["code"])
            if _no_permission(mb["code"]):
                skip[config.PICK_EXCLUDE_NAME] += 1
                continue
            cands.append({**mb, "board_bk": b["bk"], "board_name": b["name"],
                          "board_kind": b["kind"], "board_kind_cn": b["kind_cn"],
                          "board_mom": b["r5"]})
    res["cand_n"] = len(cands)

    passing = []
    for c in cands:
        if time.time() > deadline:
            print("[选股] 个股扫描超时, 提前结束")
            break
        if c.get("turnover") is not None and c["turnover"] < config.PICK_SWING_TURN_MIN:
            skip["换手不足"] += 1
            continue
        try:
            bars = quotes.fetch_kline_bars_cached(
                c["code"], klt=101, lmt=max(config.PICK_MIN_BARS + 10, lookback + 40))
        except Exception:
            skip["行情失败"] += 1
            continue
        why = _pass_hard(bars, c["name"])
        if why:
            skip[why] += 1
            continue
        ok, amp = _swing_ok(bars)
        if not ok:
            skip["振幅不足"] += 1
            continue
        _, mom, cl = _trend_ok(bars, lookback)      # 只取mom/cl; up_ok交decide每日择机
        if mom is None or mom < config.PICK_MOM_MIN:
            skip["动量不足"] += 1
            continue
        passing.append({**c, "mom": mom, "close": cl, "amp": amp, "bars": bars})
    for p in passing:      # 极端连板标记: 排序方向看连板行情
        p["extreme"] = _is_extreme(p["code"], p["bars"], p["mom"])
        _attach_disp(p)
    zt_on = limitup_market_on() if config.PICK_EXTREME_EN else False
    _sort_passing(passing, zt_on)
    res["zt_mkt"] = zt_on
    res["passing"] = passing

    picks = []
    for p in passing:
        if p["code"] in held:
            continue
        if _cooling(p["bars"], cool_dates.get(p["code"]), config.PICK_COOLDOWN):
            skip["冷却中"] += 1
            continue
        picks.append(p)
        if len(picks) >= topk:
            break
    res["picks"] = picks
    return res


# ---------------- v4 机构多因子模式(2026-11-18图片策略融合定版, PICK_V4_EN=True启用;
#   v3管线+vr量比因子, 三轮回测依据见config_quant.py PICK_V3_W注释) ----------------
# 与旧模式差异(全部与 bt_quant_v3.py 回测逐句同口径):
#   宇宙: 全市场行业板块(~86个)成分股按成交额前15, 替代"热门板块预筛"
#         (多因子打分自带截面择优, 无需板块动量预筛; 回测已含该差异)
#   门槛: "趋势内回调" —— 收盘>MA20/MA60 & MA20上行 & 120日跳段动量>=0
#         & BIAS20<=15% & 5日涨幅<=6% & 20日涨幅<=45% (回踩入场, 不追过热)
#   打分: 截面z-score四因子加权(中期动量/短期反转/低波动/回踩乖离), extreme沉底
#   连板行情优先逻辑停用(回测未含); 入选即买(engine挂次日开盘BUY单)

def _v3_factors(bars):
    """v3因子快照(与bt_quant_v3.snap同口径, 无未来函数)
    返回 (ok, why, f): ok=过门槛; why=未过原因(计入skip); f=因子dict"""
    if len(bars) < 131:                     # m120需closes[j-130]
        return False, "日K不足", None
    ind = build_ind(bars)
    j = len(bars) - 1
    closes = ind["close"]
    ma20, ma60 = ind["ma20"][j], ind["ma60"][j]
    if not ma20 or ma60 is None:
        return False, "门槛未达", None
    cl = closes[j]
    ma20up = ind["ma20"][j - 5] is not None and ma20 > ind["ma20"][j - 5]
    skip_n = config.PICK_V3_MOM_SKIP
    m120 = closes[j - skip_n] / closes[j - skip_n - config.PICK_V3_MOM_SPAN] - 1
    mom20 = cl / closes[j - 20] - 1
    r5 = cl / closes[j - 5] - 1
    bias = cl / ma20 - 1
    rs = [closes[k] / closes[k - 1] - 1 for k in range(j - 19, j + 1)]
    m = sum(rs) / 20
    vol20 = (sum((x - m) ** 2 for x in rs) / 20) ** 0.5
    # vr量比(2026-11-18采纳, 与bt_quant_v3.snap同口径): 近5日均量/20日均量
    v20 = sum(b["vol"] for b in bars[j - 19:j + 1])
    vr = ((sum(b["vol"] for b in bars[j - 4:j + 1]) / 5.0) / (v20 / 20.0)
          if v20 > 0 else 1.0)
    if not (cl > ma20 and cl > ma60 and ma20up
            and m120 >= config.PICK_V3_MOM_MIN
            and bias <= config.PICK_V3_BIAS_MAX
            and r5 <= config.PICK_V3_R5_MAX
            and mom20 <= config.PICK_V3_R20_MAX):
        return False, "门槛未达", None
    return True, "", {"m120": m120, "mom20": mom20, "r5": r5,
                      "bias": bias, "vol20": vol20, "vr": vr}


def _zscore_v3(cands, extra=()):
    """截面z-score(总体标准差, 与bt_quant_v3._zscore同口径) + 加权合成多因子分
    extra: 同门槛但被冷却剔除的候选(2026-11-10好机会豁免冷却期需要可比分数):
    z参数(mu/sd)只由cands(可投候选)决定, 保持与回测口径完全一致;
    extra仅套用同一组参数补算分数, 不影响cands的打分与排序"""
    w = config.PICK_V3_W

    def _z(key, sign):
        xs = [sign * c[key] for c in cands]
        mu = sum(xs) / len(xs)
        sd = (sum((x - mu) ** 2 for x in xs) / len(xs)) ** 0.5
        for c in list(cands) + list(extra):        # extra共用同一截面参数
            c["z_" + key] = (sign * c[key] - mu) / sd if sd > 0 else 0.0

    _z("m120", 1.0)          # 中期跳段动量: 高者加分
    _z("mom20", -1.0)        # 短期反转: 近20日涨幅低者加分
    _z("vol20", -1.0)        # 低波动加分
    _z("bias", -1.0)         # 距MA20越近越好(回调入场)
    _z("vr", 1.0)            # 量比: 近5日均量/20日均量, 适度放量加分(2026-11-18采纳)
    for c in list(cands) + list(extra):
        c["score"] = (w["mom"] * c["z_m120"] + w["rev"] * c["z_mom20"]
                      + w["vol"] * c["z_vol20"] + w["pull"] * c["z_bias"]
                      + w.get("vr", 0.0) * c["z_vr"])


def _zscore_v5(cands, extra=()):
    """v5打分(2026-11-19 brank采纳): 继承v4内核(_zscore_v3原样调用不动) +
    行业分位brank第六因子; PICK_V5_W六因子加权; brank缺失记z=0中性(回测同口径)
    半年窗口回测: 基线+20.7% -> +27.0%(brank0.5), 全档0.25~1.0均稳健跑赢且MDD改善"""
    _zscore_v3(cands, extra)               # 归档v4内核: 五因子z+score(随后覆盖)
    w = config.PICK_V5_W
    xs = [c["brank"] for c in cands if c.get("brank") is not None]
    mu = sum(xs) / len(xs) if xs else 0.0
    sd = (sum((x - mu) ** 2 for x in xs) / len(xs)) ** 0.5 if xs else 0.0
    for c in list(cands) + list(extra):        # extra共用同一截面参数
        v = c.get("brank")
        c["z_brank"] = (v - mu) / sd if v is not None and sd > 0 else 0.0
        c["score"] = (w["mom"] * c["z_m120"] + w["rev"] * c["z_mom20"]
                      + w["vol"] * c["z_vol20"] + w["pull"] * c["z_bias"]
                      + w["vr"] * c["z_vr"] + w["brank"] * c["z_brank"])


def _v3_extreme(code, bars, mom20, r5):
    """v3极端标记(与bt_quant_v3.snap逐句同口径): 近10日涨停>=3 或
    20日涨幅>=100% 或 5日涨幅>=30% (独立于PICK_EXTREME_EN, v3始终计算)"""
    lp = _limit_pct(code)
    w = bars[-10:]
    lu = sum(1 for i in range(1, len(w))
             if w[i]["close"] / w[i - 1]["close"] - 1 >= lp)
    return (lu >= config.PICK_EXTREME_LU_CNT or mom20 >= config.PICK_EXTREME_MOM
            or r5 >= config.PICK_EXTREME_R5)


def _v3_reason(p, rank, n):
    """v3入选理由(2026-11-05用户需求"看不懂为什么选他"): 不重复因子数值
    (日报因子表已有), 只解释入选逻辑: 门槛事实+两大截面亮点+综合分排名"""
    if p.get("score") is None:
        why = "持仓/冷却中, 未参与本轮截面打分(仅候选观察)"
    else:
        names = {"z_m120": "中期动量强", "z_mom20": "短期回调充分",
                 "z_vol20": "波动低", "z_bias": "贴近MA20回踩位",
                 "z_vr": "量比放大", "z_brank": "行业居前"}
        hl = sorted(((v, names[k]) for k, v in p.items() if k in names),
                    key=lambda kv: -kv[0])
        tops = "/".join(k for _, k in hl[:2])
        why = (f"过趋势内回调门槛, 截面亮点: {tops}"
               f" → 综合分{p['score']:+.2f} 第{rank}/{n}")
    if p.get("extreme"):
        why = "⚠极端(近10日≥3涨停/暴涨)排序沉底·" + why
    return why


def _select_v3(held, cool_dates, topk, deadline):
    """v3多因子选股主流程: 全市场行业板 -> 硬过滤 + 门槛 -> z-score打分 -> TopK
    返回结构与旧模式兼容(passing/picks/skip), 增v3/board_n字段"""
    skip = {config.PICK_EXCLUDE_NAME: 0, "ST": 0, "日K不足": 0, "低价": 0,
            "流动性不足": 0, "行情失败": 0, "门槛未达": 0, "冷却中": 0}
    res = {"v3": True, "boards": [], "board_n": 0, "cand_n": 0, "passing": [],
           "picks": [], "skip": skip, "lookback": config.PICK_V3_MOM_SPAN}
    boards = None
    for _retry in range(3):                     # 板块列表间歇性失败/空返回, 重试3次
        try:
            bs = quotes.fetch_board_list("industry", n=95)
            if bs:
                boards = bs
                break
            err = "行业板块列表为空"
        except Exception as e:
            err = str(e)
        if _retry == 2:
            res["note"] = f"行业板块列表获取失败: {err}"
            return res
        time.sleep(2)
    res["board_n"] = len(boards)
    cands, seen = [], set()
    for b in boards:
        if time.time() > deadline:
            print("[选股] 行业板成分扫描超时, 提前结束")
            break
        try:
            members = quotes.fetch_board_members(b["bk"], n=15)
        except Exception as e:
            print(f"[选股] 成分股获取失败 {b['bk']} {b['name']}: {e}")
            continue
        for mb in members:
            if mb["code"] in seen:
                continue
            seen.add(mb["code"])
            if _no_permission(mb["code"]):
                skip[config.PICK_EXCLUDE_NAME] += 1
                continue
            cands.append({**mb, "board_bk": b["bk"], "board_name": b["name"],
                          "board_kind": "industry", "board_kind_cn": "行业",
                          "board_mom": 0.0})   # v3无板块动量概念(兼容展示字段)
    res["cand_n"] = len(cands)

    v5s = config.PICK_V5_EN and config.PICK_V5_SCORE_EN   # v5打分(brank)开关
    bm_acc = {}                             # v5: 板块->mom20列表(硬过滤后全口径, 同bt)
    passing = []
    for c in cands:
        if time.time() > deadline:
            print("[选股] 个股扫描超时, 提前结束")
            break
        try:
            bars = quotes.fetch_kline_bars_cached(
                c["code"], klt=101, lmt=config.PICK_V3_MOM_SPAN
                + config.PICK_V3_MOM_SKIP + 20)
        except Exception:
            skip["行情失败"] += 1
            continue
        why = _pass_hard(bars, c["name"])
        if why:
            skip[why] += 1
            continue
        if v5s and len(bars) >= 21 and bars[-21]["close"] > 0:
            bm_acc.setdefault(c["board_bk"], []).append(
                bars[-1]["close"] / bars[-21]["close"] - 1)
        ok, why2, f = _v3_factors(bars)
        if not ok:
            skip[why2] += 1
            continue
        p = {**c, **f, "mom": f["mom20"], "close": bars[-1]["close"], "bars": bars}
        p["extreme"] = _v3_extreme(p["code"], bars, p["mom"], f["r5"])
        if config.PICK_V5_EN:                 # v5估算层价位快照(信息层, 不参与打分)
            p.update(estim.snapshot(bars))
        passing.append(p)
    if v5s and bm_acc:
        # brank行业分位(与bt_quant_v3同口径): 板块mom20均值(成员>=3)在全市场
        # 有效板块中的分位; 板块无效/成员不足记None -> z=0中性
        bmean = {b: sum(v) / len(v) for b, v in bm_acc.items()
                 if len(v) >= config.PICK_V5_BRANK_MIN_MEMBERS}
        bmv = sorted(bmean.values())
        for p in passing:
            bm = bmean.get(p["board_bk"])
            p["brank"] = (sum(1 for v in bmv if v < bm) / (len(bmv) - 1)
                          if bm is not None and len(bmv) > 1 else None)
    # 打分口径与回测一致: 先剔除持仓/冷却, 再对剩余候选做截面z-score
    # (2026-11-10好机会豁免: 冷却候选收集为extra, 套用elig同一截面参数补算分数,
    #  供engine判定"特别好机会"能否豁免冷却期; picks仍只出自elig, 回测口径不变)
    elig, cooled = [], []
    for p in passing:
        if p["code"] in held:
            continue
        if _cooling(p["bars"], cool_dates.get(p["code"]), config.PICK_COOLDOWN):
            skip["冷却中"] += 1
            cooled.append(p)
            continue
        elig.append(p)
    if elig:
        if v5s:
            _zscore_v5(elig, cooled)     # v5六因子(继承v4内核+brank)
        else:
            _zscore_v3(elig, cooled)     # 归档v4五因子(零改动)
        elig.sort(key=lambda x: (x["extreme"], -x["score"]))
    passing.sort(key=lambda x: (x.get("extreme", False),
                                -(x["score"] if x.get("score") is not None else -1e9)))
    for i, p in enumerate(passing, 1):
        p["reason"] = _v3_reason(p, i, len(passing))
    # 极端标的不优先(回测同款): 非极端优先取TopK, 全为极端时才轮到极端
    picks = ([p for p in elig if not p["extreme"]][:topk] or elig[:topk])
    res["passing"] = passing
    res["picks"] = picks
    return res
