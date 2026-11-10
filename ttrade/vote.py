# -*- coding: utf-8 -*-
"""多策略投票决策核(纯函数, 日内T专属)
实盘(ttrade/strategy.py)与分时回放(ttrade/replay.py)共用同一 evaluate(), 杜绝两套逻辑漂移
决策数据源: 当日1分钟bars(末根=当前形成中bar) + 实时quote{price,pct,vr}
架构: 锚定票zone(人工区间+量比)是Gate——技术票(VWAP/布林/RSI/动量)只能确认/否决/加权,
      不能独立触发, 保住"围绕人工计划做T"的项目本质
触发: ①zone票同向>0 ②加权总分≥阈值(数据降级时放宽共振) ③同向票数≥要求 ④大盘非bad
降级: bars数据失败 -> 技术票全弃权 -> zone满票时score=1.0直接通过(退化为旧策略不漏计划)
"""
import datetime
import math
from ttrade import config_ttrade as config
from common import notify

VOTER_LABEL = {"zone": "锚定区间", "vwap": "VWAP", "boll": "布林",
               "rsi": "RSI", "mom": "动量", "macd": "MACD", "anom": "异动"}
SIDE_CN = {"buy": "低吸", "sell": "高抛"}
GATE_LABEL = {"ok": "通过", "zone_gate": "锚定区间未达标",
              "regime_bad": "大盘环境否决", "agree_low": "共振票数不足",
              "score_low": "总分未过阈值", "novote": "无有效票",
              "macd_veto": "MACD确认闸否决", "anom_veto": "异动否决(急跌疑似利空)"}
VETO_GATE = {"macd": "macd_veto", "anom": "anom_veto"}   # 硬否决票 -> gate名


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _vote(name, score, weight, reason, valid=True, veto=False):
    """标准化投票: score∈[-1,1]正=支持低吸/负=支持高抛; valid=False为弃权(不入分母)
    veto=True = 对本方向一票否决(硬闸, 同regime_bad位置), 仅macd/anom使用"""
    return {"name": name, "label": VOTER_LABEL.get(name, name),
            "score": round(float(score), 2), "weight": float(weight),
            "reason": reason, "valid": bool(valid), "veto_buy": bool(veto)}


# ---------------- 指标计算(纯函数) ----------------
def bars_of_day(bars, day):
    """取某交易日的bar切片(day='2026-10-13')"""
    return [b for b in (bars or []) if b["dt"][:10] == day]


def calc_vwap(bars_today):
    """当日VWAP与偏离σ: vwap=累计额/(累计量*100); σ=各bar典型价与vwap差的标准差
    返回(vwap, sigma); 无量/数据不足返回(None, None)"""
    if not bars_today:
        return None, None
    cv = sum(b["vol"] for b in bars_today)
    ca = sum(b["amount"] for b in bars_today)
    if cv <= 0 or ca <= 0:
        return None, None
    vwap = ca / (cv * 100.0)
    devs = [(b["high"] + b["low"] + b["close"]) / 3.0 - vwap for b in bars_today]
    mean = sum(devs) / len(devs)
    sigma = math.sqrt(sum((d - mean) ** 2 for d in devs) / len(devs))
    return vwap, sigma


def calc_boll(closes, period=None, n=None):
    """布林带(1分钟收盘): 返回(mid, upper, lower, bw)或None(数据不足)"""
    period = period or config.BOLL_PERIOD
    n = n or config.BOLL_N
    if len(closes) < period:
        return None
    seg = closes[-period:]
    mid = sum(seg) / period
    sd = math.sqrt(sum((c - mid) ** 2 for c in seg) / period)
    upper, lower = mid + n * sd, mid - n * sd
    bw = (upper - lower) / mid if mid > 0 else 0.0
    return mid, upper, lower, bw


def calc_rsi_1m(closes, period=6):
    """1分钟RSI(Wilder平滑, 与quotes._rsi_series同算法), 返回最新值或None"""
    if len(closes) < period + 2:
        return None
    k = 1.0 / period
    ag = al = None
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        ag = g if ag is None else g * k + ag * (1 - k)
        al = l if al is None else l * k + al * (1 - k)
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)


def day_high_low(bars_today):
    """日内最高/最低"""
    if not bars_today:
        return None, None
    return (max(b["high"] for b in bars_today),
            min(b["low"] for b in bars_today))


