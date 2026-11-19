# -*- coding: utf-8 -*-
"""量化策略v3重构 · 组合级回测器(选股+退出 全历史模拟)
================================================================
背景(2026-11-04 用户): quant策略失败率过高(10-14上线以来-10.4%, 往返胜率1/6),
要求调研机构策略 -> 重构选股与退出 -> 回测调参直到达标。

诊断(quant_trades.csv 6笔往返): v2.5 = 热门板块20日动量TopK追高买入 + MA10紧退出
  = 买在垂直拉升顶部(德才-15%/大业-6%/凡拓均买在40%+动量末端)、首次回调即止损。
  调研结论(方正金工《市场的脉搏》): A股仅1日尺度有动量, 周/月尺度为反转效应,
  追20日高动量统计上必输; 机构做法 = 中期动量(跳过近期) + 短期反转 + 低波动 + 回调入场。

v3新策略(机构路线):
  门槛: 收盘>MA20 & >MA60 & MA20上行 & 120日跳段动量(剔近10日)≥0
        & BIAS20≤15% & 5日涨幅≤8% & 20日涨幅≤35%  (趋势内回调, 不追过热)
  打分: score = w_mom·z(mom120x10) + w_rev·z(-r20) + w_vol·z(-vol20) + w_pull·z(-bias20)
  退出: MA10 / MA20 / 吊灯(22日最高收盘-k×ATR14) 网格对比; 保留v2.4/v2.5阶梯止盈止损
  大盘门控(变体): 上证收盘<MA20时暂停新开仓(持仓退出不受影响)
  仓位: 调仓现金等权分配(与生产一致); 持仓3~5只/每次最多2新/冷却10日/周期5日

对比基准: v2.5复制(20日动量+up_ok门槛+MA10退出)。两变体共享同一宇宙与机制,
  差异仅在因子与退出(生产v2.5的板块预筛/候选池累积为两变体共同省略项)。
宇宙: 东财行业板块(~86个)成分股按成交额前15(快照), 无权限/ST剔除, 信号日硬过滤
  (价≥3元/20日均额≥1亿/自身日K≥130根)。宇宙构成含快照偏差(今日热门明日在册),
  两变体共享、结论为相对比较, 报告已注明。
数据: 东财日K前复权 2025-11-01~2026-11-04, JSON磁盘缓存(_bt_tmp/v3_bars_cache.json)
口径: 严格无未来函数(收盘决策次日开盘成交), 含全套费用, 期初资金387,467.35
用法: python bt_quant_v3.py [--refresh] [--log 变体关键词] [--params ...调参见main]

2026-11-18 扩展(图片策略融合回测): 新增7个日K代理因子进z-score网格 ——
  vr量比 / act活跃度适中(-|z(log amt20)|) / bd板块动量(板内均值mom20) /
  pos距20日高 / cpos日内收盘位(近5日(C-L)/(H-L), 分时指标代理) /
  lsb股板同向率 / lbm板市同向率(个股->板块->大盘三层联动; 注: 板块超额动量
  在截面z-score下与bd等价(常数平移不变), 故板市联动用同向率而非超额)。
  最近窗口涨幅不新增(已被现有rev/m120覆盖, 与"A股周月尺度反转"结论冲突)。

2026-11-19 第四轮(图片指标原口径三轮回测, 用户指令): 新增3因子 ——
  irank个股行业内涨幅分位(候选mom20在其板内当日有效成员>=3只中的分位, 越高=
  板内温和领涨) / brank行业全市场排名分位(板块mom20均值在全部有效板块中的分位) /
  btier板块属性非单调档位(温和领涨[cold,hot]区=+1, 过热>hot或冷淡<cold=-1)。
  三轮: R1 --days 0 全窗口 / R2 --days 120 半年窗口 / R3 --perturb 参数扰动
  (权重0.25/0.75档+btier_hot阈值8%/12%/15%+因子组合)。基线=v4现行五因子。
"""
import argparse
import json
import math
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests
import config_common as C
import config_quant as Q
from common import fees
from common import quotes
from core.indicators import build_ind

BEG, END = "20250301", "20261104"
CAPITAL = C.BT_CAPITAL       # 回测期初资金锚点(个性化, config_local.py 覆盖)
TX_LMT = 420                 # 腾讯回退路径取数根数(需覆盖BEG起全窗口)
TMP = C.CACHE_DIR            # 运行期缓存目录(个性化路径经 config_local 覆盖)
BARS_CACHE = os.path.join(TMP, "v3_bars_cache.json")
UNI_CACHE = os.path.join(TMP, "v3_universe.json")
IDX = "999999"
EXCLUDE_PREFIX = ("688", "689", "4", "8", "920")
WARM = 135                     # 预热bar数(mom120x10需130根)
SIM_DAYS = 120                 # 模拟窗口长度(交易日; 2026-11-18用户要求缩短至半年,
                               #  取日历末段N日, 预热仍用全历史; 0=全窗口)