def _ema_last(values, n):
    """EMA最新值; 数据不足返回None"""
    if len(values) < n:
        return None
    e, k = None, 2.0 / (n + 1)
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
    return e


def _ema_series(values, n):
    """EMA完整序列(与quotes._ema_series同算法)"""
    out, e, k = [], None, 2.0 / (n + 1)
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def agg_bars(bars, k):
    """把连续k根1分钟bar合成一根(k<=1原样返回): 开=首根开/高=最高/低=最低/收=末根收/量额累加
    用于MACD降噪(MACD1M_AGG>1): 1分钟级金叉死叉抖动过频, 聚合后方向确认更稳"""
    if not bars or k <= 1:
        return bars
    out = []
    for i in range(0, len(bars), k):
        seg = bars[i:i + k]
        if not seg:
            continue
        out.append({"dt": seg[-1]["dt"], "open": seg[0]["open"],
                    "close": seg[-1]["close"],
                    "high": max(b["high"] for b in seg),
                    "low": min(b["low"] for b in seg),
                    "vol": sum(b.get("vol", 0.0) for b in seg),
                    "amount": sum(b.get("amount", 0.0) for b in seg)})
    return out


def calc_macd_1m(bars_today, agg=None):
    """1分钟(或AGG分钟聚合)MACD: 返回{dif,dea,hist,pdif,pdea,phist}或None
    bar不足(需 SLOW+SIGNAL+2 根)返回None -> 调用方弃权"""
    agg = config.MACD1M_AGG if agg is None else agg
    if not bars_today:
        return None
    bars = agg_bars(bars_today, agg)
    closes = [b["close"] for b in bars]
    if len(closes) < config.MACD1M_SLOW + config.MACD1M_SIGNAL + 2:
        return None
    dif = [f - s for f, s in zip(_ema_series(closes, config.MACD1M_FAST),
                                 _ema_series(closes, config.MACD1M_SLOW))]
    dea = _ema_series(dif, config.MACD1M_SIGNAL)
    hist = [d - s for d, s in zip(dif, dea)]
    return {"dif": dif[-1], "dea": dea[-1], "hist": hist[-1],
            "pdif": dif[-2], "pdea": dea[-2], "phist": hist[-2]}


# ---------------- 投票器 ----------------
def vote_zone(plan, quote, dwell_min=0.0):
    """锚定票: 人工T计划区间+量比(与旧系统行为完全一致)
    +1: 低吸区且(量比达标或dwell超时豁免); -1: 高抛区对称; 0: 在区但量比未达标(不触发)"""
    w = config.VOTE_ZONE_W
    p, vr = quote["price"], quote.get("vr")
    if "buy_low" in plan and plan["buy_low"] <= p <= plan["buy_high"]:
        vmax = plan.get("buy_vr_max")
        if vmax is None or vr is None or vr <= vmax:
            return _vote("zone", 1.0, w,
                         f"低吸区{plan['buy_low']}~{plan['buy_high']} 量比达标")
        if config.T_DWELL_MIN and dwell_min >= config.T_DWELL_MIN:
            return _vote("zone", 1.0, w,
                         f"低吸区停留{dwell_min:.0f}分钟, 量比豁免")
        return _vote("zone", 0.0, w, f"低吸区内但量比{vr:.2f}>上限{vmax}")
    if "sell_low" in plan and plan["sell_low"] <= p <= plan["sell_high"]:
        vmin = plan.get("sell_vr_min")
        if vmin is None or vr is None or vr >= vmin:
            return _vote("zone", -1.0, w,
                         f"高抛区{plan['sell_low']}~{plan['sell_high']} 量比达标")
        return _vote("zone", 0.0, w, f"高抛区内但量比{vr:.2f}<下限{vmin}")
    return _vote("zone", 0.0, w, "价格不在T区间")


def vote_vwap(quote, bars_today):
    """VWAP票: 偏离分时均线越深分越高(均值回归核心)
    score=clip(-偏离σ数/2σ, -1, 1): 深低于均线支持低吸, 深高于均线支持高抛
    开盘30根内σ不稳, 弃权"""
    w = config.VOTE_VWAP_W
    if not config.VOTE_VWAP_EN:
        return _vote("vwap", 0.0, w, "已关闭", False)
    if not bars_today or len(bars_today) < 30:
        return _vote("vwap", 0.0, w, "开盘初σ未稳, 弃权", False)
    vwap, sigma = calc_vwap(bars_today)
    if not vwap or sigma <= 0:
        return _vote("vwap", 0.0, w, "无量, 弃权", False)
    dev = (quote["price"] - vwap) / sigma
    score = _clip(-dev / config.VWAP_DEV_SIGMA, -1.0, 1.0)
    return _vote("vwap", score, w, f"偏离{dev:+.1f}σ (VWAP{vwap:.3f})")


def vote_boll(quote, bars_today):
    """布林票: %B口径 score=clip((0.5-%B)*2, -1, 1): 触下轨+1, 中轨0, 触上轨-1
    带宽挤压(<BOLL_MIN_BW)时方向未明, 弃权"""
    w = config.VOTE_BOLL_W
    if not config.VOTE_BOLL_EN:
        return _vote("boll", 0.0, w, "已关闭", False)
    if not bars_today or len(bars_today) < config.BOLL_PERIOD:
        return _vote("boll", 0.0, w, "数据不足弃权", False)
    r = calc_boll([b["close"] for b in bars_today])
    if not r:
        return _vote("boll", 0.0, w, "数据不足弃权", False)
    mid, upper, lower, bw = r
    if bw < config.BOLL_MIN_BW:
        return _vote("boll", 0.0, w, f"带宽{bw * 100:.2f}%挤压待突破", False)
    if upper <= lower:
        return _vote("boll", 0.0, w, "带宽为0, 弃权", False)
    pb = (quote["price"] - lower) / (upper - lower)
    score = _clip((0.5 - pb) * 2.0, -1.0, 1.0)
    pos = ("触下轨" if pb < 0.15 else "触上轨" if pb > 0.85 else "中轨附近")
    return _vote("boll", score, w, f"%B={pb:.2f} ({pos})")


def vote_rsi(quote, bars_today):
    """RSI票: 1分钟RSI6, score=clip((45-rsi6)/20, -1, 1)
    RSI6=25饱和超卖→+1.0, 45中性→0, 65饱和超买→-1.0"""
    w = config.VOTE_RSI_W
    if not config.VOTE_RSI_EN:
        return _vote("rsi", 0.0, w, "已关闭", False)
    if not bars_today or len(bars_today) < 10:
        return _vote("rsi", 0.0, w, "数据不足弃权", False)
    closes = [b["close"] for b in bars_today]
    rsi6 = calc_rsi_1m(closes)
    if rsi6 is None:
        return _vote("rsi", 0.0, w, "数据不足弃权", False)
    score = _clip((45.0 - rsi6) / 20.0, -1.0, 1.0)
    tag = ("深度超卖" if rsi6 <= config.RSI1M_BUY_TH else
           "超买" if rsi6 >= config.RSI1M_SELL_TH else "")
    r6p = calc_rsi_1m(closes[:-1])
    trend = "↗" if (r6p is not None and rsi6 > r6p) else \
            ("↘" if (r6p is not None and rsi6 < r6p) else "")
    return _vote("rsi", score, w, f"RSI6={rsi6:.1f}{trend} {tag}".strip())