TRAIL = Q.DYN_TRAIL_TIERS      # v2.4阶梯止盈(生产现行)
STOPT = Q.DYN_STOP_TIERS       # v2.5阶梯止损(生产现行)

DEFAULTS = {"mom_min": 0.0, "bias_max": 0.15, "r5_max": 0.08, "r20_max": 0.35,
            "w": {"mom": 1.0, "rev": 1.0, "vol": 1.0, "pull": 0.5},
            "chand_k": 3.0, "btier_hot": 0.10, "btier_cold": 0.0}

# 2026-11-18 图片策略融合: 因子注册表(权重键 -> (sp字段, z-score符号)); act特殊处理
# 2026-11-19 irank/brank/btier: 板内分位/板块间分位为[0,1](越高越领涨, sign=+1);
#   btier档位值±1自带方向(温和+1/过热冷淡-1), z-score后加权
FACTORS = {"mom": ("m120", 1.0), "rev": ("mom20", -1.0), "vol": ("vol20", -1.0),
           "pull": ("bias", -1.0), "vr": ("vr", 1.0), "bd": ("bmom", 1.0),
           "pos": ("pos20", 1.0), "cpos": ("cpos5", 1.0),
           "lsb": ("lnk_sb", 1.0), "lbm": ("lnk_bm", 1.0),
           "irank": ("irank", 1.0), "brank": ("brank", 1.0),
           "btier": ("btier", 1.0)}
BRET = {}    # 板块等权日收益序列(main预计算一次, simulate只读)


_TX2_HOSTS = ["https://proxy.finance.qq.com/ifzqgtimg",
              "https://web.ifzq.gtimg.cn"]
_TX2_TURN = [0]


def _tx2_daily(code):
    """腾讯双节点直连日K(qfq前复权, 与东财fqt=1同口径); 东财被临时封时的兜底
    额=量(手)×100×均价 近似(与生产腾讯回退同口径)"""
    if code == IDX:
        txc = "sh000001"
    else:
        txc = ("sh" if code[0] in "56" else "sz") + code
    _TX2_TURN[0] += 1
    host = _TX2_HOSTS[_TX2_TURN[0] % len(_TX2_HOSTS)]
    url = f"{host}/appstock/app/fqkline/get?param={txc},day,,,{TX_LMT},qfq"
    r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    d = (r.json().get("data") or {}).get(txc) or {}
    rows = d.get("qfqday") or d.get("day") or []
    bars = []
    for row in rows:
        try:
            o, c, h, l, v = (float(row[1]), float(row[2]), float(row[3]),
                             float(row[4]), float(row[5]))
        except (ValueError, IndexError):
            continue
        bars.append({"dt": str(row[0]), "open": o, "close": c, "high": h,
                     "low": l, "vol": v, "amount": round(v * 100 * (h + l + c) / 3, 2)})
    return bars


def fetch_daily(code):
    """日K拉取: 生产级(东财多轮重试->腾讯ifzq) -> 腾讯双节点兜底"""
    try:
        bars = quotes._fetch_kline_raw(code, 101, TX_LMT, beg=BEG)
        if bars:
            return bars
    except Exception:
        pass
    try:
        return _tx2_daily(code)
    except Exception:
        return []