def vote_momentum(quote, bars_today):
    """动量票: 高抛侧(冲高滞涨-1.0/放量长上影-0.8) + 低吸侧(缩量止跌+1.0/EMA3上穿EMA10+0.8)
    + 默认轻趋势倾斜±0.4; 形态只用已完成bar(bars[:-1], 末根为形成中bar), docstring口径:
    形成中bar的上影可能被后续收回, 故K线形态判断一律取倒数第2根(最近完成bar)
    高抛侧开盘MOM_OPEN_MIN分钟内弃权负向(避开开盘噪声)"""
    w = config.VOTE_MOMENTUM_W
    if not config.VOTE_MOMENTUM_EN:
        return _vote("mom", 0.0, w, "已关闭", False)
    if not bars_today or len(bars_today) < 21:
        return _vote("mom", 0.0, w, "数据不足弃权", False)
    p = quote["price"]
    done = bars_today[:-1]          # 已完成bar
    hi, lo = day_high_low(bars_today)
    if not hi or not lo or hi <= 0 or lo <= 0:
        return _vote("mom", 0.0, w, "数据异常弃权", False)
    closes = [b["close"] for b in done]
    mean_vol = sum(b["vol"] for b in done[-20:]) / 20.0
    open_ok = len(bars_today) >= config.MOM_OPEN_MIN
    score, reason = 0.0, ""

    # ① 冲高滞涨(高抛): 价距日内高≤0.2%, 近5根未创新高, 近3根均量<峰值量×MOM_VOL_SHRINK
    if open_ok and p >= hi * 0.998:
        no_new_high = all(b["high"] < hi for b in done[-5:])
        peak_vol = max(b["vol"] for b in done[-20:])
        shrink = sum(b["vol"] for b in done[-3:]) / 3.0 \
            < peak_vol * config.MOM_VOL_SHRINK
        if no_new_high and shrink:
            score = -1.0
            reason = f"冲高滞涨(距日内高{(p / hi - 1) * 100:.2f}% 量能萎缩)"
    # ② 放量长上影(高抛): 最近完成bar上影≥实体2倍且量>均量2倍
    if score == 0.0 and open_ok:
        lastb = done[-1]
        body = abs(lastb["close"] - lastb["open"])
        upper_shadow = lastb["high"] - max(lastb["close"], lastb["open"])
        if body > 0 and upper_shadow >= body * 2 and lastb["vol"] > mean_vol * 2:
            score, reason = -0.8, "放量长上影"
    # ③ 缩量止跌(低吸): 距日内低≤0.5%, 近3根企稳形态(收≥开或下影≥实体), 均量<0.7倍
    if score == 0.0 and p <= lo * 1.005:
        last3 = done[-3:]
        calm = sum(b["vol"] for b in last3) / 3.0 < mean_vol * 0.7
        shape = all(b["close"] >= b["open"]
                     or (min(b["close"], b["open"]) - b["low"])
                     >= abs(b["close"] - b["open"]) for b in last3)
        if calm and shape:
            score, reason = 1.0, "缩量止跌企稳"
    # ④ 动量反转(低吸): EMA3上穿EMA10且价格在日内低点上方1.5%内
    if score == 0.0 and p <= lo * 1.015:
        e3, e10 = _ema_last(closes, 3), _ema_last(closes, 10)
        e3p, e10p = _ema_last(closes[:-1], 3), _ema_last(closes[:-1], 10)
        if None not in (e3, e10, e3p, e10p) and e3p <= e10p and e3 > e10:
            score, reason = 0.8, "EMA3上穿EMA10(日内低点附近)"
    # ⑤ 默认轻趋势倾斜
    if score == 0.0:
        e3, e10 = _ema_last(closes, 3), _ema_last(closes, 10)
        if e3 and e10:
            score = _clip((e3 - e10) / e10 / 0.003, -0.4, 0.4)
            reason = f"轻趋势{'偏多' if score > 0 else '偏空' if score < 0 else '中性'}"
    if not reason:
        reason = "无明显形态"
    return _vote("mom", score, w, reason)


def vote_macd(quote, bars_today):
    """MACD票(第六票): 1分钟(或MACD1M_AGG分钟聚合)EMA12/26/9
    金叉+1.0 / 死叉-1.0 / 柱为正放大+1.0收敛+0.5 / 柱为负放大-1.0收敛-0.5
    低吸确认闸(MACD1M_BUY_VETO_MODE): accel=只否决"柱负且放大"(加速下跌, 不接飞刀)
    any_neg=柱体为负即否决; off=纯软票不否决
    数据不足弃权(与其余技术票同哲学)"""
    w = config.VOTE_MACD_W
    if not config.VOTE_MACD_EN:
        return _vote("macd", 0.0, w, "已关闭", False)
    m = calc_macd_1m(bars_today)
    if not m:
        return _vote("macd", 0.0, w, "数据不足弃权", False)
    hist, phist = m["hist"], m["phist"]
    cross_up = m["pdif"] <= m["pdea"] and m["dif"] > m["dea"]
    cross_dn = m["pdif"] >= m["pdea"] and m["dif"] < m["dea"]
    accel_down = hist < 0 and hist <= phist      # 柱为负且继续放大 = 下跌加速
    if cross_up:
        score, reason = 1.0, "金叉"
    elif cross_dn:
        score, reason = -1.0, "死叉"
    elif hist > 0:
        score = 1.0 if hist >= phist else 0.5
        reason = "柱为正" + ("放大" if hist >= phist else "收敛")
    elif hist < 0:
        score = -1.0 if accel_down else -0.5
        reason = "柱为负" + ("放大" if accel_down else "收敛")
    else:
        score, reason = 0.0, "柱体归零"
    mode = config.MACD1M_BUY_VETO_MODE
    veto = bool(mode == "any_neg" and hist < 0) or \
        bool(mode == "accel" and accel_down)
    if veto:
        reason += "·低吸否决"
    return _vote("macd", score, w, f"{reason}(柱{hist:+.4f})", veto=veto)


def vote_anomaly(quote, bars_today):
    """异动票(第七票): 近ANOM_WIN分钟放量急拉/急跌识别
    能力边界: 项目无新闻数据源, "突然利好/利空"只能从量价异动推断(不能真读新闻/公告)
    放量急拉(≥ANOM_PCT且量≥ANOM_VOL_MULT倍) -> 疑似利好, 支持高抛(score<0, 冲高出货)
    放量急跌分两档:
      极端档(跌幅≥ANOM_PCT_EXTREME) -> 疑似利空, veto_buy=True 禁止低吸(防接飞刀)
      中等档(ANOM_PCT≤跌幅<极端)且末根企稳(收≥开或长下影或量萎缩) -> 支持低吸(score>0)
    无异动 -> 弃权(不入权重分母, 不稀释既有信号强度)
    与regime_bad分工: 本票per-stock(个股异动), regime_bad per-market(大盘), 两者独立生效"""
    w = config.VOTE_ANOM_W
    if not config.VOTE_ANOM_EN:
        return _vote("anom", 0.0, w, "已关闭", False)
    done = (bars_today or [])[:-1]        # 只用已完成bar(末根为形成中bar)
    k = config.ANOM_WIN
    if len(done) < max(k + 1, 21):
        return _vote("anom", 0.0, w, "数据不足弃权", False)
    p = quote["price"]
    c0 = done[-k]["close"]      # 基准=窗口起点bar收盘; 从该点到当前价恰好跨k个bar间隔
    if not c0:
        return _vote("anom", 0.0, w, "数据异常弃权", False)
    move = (p / c0 - 1) * 100.0
    mean_vol = sum(b["vol"] for b in done[-20:]) / 20.0
    vol_mult = (sum(b["vol"] for b in done[-k:]) / k / mean_vol) \
        if mean_vol > 0 else 0.0
    if abs(move) < config.ANOM_PCT or vol_mult < config.ANOM_VOL_MULT:
        return _vote("anom", 0.0, w, "无异动", False)
    if move > 0:
        return _vote("anom", -0.8, w,
                     f"放量急拉{move:+.2f}%(量{vol_mult:.1f}倍, 疑似利好→优先高抛)")
    if -move >= config.ANOM_PCT_EXTREME:
        return _vote("anom", -0.8, w,
                     f"极端放量急跌{move:+.2f}%(量{vol_mult:.1f}倍, 疑似利空→禁止低吸)",
                     veto=True)
    last = done[-1]
    calm = last["vol"] < mean_vol * 0.7
    shape = last["close"] >= last["open"] or \
        (min(last["close"], last["open"]) - last["low"]) \
        >= abs(last["close"] - last["open"])
    if calm or shape:
        return _vote("anom", 0.8, w,
                     f"急跌{move:+.2f}%后企稳(量{vol_mult:.1f}倍, 疑似利空出尽→优先低吸)")
    return _vote("anom", 0.0, w, f"急跌{move:+.2f}%未见企稳, 观望", False)


# ---------------- regime大盘门控 ----------------
def regime_state(index_quote, index_bars_today):
    """大盘择时(上证指数): 三态 good/neutral/bad, 每轮算一次全股共享
    bad: 指数≤-1.5% 或 (≤-0.8%且跌破指数分时均线VWAP) -> 低吸一票否决(高抛/止损不受限)
    good: ≥+0.8%且站上分时均线 -> 买方向阈值降REGIME_BONUS
    指数数据缺失 -> neutral(降级不拦截, 与AI失败降级同哲学)"""
    if not index_quote:
        return {"state": "neutral", "reason": "大盘数据缺失, 不拦截"}
    pct = index_quote.get("pct", 0) or 0
    p = index_quote.get("price")
    vwap, _ = calc_vwap(index_bars_today or [])
    below = vwap is not None and p is not None and p < vwap
    if pct <= config.REGIME_BAD_PCT or (
            pct <= config.REGIME_BAD_VWAP_PCT and below):
        why = f"上证{pct:+.2f}%"
        if pct > config.REGIME_BAD_PCT:
            why += "(跌破分时均线)"
        return {"state": "bad", "reason": why + ", 低吸暂缓"}
    if pct >= config.REGIME_GOOD_PCT and not below:
        return {"state": "good", "reason": f"上证{pct:+.2f}%站上分时均线"}
    return {"state": "neutral", "reason": f"上证{pct:+.2f}%"}