def build_universe(refresh=False):
    """行业板块成分股(成交额前15/板) -> 去重/剔ST与无权限; 快照缓存当日有效"""
    if not refresh and os.path.exists(UNI_CACHE):
        d = json.load(open(UNI_CACHE, encoding="utf-8"))
        if d.get("end") == END:
            return d["members"]
    members, seen = [], set()
    boards = quotes.fetch_board_list("industry", n=95)
    print(f"[宇宙] 行业板块 {len(boards)} 个")
    for b in boards:
        try:
            ms = quotes.fetch_board_members(b["bk"], n=15)
        except Exception as e:
            print(f"[宇宙] 成分股获取失败 {b['name']}: {e}")
            continue
        time.sleep(0.35)
        for m in ms:
            c = m["code"]
            if c in seen or c.startswith(EXCLUDE_PREFIX):
                continue
            if "ST" in (m["name"] or "").upper():
                continue
            seen.add(c)
            members.append({"code": c, "name": m["name"], "board": b["name"]})
    os.makedirs(TMP, exist_ok=True)
    json.dump({"end": END, "members": members},
              open(UNI_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return members


def load_bars(members, refresh=False):
    cache = {}
    if not refresh and os.path.exists(BARS_CACHE):
        d = json.load(open(BARS_CACHE, encoding="utf-8"))
        if d.get("beg") == BEG and d.get("end") == END:
            cache = d["data"]
    codes = {IDX: "上证指数"}
    codes.update({m["code"]: m["name"] for m in members})
    todo = [c for c in codes if c not in cache]
    dirty, fail = False, 0
    for i, code in enumerate(todo):
        bars = fetch_daily(code)
        time.sleep(0.22)              # 节流: 防腾讯源连续快速请求被WAF拦截
        if code == IDX:
            if bars:
                cache[code] = {"name": "上证指数", "bars": bars}
                dirty = True
            else:
                print("[行情] 指数K线获取失败, 终止")
                sys.exit(1)
            continue
        if len(bars) < 150:            # 上市过晚/拉取失败: 无预热所需历史
            fail += 1
            continue
        cache[code] = {"name": codes[code], "bars": bars}
        dirty = True
        if (i + 1) % 50 == 0:
            print(f"[行情] {i + 1}/{len(todo)} 累计{len(cache) - 1}只 fail={fail}")
    # 失败重试一轮(腾讯源偶发限流, 缓存重启进程也可增量补)
    retry = [c for c in codes if c not in cache and c != IDX]
    for j, code in enumerate(retry):
        time.sleep(0.5)
        bars = fetch_daily(code)
        if len(bars) >= 150:
            cache[code] = {"name": codes[code], "bars": bars}
            dirty = True
        if (j + 1) % 50 == 0:
            print(f"[重试] {j + 1}/{len(retry)}")
    fail = len([c for c in codes if c not in cache and c != IDX])
    if fail:
        print(f"[行情] {fail}只未取得(上市晚或无数据)")
    if dirty:
        json.dump({"beg": BEG, "end": END, "data": cache},
                  open(BARS_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return cache


def prep(code, name, bars, board=""):
    """预计算指标与因子原料(全部只用截至当根收盘, 无未来)"""
    ind = build_ind(bars)
    closes = [b["close"] for b in bars]
    n = len(bars)
    vol20 = [None] * n
    for i in range(19, n):
        rs = [closes[k] / closes[k - 1] - 1 for k in range(i - 19, i + 1)]
        m = sum(rs) / 20
        vol20[i] = (sum((x - m) ** 2 for x in rs) / 20) ** 0.5
    return {"code": code, "name": name, "bars": bars, "ind": ind,
            "closes": closes, "vol20": vol20, "board": board,
            "dates": [b["dt"][:10] for b in bars],
            "dmap": {b["dt"][:10]: j for j, b in enumerate(bars)}}


def _lp(code):
    return 0.198 if code[:3] in ("300", "301", "688", "689") else 0.098


def snap(st, j, code):
    """收盘j因子快照; 指标未就绪返回None"""
    ind = st["ind"]
    cl = st["closes"][j]
    ma20, ma5, ma10, ma60 = (ind["ma20"][j], ind["ma5"][j],
                             ind["ma10"][j], ind["ma60"][j])
    if None in (ma20, ma5, ma10, ma60) or j < 130:
        return None
    closes = st["closes"]
    ma20up = ind["ma20"][j - 5] is not None and ma20 > ind["ma20"][j - 5]
    mom20 = closes[j] / closes[j - 20] - 1
    m120 = closes[j - 10] / closes[j - 130] - 1
    r5 = closes[j] / closes[j - 5] - 1
    amt20 = sum(b["amount"] for b in st["bars"][j - 19:j + 1]) / 20
    w = st["bars"][j - 9:j + 1]
    lu = sum(1 for k in range(1, len(w))
             if w[k]["close"] / w[k - 1]["close"] - 1 >= _lp(code))
    extreme = lu >= 3 or mom20 >= 1.0 or r5 >= 0.30
    # ---- 2026-11-18 图片策略融合: 日K代理因子(全用截至j收盘数据, 无未来) ----
    w5 = st["bars"][j - 4:j + 1]
    v20 = sum(b["vol"] for b in st["bars"][j - 19:j + 1])
    vr = (sum(b["vol"] for b in w5) / 5.0) / (v20 / 20.0) if v20 > 0 else None
    hh20 = max(closes[j - 19:j + 1])
    pos20 = cl / hh20 - 1 if hh20 > 0 else None
    cps = [(b["close"] - b["low"]) / (b["high"] - b["low"])
           for b in w5 if b["high"] > b["low"]]
    cpos5 = sum(cps) / len(cps) if cps else 0.5
    lamt = math.log(amt20) if amt20 > 0 else None
    return {"cl": cl, "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
            "ma20up": ma20up, "mom20": mom20, "m120": m120, "r5": r5,
            "bias": cl / ma20 - 1, "amt20": amt20, "vol20": st["vol20"][j],
            "extreme": extreme, "vr": vr, "pos20": pos20, "cpos5": cpos5,
            "lamt": lamt}


def _zscore(cands, key, sign=1.0):
    """截面z-score(None容错: 缺失记z=0中性, 不影响mu/sd; 与生产_zscore_v3同口径)"""
    xs = [sign * c["sp"][key] for c in cands if c["sp"].get(key) is not None]
    if not xs:
        for c in cands:
            c["z_" + key] = 0.0
        return
    mu = sum(xs) / len(xs)
    sd = (sum((x - mu) ** 2 for x in xs) / len(xs)) ** 0.5
    for c in cands:
        v = c["sp"].get(key)
        c["z_" + key] = ((sign * v - mu) / sd if sd > 0 else 0.0
                         ) if v is not None else 0.0


def _board_series(U, cal):
    """板块等权日收益序列(对齐指数日历; 当日>=3只成员有行情才记值)
    -> {board: {date: eq_weight_daily_ret}}"""
    groups = {}
    for code, st in U.items():
        if code != IDX and st.get("board"):
            groups.setdefault(st["board"], []).append(st)
    out = {}
    for bname, sts in groups.items():
        rets = {}
        for i in range(1, len(cal)):
            d, pd = cal[i], cal[i - 1]
            rs = []
            for st in sts:
                j, pj = st["dmap"].get(d), st["dmap"].get(pd)
                if j is not None and pj is not None and st["closes"][pj] > 0:
                    rs.append(st["closes"][j] / st["closes"][pj] - 1)
            if len(rs) >= 3:
                rets[d] = sum(rs) / len(rs)
        out[bname] = rets
    return out


def _lnk_sb(st, j, bret_b):
    """股板同向率: 近20交易日个股与板块等权日收益同向(同涨同跌)天数占比
    <10个有效日返回None(记中性); 高=板块驱动, 低=独立行情"""
    if not bret_b:
        return None
    agree = n = 0
    for k in range(j - 19, j + 1):
        br = bret_b.get(st["dates"][k])
        if br is None or st["closes"][k - 1] <= 0:
            continue
        sr = st["closes"][k] / st["closes"][k - 1] - 1
        n += 1
        if (sr > 0) == (br > 0):
            agree += 1
    return agree / n if n >= 10 else None


def _lnk_bm(bret_b, cal, ic, ti):
    """板市同向率: 近20交易日板块等权日收益与大盘指数同向天数占比
    <10个有效日返回None(记中性); 高=与大盘共振"""
    if not bret_b:
        return None
    agree = n = 0
    for k in range(max(1, ti - 19), ti + 1):
        br = bret_b.get(cal[k])
        if br is None or ic[k - 1] <= 0:
            continue
        mr = ic[k] / ic[k - 1] - 1
        n += 1
        if (br > 0) == (mr > 0):
            agree += 1
    return agree / n if n >= 10 else None


def simulate(U, cal, V):
    """组合级模拟: 调仓(收盘决策)->次日开盘成交; 退出同口径"""
    P = dict(DEFAULTS)
    P.update(V.get("params") or {})
    cd = V.get("cooldown", Q.PICK_COOLDOWN)
    cal_idx = {d: i for i, d in enumerate(cal)}
    ic = [U[IDX]["closes"][i] for i in range(len(cal))]

    def _sma(w):
        out = [None] * len(cal)
        s = 0.0
        for i in range(len(cal)):
            s += ic[i]
            if i >= w:
                s -= ic[i - w]
            if i >= w - 1:
                out[i] = s / w
        return out

    isma = {20: _sma(20), 60: _sma(60)}
    cash = CAPITAL
    pos, pend_buy, pend_sell = {}, [], set()
    last_sell, last_reb = {}, -999
    trades, equity, picks_log = [], [], []

    # 模拟窗口: 日历末段SIM_DAYS日(0=全窗口); 预热指标仍由WARM前全历史计算
    t0 = max(WARM, len(cal) - SIM_DAYS) if SIM_DAYS else WARM
    for ti in range(t0, len(cal)):
        t = cal[ti]
        # ① 开盘执行挂单
        for pb in [x for x in pend_buy]:
            st = U.get(pb["code"])
            j = st["dmap"].get(t) if st else None
            if j is None:
                continue                       # 停牌顺延
            o = st["bars"][j]["open"]
            q = int(pb["alloc"] / o // 100 * 100)
            while q >= 100:
                amt = round(q * o, 2)
                if amt + fees.buy_fee(amt, pb["code"]) <= pb["alloc"]:
                    break
                q -= 100
            if q < 100 or round(q * o, 2) + fees.buy_fee(round(q * o, 2), pb["code"]) > cash:
                pend_buy.remove(pb)
                continue
            amt = round(q * o, 2)
            f = fees.buy_fee(amt, pb["code"])
            cash -= amt + f
            pos[pb["code"]] = {"sh": q, "cost": amt + f, "entry": o, "hmax": o,
                               "bi": j, "date": t, "last_j": j}
            pend_buy.remove(pb)
        for code in [x for x in pend_sell]:
            st = U.get(code)
            j = st["dmap"].get(t) if st else None
            if j is None:
                continue
            p = pos.get(code)
            if not p:
                pend_sell.discard(code)
                continue
            o = st["bars"][j]["open"]
            amt = round(p["sh"] * o, 2)
            f = fees.sell_fee(amt, code)
            cash += amt - f
            trades.append({"code": code, "name": st["name"], "sh": p["sh"],
                           "cost": p["cost"], "entry": p["entry"],
                           "buy_date": p["date"], "sell_date": t, "sell_px": o,
                           "pnl": amt - f - p["cost"],
                           "ret": (amt - f) / p["cost"] - 1,
                           "reason": p.get("exit_sig", "")})
            del pos[code]
            last_sell[code] = t
            pend_sell.discard(code)
        # ② 收盘退出检查(阶梯止盈/止损 + 变体退出)
        for code, p in pos.items():
            if code in pend_sell:
                continue
            st = U[code]
            j = st["dmap"].get(t)
            if j is None or j < p["bi"]:
                continue
            cl = st["closes"][j]
            p["hmax"] = max(p["hmax"], cl)
            ent, pc = p["entry"], cl
            sig = None
            pf = pc / ent - 1
            if not V.get("no_trail"):
                for th, bw in TRAIL:
                    if pf >= th:
                        if pc <= p["hmax"] * (1 - bw):
                            sig = f"移动止盈{bw * 100:.0f}%"
                        break
            if not sig:
                pfh = p["hmax"] / ent - 1
                for th, ln in STOPT:
                    if pfh >= th:
                        if pc < ent * ln:
                            sig = "阶梯止损"
                        break
            if not sig:
                if V["exit"] == "ma10" and cl < st["ind"]["ma10"][j]:
                    sig = "破MA10"
                elif V["exit"] == "ma20" and cl < st["ind"]["ma20"][j]:
                    sig = "破MA20"
                elif V["exit"] == "chand":
                    hh = max(st["closes"][j - 21:j + 1])
                    atr = st["ind"]["atr"][j]
                    if atr and cl < hh - P["chand_k"] * atr:
                        sig = "吊灯止损"
            if sig:
                p["exit_sig"] = sig
                pend_sell.add(code)
        # ③ 收盘调仓判定
        n_busy = len(pos) + len(pend_buy)
        due = ti - last_reb >= Q.PICK_REBAL
        early = n_busy < Q.PICK_MIN_HOLD
        if due or early:
            last_reb = ti
            cands = []
            for code, st in U.items():
                if code == IDX or code in pos:
                    continue
                if any(pb["code"] == code for pb in pend_buy):
                    continue
                j = st["dmap"].get(t)
                if j is None:
                    continue
                sp = snap(st, j, code)
                if sp is None:
                    continue
                if sp["cl"] < Q.PICK_MIN_PRICE or sp["amt20"] < Q.PICK_MIN_AMOUNT:
                    continue
                ls = last_sell.get(code)
                if ls and cal_idx[t] - cal_idx[ls] < cd:
                    continue
                if V["entry"] == "v25":
                    ok = (sp["cl"] > sp["ma20"] and sp["ma5"] > sp["ma10"]
                          and sp["cl"] > sp["ma60"] and sp["ma20up"]
                          and sp["mom20"] > Q.PICK_MOM_MIN)
                    if not ok:
                        continue
                    cands.append({"code": code, "name": st["name"], "sp": sp,
                                  "score": sp["mom20"],
                                  "extreme": sp["extreme"]})
                else:
                    ok = (sp["cl"] > sp["ma20"] and sp["cl"] > sp["ma60"]
                          and sp["ma20up"] and sp["m120"] >= P["mom_min"]
                          and sp["bias"] <= P["bias_max"]
                          and sp["r5"] <= P["r5_max"]
                          and sp["mom20"] <= P["r20_max"])
                    if not ok:
                        continue
                    cands.append({"code": code, "name": st["name"], "sp": sp,
                                  "j": j, "extreme": sp["extreme"]})
            if V["entry"] == "v3" and cands:
                w = P["w"]
                # ---- 板块层因子(2026-11-18 三层联动) ----
                # bd 板块动量: 板内当日有效成员(>=3只)的20日涨幅均值
                # lbm 板市同向率 / lsb 股板同向率(见函数docstring)
                acc = {}
                for code2, st2 in U.items():
                    if code2 == IDX or not st2.get("board"):
                        continue
                    jj = st2["dmap"].get(t)
                    if jj is not None and jj >= 20 and st2["closes"][jj - 20] > 0:
                        acc.setdefault(st2["board"], []).append(
                            st2["closes"][jj] / st2["closes"][jj - 20] - 1)
                bmean = {b: sum(v) / len(v) for b, v in acc.items() if len(v) >= 3}
                # ---- 2026-11-19 图片指标原口径: irank/brank/btier ----
                # irank 候选mom20在其板内有效成员分位 / brank 板块动量在板块间分位
                # btier 板块非单调档位: 温和[cold,hot]=+1, 过热/冷淡=-1
                use_ibt = any(w.get(k, 0) for k in ("irank", "brank", "btier"))
                bmv = sorted(bmean.values()) if (use_ibt and bmean) else []
                hot, cold = P["btier_hot"], P["btier_cold"]
                lbm_cache = {}
                for c in cands:
                    st = U[c["code"]]
                    bd_ = st.get("board")
                    if bd_ not in lbm_cache:
                        lbm_cache[bd_] = _lnk_bm(BRET.get(bd_), cal, ic, ti)
                    c["sp"]["bmom"] = bmean.get(bd_)
                    c["sp"]["lnk_bm"] = lbm_cache[bd_]
                    c["sp"]["lnk_sb"] = _lnk_sb(st, c["j"], BRET.get(bd_))
                    if use_ibt:
                        vals = acc.get(bd_) or []
                        if bd_ in bmean and len(vals) > 1:
                            c["sp"]["irank"] = (
                                sum(1 for v in vals if v < c["sp"]["mom20"])
                                / (len(vals) - 1))
                            lo_b = sum(1 for v in bmv if v < bmean[bd_])
                            c["sp"]["brank"] = (lo_b / (len(bmv) - 1)
                                                if len(bmv) > 1 else 0.5)
                            bm = bmean[bd_]
                            c["sp"]["btier"] = (1.0 if cold <= bm <= hot else -1.0)
                        else:
                            c["sp"]["irank"] = c["sp"]["brank"] = None
                            c["sp"]["btier"] = None
                for key, (src, sign) in FACTORS.items():
                    if w.get(key, 0):
                        _zscore(cands, src, sign)
                if w.get("act", 0):   # 活跃度适中: 距截面流动性档位中位越近越好
                    _zscore(cands, "lamt", 1.0)
                    for c in cands:
                        c["z_act"] = -abs(c["z_lamt"])
                for c in cands:
                    c["score"] = sum(w[k] * c["z_" + FACTORS[k][0]]
                                     for k in FACTORS if w.get(k, 0))
                    if w.get("act", 0):
                        c["score"] += w["act"] * c["z_act"]
            cands.sort(key=lambda c: (c["extreme"], -c["score"]))
            gate_ok = True
            if V.get("mkt_gate"):
                gm = isma[V.get("gate_ma", 20)][ti]
                gate_ok = gm is None or ic[ti] > gm
            max_new = min(Q.PICK_TOPK, Q.PICK_MAX_HOLD - n_busy)
            picks = []
            if gate_ok and max_new > 0:
                picks = [c for c in cands if not c["extreme"]][:max_new] or cands[:max_new]
            picks_log.append({"date": t, "trig": "early" if early else "due",
                              "gate": gate_ok, "cands": len(cands),
                              "picks": [{"code": c["code"], "name": c["name"],
                                         "score": round(c["score"], 3),
                                         "mom20": round(c["sp"]["mom20"], 3),
                                         "m120": round(c["sp"]["m120"], 3),
                                         "bias": round(c["sp"]["bias"], 3)}
                                        for c in picks]})
            if picks:
                alloc = cash / len(picks)
                for c in picks:
                    if alloc < c["sp"]["cl"] * 100 * 1.01:
                        continue
                    pend_buy.append({"code": c["code"], "alloc": alloc})
        # ④ 逐日盯市
        mv = 0.0
        for code, p in pos.items():
            st = U[code]
            j = st["dmap"].get(t)
            if j is not None:
                p["last_j"] = j
            mv += p["sh"] * st["closes"][p["last_j"]]
        equity.append(cash + mv)

    # 期末未平仓按最后收盘估值并入trades统计(标记open)
    for code, p in pos.items():
        st = U[code]
        px = st["closes"][p["last_j"]]
        amt = p["sh"] * px
        trades.append({"code": code, "name": st["name"], "sh": p["sh"],
                       "cost": p["cost"], "entry": p["entry"], "buy_date": p["date"],
                       "sell_date": cal[-1], "sell_px": px, "open": True,
                       "pnl": amt - p["cost"], "ret": amt / p["cost"] - 1,
                       "reason": "期末持仓" + (("(" + p.get("exit_sig", "") + ")")
                                            if code in pend_sell else "")})
    return {"trades": trades, "equity": equity, "picks_log": picks_log,
            "cal": cal[t0:], "cash": cash, "npos": len(pos)}


def metrics(res):
    eq = res["equity"]
    cal = res["cal"]
    cal_idx = {d: i for i, d in enumerate(cal)}
    total = eq[-1] / CAPITAL - 1 if eq else 0.0
    days = len(eq)
    ann = (eq[-1] / CAPITAL) ** (244 / max(days, 1)) - 1 if eq and days > 5 else 0.0
    mdd, peak = 0.0, eq[0] if eq else 1
    for v in (eq or [1]):
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    half = max(days // 2, 1)
    h1 = eq[half] / eq[0] - 1 if days > 10 else 0.0
    h2 = eq[-1] / eq[half] - 1 if days > 10 else 0.0
    closed = [t for t in res["trades"] if not t.get("open")]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    winr = len(wins) / len(closed) if closed else 0.0
    aw = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
    al = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0
    pf = (sum(t["pnl"] for t in wins) / -sum(t["pnl"] for t in losses)
          if losses and sum(t["pnl"] for t in losses) < 0 else float("inf"))
    hold = (sum(cal_idx[t["sell_date"]] - cal_idx[t["buy_date"]]
                for t in closed) / len(closed) if closed else 0.0)
    wins_pnl = sorted((t["pnl"] for t in closed if t["pnl"] > 0), reverse=True)
    net = sum(t["pnl"] for t in closed)
    conc = (sum(wins_pnl[:3]) / net) if net > 0 and wins_pnl else 0.0
    return {"total": total, "ann": ann, "mdd": mdd, "n": len(closed),
            "winr": winr, "aw": aw, "al": al, "pf": pf, "hold": hold,
            "h1": h1, "h2": h2, "conc": conc,
            "open": len(res["trades"]) - len(closed)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--log", default="")
    ap.add_argument("--days", type=int, default=None,
                    help="模拟窗口交易日数(0=全窗口, 默认=SIM_DAYS现行120)")
    ap.add_argument("--perturb", action="store_true",
                   help="R3参数扰动变体集(权重档+btier阈值+组合)")
    ap.add_argument("--scan", action="store_true",
                    help="R5全参数扫描: 图片全因子(bd/act/pos/cpos/lsb/lbm/"
                         "irank/btier)+brank权重档+策略参数(chand/r5/r20)+btier阈值")
    args = ap.parse_args()
    global SIM_DAYS
    if args.days is not None:
        SIM_DAYS = args.days
    members = build_universe(refresh=args.refresh)
    print(f"[宇宙] 成分股去重后 {len(members)} 只")
    data = load_bars(members, refresh=args.refresh)
    bmap = {m["code"]: m["board"] for m in members}
    U = {c: prep(c, v["name"], v["bars"], bmap.get(c, ""))
         for c, v in data.items()}
    cal = U[IDX]["dates"]
    global BRET
    BRET = _board_series(U, cal)
    t0 = max(WARM, len(cal) - SIM_DAYS) if SIM_DAYS else WARM
    print(f"[行情] 就绪 {len(U)} 只(含指数), 交易日 {len(cal)} 根, "
          f"模拟窗口 {cal[t0]} ~ {cal[-1]} ({len(cal) - t0}日)")

    # 基线=v5现行六因子(2026-11-19 brank采纳: v4内核五因子+行业分位0.5)
    F0 = {"chand_k": 2.0, "r5_max": 0.06, "r20_max": 0.45,
          "w": {"mom": 0.5, "rev": 1.0, "vol": 1.0, "pull": 0.5, "vr": 0.5,
                "brank": 0.5}}

    def _v(nm, **kw):
        w = dict(F0["w"])
        w.update(kw.pop("w", {}))
        cd = kw.pop("cd", None)
        V = {"name": nm, "entry": "v3", "exit": "chand",
             "mkt_gate": kw.pop("gate", 20), "params": {**F0, "w": w, **kw}}
        if cd is not None:
            V["cooldown"] = cd            # 缺省回落Q.PICK_COOLDOWN
        return V

    # 2026-11-19 第四轮R1/R2: 三因子单加(0.5/1.0档) vs v4基线(已完成, 结论留档)
    VARIANTS = [
        _v("F0基线(v5现行)"),
        _v("S·irank0.5", w={"irank": 0.5}),
        _v("S·brank0.25", w={"brank": 0.25}),
        _v("S·btier0.5", w={"btier": 0.5}),
    ]
    # R3参数扰动: 权重0.25/0.75档 + btier_hot阈值8%/12%/15% + 因子组合
    if args.perturb:
        # R5轮C(2026-11-19, 多周期对比): 轮B赢家=chand降档(30日窗口chand1.0~1.5
        # 均-6.30% vs 基线-8.28%, 门槛/冷却不分化; gate_off=-14.5%门控证伪)
        # -> chand梯度+gate_off在120日/全窗口复验, 与30日结果合并出三周期对比表
        VARIANTS = [
            _v("F0基线(v5现行)"),
            _v("C·chand1.0", chand_k=1.0),
            _v("C·chand1.25", chand_k=1.25),
            _v("C·chand1.5", chand_k=1.5),
            _v("C·chand1.75", chand_k=1.75),
            _v("C·gate_off", gate=0),
        ]
    # R5全参数扫描(2026-11-19用户指令: 30日窗口+全参数迭代+多周期对比):
    # 图片全因子单加0.5(含此前否决的bd/act/pos/cpos/lsb/lbm) + brank权重档
    # + 策略参数(吊灯系数/5日/20日涨幅上限) + btier过热阈值
    elif args.scan:
        VARIANTS = [
            _v("F0基线(v5现行)"),
            _v("S·bd0.5", w={"bd": 0.5}),
            _v("S·act0.5", w={"act": 0.5}),
            _v("S·pos0.5", w={"pos": 0.5}),
            _v("S·cpos0.5", w={"cpos": 0.5}),
            _v("S·lsb0.5", w={"lsb": 0.5}),
            _v("S·lbm0.5", w={"lbm": 0.5}),
            _v("S·irank0.5", w={"irank": 0.5}),
            _v("S·btier0.5", w={"btier": 0.5}),
            _v("S·brank0.25", w={"brank": 0.25}),
            _v("S·brank0.75", w={"brank": 0.75}),
            _v("S·brank1.0", w={"brank": 1.0}),
            _v("S·chand1.5", chand_k=1.5),
            _v("S·chand2.5", chand_k=2.5),
            _v("S·r5max4%", r5_max=0.04),
            _v("S·r5max8%", r5_max=0.08),
            _v("S·r20max35%", r20_max=0.35),
            _v("S·r20max55%", r20_max=0.55),
            _v("S·btier0.5·hot8%", w={"btier": 0.5}, btier_hot=0.08),
            _v("S·btier0.5·hot12%", w={"btier": 0.5}, btier_hot=0.12),
            _v("S·btier0.5·hot15%", w={"btier": 0.5}, btier_hot=0.15),
        ]
    idx_ret = U[IDX]["closes"][-1] / U[IDX]["closes"][t0] - 1
    ew = []
    for c, st in U.items():
        if c == IDX:
            continue
        j0 = max((st["dmap"].get(d, -1) for d in cal[t0:t0 + 5]), default=-1)
        if j0 and j0 > 0 and st["closes"][-1] > 0:
            ew.append(st["closes"][-1] / st["closes"][j0] - 1)
    print(f"[基准] 上证指数 {idx_ret * 100:+.2f}% | 宇宙等权 "
          f"{sum(ew) / len(ew) * 100:+.2f}% ({len(ew)}只)\n")

    hdr = (f"{'变体':<24}{'总收益':>9}{'年化':>8}{'MDD':>8}{'H1':>8}{'H2':>8}"
           f"{'笔数':>5}{'胜率':>7}{'PF':>6}{'集中度':>7}{'均持':>6}")
    print(hdr)
    print("-" * 105)
    results = {}
    for V in VARIANTS:
        res = simulate(U, cal, V)
        m = metrics(res)
        results[V["name"]] = (res, m)
        print(f"{V['name']:<24}{m['total'] * 100:>+8.2f}%{m['ann'] * 100:>+7.1f}%"
              f"{m['mdd'] * 100:>7.1f}%{m['h1'] * 100:>+7.1f}%{m['h2'] * 100:>+7.1f}%"
              f"{m['n']:>5}{m['winr'] * 100:>6.1f}%{m['pf']:>6.2f}"
              f"{m['conc'] * 100:>6.0f}%{m['hold']:>6.1f}"
              + (f" 持{m['open']}" if m["open"] else ""))
    if args.log:
        for name, (res, m) in results.items():
            if args.log in name:
                print(f"\n[{name}] 调仓明细:")
                for pl in res["picks_log"]:
                    ps = ", ".join(f"{p['name']}(s={p['score']},mom20={p['mom20'] * 100:+.0f}%,"
                                   f"m120={p['m120'] * 100:+.0f}%,bias={p['bias'] * 100:+.0f}%)"
                                   for p in pl["picks"]) or "-"
                    print(f"  {pl['date']} [{pl['trig']}] gate={pl['gate']} "
                          f"cands={pl['cands']}: {ps}")
                print("  成交明细:")
                for t in res["trades"]:
                    print(f"    {t['buy_date']}买{t['name']}@{t['entry']:.2f} -> "
                          f"{t['sell_date']}卖@{t['sell_px']:.2f} "
                          f"{t['ret'] * 100:+.1f}% {t['reason']}"
                          + ("(open)" if t.get("open") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