# ---------------- 引擎入口 ----------------
def evaluate(plan, quote, bars_today, dwell_min=0.0, regime=None, tech=True):
    """多策略投票评估(实盘/回测唯一共用入口)
    bars_today: 当日1分钟bar切片(末根=当前bar); None=数据失败(技术票弃权退化为旧策略)
    regime: regime_state()结果; None=neutral
    tech=False: 对照组模式, 技术票全部弃权(回测zone-only跑旧策略基线)
    返回 EvalResult{"buy":side, "sell":side, "regime":..., "degraded":..., "votes":[...]}
    side = {"pass", "score", "agree", "agree_need", "thr", "gate", "votes"}"""
    regime = regime or {"state": "neutral", "reason": "无大盘数据"}
    votes = [vote_zone(plan, quote, dwell_min)]
    if tech:
        votes += [vote_vwap(quote, bars_today), vote_boll(quote, bars_today),
                  vote_rsi(quote, bars_today), vote_momentum(quote, bars_today)]
        if config.VOTE_MACD_EN:
            votes.append(vote_macd(quote, bars_today))
        if config.VOTE_ANOM_EN:
            votes.append(vote_anomaly(quote, bars_today))
    else:
        off = ["vwap", "boll", "rsi", "mom"] \
            + (["macd"] if config.VOTE_MACD_EN else []) \
            + (["anom"] if config.VOTE_ANOM_EN else [])
        votes += [_vote(k, 0.0, 0.0, "对照组关闭", False) for k in off]
    n_valid = sum(1 for v in votes if v["valid"])
    degraded = all(not v["valid"] for v in votes[1:])   # 技术票全弃权=数据降级

    def _side(direction):   # direction=+1买 / -1卖
        wsum = sum(v["weight"] for v in votes if v["valid"])
        if wsum <= 0:
            return {"pass": False, "score": 0.0, "agree": 0, "agree_need": 0,
                    "thr": 0.0, "gate": "novote", "votes": votes}
        score = sum(v["weight"] * max(direction * v["score"], 0.0)
                    for v in votes if v["valid"]) / wsum
        zone_ok = direction * votes[0]["score"] > 0        # 锚定Gate
        agree = sum(1 for v in votes if v["valid"]
                    and v["score"] * direction >= config.QUANT_MIN_VOTE)
        # 硬否决(仅买方向): macd确认闸/异动极端急跌, 位置同regime_bad
        veto = next((v["name"] for v in votes
                     if direction > 0 and v.get("veto_buy")), None)
        # 数据降级时共振要求放宽到有效票数(只剩zone时=1, 保证退化为旧策略)
        agree_need = min(config.QUANT_MIN_AGREE, n_valid)
        thr = config.QUANT_BUY_SCORE if direction > 0 else config.QUANT_SELL_SCORE
        if direction > 0 and regime.get("state") == "good":
            thr = round(thr - config.REGIME_BONUS, 2)     # 好环境买分加成=降门槛
        gate = "ok"
        if not zone_ok:
            gate = "zone_gate"
        elif direction > 0 and regime.get("state") == "bad":
            gate = "regime_bad"
        elif veto:
            gate = VETO_GATE.get(veto, "veto")
        elif agree < agree_need:
            gate = "agree_low"
        elif score < thr:
            gate = "score_low"
        return {"pass": gate == "ok", "score": round(score, 3),
                "agree": agree, "agree_need": agree_need,
                "thr": thr, "gate": gate, "votes": votes}

    return {"buy": _side(1), "sell": _side(-1), "regime": regime,
            "degraded": degraded, "votes": votes}


# ---------------- 格式化(推送/AI prompt/归因共用一份数据) ----------------
def votes_text(res, side):
    """AI prompt用纯文本投票明细"""
    s = res[side]
    state = "达共振" if s["pass"] else \
        f"未达({GATE_LABEL.get(s['gate'], s['gate'])})"
    lines = [f"【量化投票({SIDE_CN[side]}方向 总分{s['score']:.2f}, "
             f"共振{s['agree']}/{s['agree_need']}票, {state})】"]
    for v in res["votes"]:
        sc = f"{v['score']:+.2f}" if v["valid"] else "弃权"
        lines.append(f"- {v['label']}(权重{v['weight']:.2f}): {sc} ({v['reason']})")
    lines.append(f"- 大盘门控: {(res.get('regime') or {}).get('reason', '无')}")
    return "\n".join(lines)


def votes_html(res, side):
    """推送用投票明细表格(复用notify.table)"""
    s = res[side]
    state = notify.colored(notify.bold("✅共振触发"), config.C_RED) if s["pass"] \
        else notify.colored(f"❌{GATE_LABEL.get(s['gate'], s['gate'])}", config.C_GRAY)
    title = f"🗳 量化投票 · {SIDE_CN[side]} 总分{s['score']:.2f}"
    rows = []
    for v in res["votes"]:
        if not v["valid"]:
            sc = notify.colored("弃权", config.C_GRAY)
        elif v["score"] > 0:
            sc = notify.colored(f"{v['score']:+.2f}", config.C_RED)
        elif v["score"] < 0:
            sc = notify.colored(f"{v['score']:+.2f}", config.C_GREEN)
        else:
            sc = notify.colored("0.00", config.C_GRAY)
        reason = v["reason"] if v["score"] != 0 else \
            notify.colored(v["reason"], config.C_GRAY)
        rows.append([v["label"], sc, f"{v['weight']:.2f}", reason])
    foot = (f"共振{s['agree']}/{s['agree_need']}票"
            + ("✅" if s["agree"] >= s["agree_need"] else "❌")
            + f" | 总分{s['score']:.2f}/阈值{s['thr']:.2f}"
            + ("✅" if s["score"] >= s["thr"] else "❌")
            + f" | {(res.get('regime') or {}).get('reason', '')}")
    return (notify.sec(title) + state + "<br>" +
            notify.table(["投票器", "得分", "权重", "理由"], rows) +
            f"<div style='color:#bbbbbb;font-size:12px;'>{foot}</div>")


def main_tag(res, side):
    """归因主导票: 非zone票中权重×|得分|最大且同向者; 无则锚定zone
    返回如 'vwap+0.72' / 'zone+1.00'(strategy_tags, 供流水CSV/日志/EOD归因)"""
    direction = 1 if side == "buy" else -1
    best, bw = None, 0.0
    for v in res["votes"][1:]:
        if not v["valid"] or v["score"] * direction <= 0:
            continue
        wsc = v["weight"] * abs(v["score"])
        if wsc > bw:
            best, bw = v["name"], wsc
    name = best or "zone"
    return f"{name}{res[side]['score']:+.2f}"


def tag_key(tag):
    """从strategy_tags('vwap+0.72')解析投票器短名"""
    for k in ("vwap", "boll", "rsi", "mom", "macd", "anom", "zone"):
        if (tag or "").startswith(k):
            return k
    return ""


# ---------------- 埋点 ----------------
def record_vote_log(state, code, name, side, res, executed=False,
                   ai="", obs=False, qty=0, price=0.0, now=None):
    """过线信号写入state['today_vote_log'](rollover每日重置), 供EOD策略统计
    obs=True为STRATEGY_DRY_RUN观察模式信号"""
    s = res.get(side) or {}
    state.setdefault("today_vote_log", []).append({
        "time": (now or datetime.datetime.now()).strftime("%H:%M:%S"),
        "code": code, "name": name, "side": side,
        "score": s.get("score", 0), "agree": s.get("agree", 0),
        "gate": s.get("gate", ""), "regime": (res.get("regime") or {}).get("state", ""),
        "degraded": res.get("degraded", False),
        "votes": {v["label"]: v["score"] for v in res.get("votes", []) if v["valid"]},
        "tag": main_tag(res, side) if side in res else "",
        "executed": bool(executed), "obs": bool(obs), "ai": ai,
        "qty": qty, "price": price})
