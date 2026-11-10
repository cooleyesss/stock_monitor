# -*- coding: utf-8 -*-
"""日内T独立看板(HTML): report/dashboard_ttrade.html
纯日内T内容, 与量化独立账户的 dashboard_quant.html 完全独立(不同文件, 互不覆盖):
  账户概况卡片 + 日内T盈亏记录 + 今日做T交易 + 持仓概览 + 策略统计
  + 每日总资产/每日净T盈亏曲线 + 持仓标的日K与做T买卖点(鼠标悬停看当日详情)
生成时机: 收盘战报(strategy.do_eod)之后, 15:00 后自动生成
数据源: data/ttrade/sim_state.json + sim_trades.csv + trades.log(逐日总资产/累计净)
"""
import csv
import datetime
import json
import os
import re

from ttrade import config_ttrade as config
from common import quotes
from core.indicators import build_ind
from ttrade import account
from ttrade import vote

KLT_DAILY, BARS_LMT = 101, 120
DASH_PATH = os.path.join(config.REPORT_DIR, "ttrade", "dashboard_ttrade.html")
os.makedirs(os.path.dirname(DASH_PATH), exist_ok=True)   # report/ttrade/ 子目录(2026-10-28用户要求与量化日报分离)


def _f(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------- 做T流水(买卖点) ----------------
def _kind_of(side_txt):
    """做T方向文本 -> 图上的点型(BUY/SELL/STOP); 止损优先判定"""
    if "止损" in side_txt:
        return "STOP"
    if "买" in side_txt or "低吸" in side_txt:
        return "BUY"
    return "SELL"


def _load_ops():
    """做T成交流水 -> {code: [{date,time,kind,side_txt,qty,price,reason,ai}]}"""
    out = {}
    if not os.path.exists(config.TRADES_CSV):
        return out
    try:
        with open(config.TRADES_CSV, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = (row.get("代码") or "").strip()
                if not code:
                    continue
                try:
                    price = float(row.get("价格") or 0)
                    qty = int(float(row.get("股数") or 0))
                except ValueError:
                    continue
                reason, _, ai = (row.get("备注") or "").partition(" | AI:")
                out.setdefault(code, []).append({
                    "date": (row.get("日期") or "")[:10],
                    "time": (row.get("时间") or "")[:8],
                    "kind": _kind_of(row.get("方向") or ""),
                    "side_txt": (row.get("方向") or "").strip(),
                    "qty": qty, "price": price,
                    "reason": reason.strip(), "ai": ai.strip()})
    except Exception as e:
        print("[dashboard_ttrade] 做T流水读取失败:", e)
    return out


# ---------------- 逐日总资产/净T盈亏(从 trades.log 解析) ----------------
_HDR = re.compile(r"收盘·日内T盈亏记录")
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_NET = re.compile(r"今日净:\s*([-+]?[\d,]+\.?\d*)")
_CUM = re.compile(r"累计净:\s*([-+]?[\d,]+\.?\d*)\s*\|\s*总资产:\s*([-+]?[\d,]+\.?\d*)")
_CASH = re.compile(r"现金\s*([\d,]+\.?\d*)")
_FEE = re.compile(r"费用:\s*([\d,]+\.?\d*)")


def _load_daily():
    """trades.log 的每日收盘块 -> [{date,total,net,cum,cash,fee}](同日只留最后一次)
    cash=该日最后一笔逐笔成交的现金(2026-11-09用户需求: 供卡片昨日环比);
    fee=该日收盘块的当日费用"""
    if not os.path.exists(config.TRADES_LOG):
        return []
    by_date, cur = {}, None

    def _day(d):
        return by_date.setdefault(
            d, {"date": d, "total": None, "net": 0.0, "cum": None,
                "cash": None, "fee": None})

    try:
        with open(config.TRADES_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                if _HDR.search(line):
                    m = _DATE.search(line)
                    cur = None
                    if m:
                        cur = _day(m.group(1))
                    continue
                # 逐笔成交行: 按行内日期归属更新现金(独立于cur, 防跨日错挂)
                m3 = _CASH.search(line)
                if m3:
                    dm = _DATE.search(line)
                    if dm:
                        v = _f(m3.group(1))
                        if v is not None:
                            _day(dm.group(1))["cash"] = v
                if cur is None:
                    continue
                m = _NET.search(line)
                if m and "费用:" in line:
                    v = _f(m.group(1))
                    if v is not None:
                        cur["net"] = v
                    m4 = _FEE.search(line)
                    if m4:
                        cur["fee"] = _f(m4.group(1))
                m2 = _CUM.search(line)
                if m2:
                    cur["cum"] = _f(m2.group(1))
                    cur["total"] = _f(m2.group(2))
    except Exception as e:
        print("[dashboard_ttrade] trades.log读取失败:", e)
    since = getattr(config, "DASH_SINCE", "")   # 展示窗口起点(与项目看板口径一致)
    return [by_date[k] for k in sorted(by_date)
            if by_date[k]["total"] and by_date[k]["date"] >= since]


# ---------------- 当日T盈亏/策略统计(同 build_eod_summary 口径) ----------------
def _today_t(state, px, ops):
    """日内T盈亏记录: [(name, 已回补, 未回补浮动, 合计, 备注)] + 汇总 + code映射"""
    names = {t["code"]: t["name"] for t in state["today_trades"]}
    rows, info, realized, floating = [], {}, 0.0, 0.0
    for code, led in state["today_ledger"].items():
        if led["bq"] == 0 and led["sq"] == 0:
            continue
        price = px.get(code) or state.get("last_prices", {}).get(code, 0)
        pnl = account.t_pnl(led, price)
        rz, fl = account.t_pnl_detail(led, price)
        realized += rz
        floating += fl
        net = led["bq"] - led["sq"]
        extra = (f"{'多买' if net > 0 else '多卖'}{abs(net)}股未回补" if net else "")
        name = (state["positions"].get(code, {}).get("name")
                or names.get(code) or code)
        rows.append((name, rz, fl, pnl, extra))
        info[code] = rows[-1]
    gross = realized + floating
    fee = state["today_fee"]
    return rows, {"realized": realized, "floating": floating, "gross": gross,
                  "fee": fee, "net": gross - fee}, info


def _stats(state, px):
    """策略归因统计(启发式, 与收盘战报完全同口径)"""
    trades = state["today_trades"]
    s_pairs, s_opens = account.fifo_pairs(trades)
    lp = dict(px)
    lpm = state.get("last_prices") or {}
    for k, v in lpm.items():
        lp.setdefault(k, v)
    SKEYS = list(vote.VOTER_LABEL.keys()) + ["none"]   # 从投票器字典构建, 防新增投票器漏key致KeyError
    SLBL = dict(vote.VOTER_LABEL)
    SLBL["none"] = "无归因/旧策略"
    LBL2KEY = {v: k for k, v in SLBL.items()}
    st = {k: {"trig": 0, "exec": 0, "wait": 0, "realized": 0.0,
              "floating": 0.0, "win": 0, "pair": 0} for k in SKEYS}
    for v in state.get("today_vote_log") or []:
        d = 1 if v.get("side") == "buy" else -1
        for lbl, sc in (v.get("votes") or {}).items():
            k = LBL2KEY.get(lbl)
            if k and sc * d >= config.QUANT_MIN_VOTE:
                st[k]["trig"] += 1
        k = vote.tag_key(v.get("tag", "")) or "none"
        st[k]["exec" if v.get("executed") else "wait"] += 1
    for pp in s_pairs:
        k = vote.tag_key(pp["tag"]) or "none"
        st[k]["realized"] += pp["pnl"]
        st[k]["pair"] += 1
        if pp["pnl"] > 0:
            st[k]["win"] += 1
    for o in s_opens:
        k = vote.tag_key(o["tag"]) or "none"
        p = lp.get(o["code"], o["price"])
        diff = (p - o["price"]) if o["side"] == "buy" else (o["price"] - p)
        st[k]["floating"] += o["qty"] * diff - o["fps"] * o["qty"]
    out = []
    for k in SKEYS:
        s = st[k]
        if not (s["trig"] or s["pair"] or s["exec"] or s["wait"] or s["floating"]):
            continue
        out.append({"label": SLBL[k], "trig": s["trig"], "exec": s["exec"],
                    "wait": s["wait"], "realized": round(s["realized"], 2),
                    "floating": round(s["floating"], 2),
                    "win": s["win"], "pair": s["pair"]})
    return out


# ---------------- 持仓标的日K/做T点 ----------------
def _stock_payload(code, pos, bars, ops, px):
    ind = build_ind(bars)                          # 均线用全量历史算
    since = getattr(config, "DASH_SINCE", "")
    k0 = next((k for k, b in enumerate(bars) if b["dt"][:10] >= since), 0)
    win = bars[k0:]
    wdates = {b["dt"][:10] for b in win}
    in_win = [op for op in ops if op["date"] in wdates]
    cl = bars[-1]["close"]
    prev0 = bars[k0 - 1]["close"] if k0 > 0 else None
    chg = []
    for i, b in enumerate(win):
        pc = win[i - 1]["close"] if i > 0 else prev0
        chg.append(round(b["close"] / pc - 1, 4) if pc else None)
    bc = account.basis_cost(pos)   # 整体累计基准成本(2026-11-03): 清仓接回不重置
    return {"name": pos["name"], "sh": pos["shares"], "cost": bc,
            "close": cl,
            "pnl_pct": ((cl / bc - 1) * 100) if bc else None,
            "float_pnl": round(pos["shares"] * (cl - bc), 2),
            "days": [b["dt"][:10] for b in win],
            "open": [b["open"] for b in win], "close_": [b["close"] for b in win],
            "high": [b["high"] for b in win], "low": [b["low"] for b in win],
            "vol": [round(b.get("vol") or 0, 1) for b in win],
            "amount": [round(b.get("amount") or 0, 2) for b in win],
            "chg": chg,
            "ma5": ind["ma5"][k0:], "ma10": ind["ma10"][k0:], "ma20": ind["ma20"][k0:],
            "ops": in_win,
            "range_chg": round(cl / win[0]["close"] - 1, 4) if win else 0}


# ---------------- HTML表格(Python侧生成, 复用深蓝灰表头样式) ----------------
def _tbl(headers, rows):
    h = "<tr>" + "".join(f"<th>{_esc(x)}</th>" for x in headers) + "</tr>"
    for r in rows:
        h += "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
    return f"<table>{h}</table>"


def _cls(v):
    return "pos" if (v or 0) >= 0 else "neg"


def _pnl(v):
    return f'<span class="{_cls(v)}">{account.fmt_pnl(v)}</span>'


def _side_tag(side_txt):
    s = str(side_txt)
    if "止损" in s:
        k = "x"
    elif "买" in s or "低吸" in s:
        k = "b"
    else:
        k = "s"
    return f'<span class="tag {k}">{_esc(s)}</span>'

# =============================== 主入口 ===============================
def build(state, prices=None, rep_date=None, now=None, path=None):
    """生成 report/dashboard_ttrade.html(纯日内T; 与 dashboard_quant.html 独立); 返回文件路径"""
    now = now or datetime.datetime.now()
    rep_date = rep_date or state.get("today") or now.strftime("%Y-%m-%d")
    px = dict(state.get("last_prices") or {})
    px.update(prices or {})
    ops_by_code = _load_ops()

    asset = account.total_asset(state, px)
    base = state.get("baseline") or {}
    acct_pnl = (asset - base["total_asset"]) if base else 0.0
    # 昨日环比(2026-10-30用户需求): 最近一个有总资产的往日收盘
    prev_total = None
    for d in reversed(state.get("daily_history") or []):
        if d.get("total_asset") and d.get("date", "") < rep_date:
            prev_total = d["total_asset"]
            break
    day_chg = (asset - prev_total) if prev_total else None

    t_rows, t_tot, t_info = _today_t(state, px, ops_by_code)
    stats = _stats(state, px)
    daily = _load_daily()

    # T腿配对明细(2026-11-03用户需求; 2026-11-09改总分结构+时间正序): 个股合计 + 逐对明细
    leg_info, leg_rows = account.t_leg_pairs(state["today_trades"], px)
    leg_grp = {}
    for nm, tg, bp, sp, qy, pn, stt, tm in leg_rows:
        leg_grp.setdefault(nm, []).append((tm, tg, bp, sp, qy, pn, stt))
    leg_html = "<table><tr>" + "".join(
        f"<th>{_esc(x)}</th>" for x in
        ["时间", "类型", "买价", "卖价", "股数", "盈亏", "状态"]) + "</tr>"
    for nm, items in leg_grp.items():
        tot = sum(x[5] for x in items)
        n_open = sum(1 for x in items if not x[6].startswith("已闭合"))
        leg_html += (f'<tr><td colspan="7"><b>{_esc(nm)}</b>　配对{len(items)}对 · '
                     f'合计{_pnl(tot)}'
                     + (f'<span class="mut"> (未闭合{n_open}对)</span>'
                        if n_open else "") + "</td></tr>")
        for tm, tg, bp, sp, qy, pn, stt in sorted(items, key=lambda x: x[0]):
            leg_html += ('<tr><td style="padding-left:3em">' + _esc(tm) + "</td>"
                         + "".join(f"<td>{y}</td>" for y in
                                   [_esc(tg), bp, sp, str(qy),
                                    _pnl(pn), _esc(stt)]) + "</tr>")
    if not leg_grp:
        leg_html += '<tr><td colspan="7" class="mut">今日无T腿配对</td></tr>'
    leg_html += "</table>"

    # 今日做T总览(总分, 2026-11-03用户需求): 个股合计(总)+逐笔成交(分)
    grp_order = []
    for t in state["today_trades"]:
        if t["code"] not in grp_order:
            grp_order.append(t["code"])
    for c in state["today_ledger"]:
        led = state["today_ledger"][c]
        if (led["bq"] or led["sq"]) and c not in grp_order:
            grp_order.append(c)
    ths = "".join(f"<th>{_esc(x)}</th>" for x in
                  ["时间", "方向", "股数", "买价", "卖价", "持仓变化", "T类"])
    # 2026-11-09用户需求: 个股汇总行追加当日涨幅/T幅度/仓位变化
    pct_map = {}
    if grp_order:
        try:
            pct_map = {c: q.get("pct") for c, q in
                       quotes.fetch_quotes(grp_order).items()}
        except Exception:
            pass
    grp_html = f"<table><tr>{ths}</tr>"
    for c in grp_order:
        nm, rz, fl2, pn, ex = t_info[c]
        led = state["today_ledger"].get(c) or {}
        pct = pct_map.get(c)
        pct_s = (f'<span class="{_cls(pct)}">{pct:+.2f}%</span>'
                 if pct is not None else "—")
        # T幅 = 已回补T利润 / 当日回补买入金额(未回补浮动不计入)
        t_amp = (rz / led["ba"] * 100) if led.get("ba") else None
        t_amp_s = (f'<span class="{_cls(t_amp)}">{t_amp:+.2f}%</span>'
                   if t_amp is not None else "—")
        ts_c = [x for x in state["today_trades"] if x["code"] == c]
        if ts_c:
            sh0, sh1 = ts_c[0].get("shares_before"), ts_c[-1].get("shares_after")
            d_sh = (sh1 or 0) - (sh0 or 0)
            pos_s = f"{sh0}→{sh1}股"
            pos_s += (f'(<span class="{_cls(d_sh)}">{d_sh / sh0 * 100:+.1f}%</span>)'
                      if sh0 else "(新建仓)")
        else:
            pos_s = "—"
        grp_html += (f'<tr><td colspan="7"><b>{_esc(nm)}</b>　'
                     f'已回补{_pnl(rz)} · 未回补浮动{_pnl(fl2)} · 合计{_pnl(pn)}'
                     + (f'<span class="mut"> ({_esc(ex)})</span>' if ex else "")
                     + "</td></tr>")
        grp_html += (f'<tr><td colspan="7" style="padding-left:3em" class="mut">'
                     f'当日 {pct_s} · T幅 {t_amp_s} · 仓位 {pos_s}</td></tr>')
        for t in [x for x in state["today_trades"] if x["code"] == c]:
            inf = leg_info.get(id(t))
            if inf:
                bp, sp = "/".join(inf["bp"]) or "—", "/".join(inf["sp"]) or "—"
            else:   # 非T腿成交(止损/手动同步): 显示自身成交价
                bp = (account.fmt_price(t["price"])
                      if t["side"] == "buy" else "—")
                sp = (account.fmt_price(t["price"])
                      if t["side"] != "buy" else "—")
            cells = [t["time"][:5], _side_tag(t.get("side_txt", t["side"])),
                     str(t["qty"]), bp, sp,
                     f'{t.get("shares_before", "-")}→{t.get("shares_after", "-")}',
                     f'<span class="mut">{_esc(t.get("strat") or "")}</span>']
            # 具体操作行相对个股汇总行缩进(2026-11-09用户需求: 约3个汉字宽);
            # 调节缩进距离: 改下面的 3em(1em≈1个汉字, 18px≈1.3个汉字)
            grp_html += ('<tr><td style="padding-left:3em">' + cells[0] + "</td>"
                         + "".join(f"<td>{x}</td>" for x in cells[1:]) + "</tr>")
    if not grp_order:
        grp_html += '<tr><td colspan="7" class="mut">今日无做T交易</td></tr>'
    grp_html += "</table>"

    # 持仓概览
    held, missing, stocks = [], [], {}
    for code in sorted(state["positions"]):
        pos = state["positions"][code]
        if pos["shares"] <= 0:
            continue
        bars = None
        try:
            bars = quotes.fetch_kline_bars(code, klt=KLT_DAILY, lmt=BARS_LMT)
        except Exception as e:
            print(f"[dashboard_ttrade] {pos['name']}({code}) 日K获取失败: {e}")
        if bars and bars[-1]["dt"][:10] == now.strftime("%Y-%m-%d") and now.hour < 15:
            bars = bars[:-1]
        if not bars:
            missing.append(f"{pos['name']}({code})")
            cl = account.pos_price(state, code, px)
            fl = pos["shares"] * (cl - account.basis_cost(pos))
            held.append((code, pos, cl, fl, "静态价/无日K*"))
            continue
        stocks[code] = _stock_payload(code, pos, bars, ops_by_code.get(code, []), px)
        held.append((code, pos, stocks[code]["close"], stocks[code]["float_pnl"], ""))

    # 单股今日T盈亏(2026-10-30用户需求): 台账合计(未扣费) + T收益率(买入金额口径)
    t_today = {}
    for c, led in (state.get("today_ledger") or {}).items():
        if not led or (not led["bq"] and not led["sq"]):
            continue
        pr = (px.get(c) or (state.get("last_prices") or {}).get(c, 0)
              or account.pos_price(state, c, px))
        v = account.t_pnl(led, pr)
        t_base = led["ba"] if led["bq"] else led["sa"]
        t_today[c] = (v, (v / t_base * 100) if t_base else None)

    pos_rows = []
    for code, p, pr, fl, tag in sorted(held, key=lambda x: -x[1]["shares"] * x[2]):
        tv, tvp = t_today.get(code, (None, None))
        pos_rows.append([_esc(p["name"]) + f'<span class="mut">({code})</span>',
                         str(p["shares"]), account.fmt_price(pr),
                         account.fmt_amt(p["shares"] * pr),
                         account.fmt_price(account.basis_cost(p)),
                         _pnl(tv) if tv is not None else '<span class="mut">—</span>',
                         (f'<span class="{_cls(tv)}">{tvp:+.2f}%</span>'
                          if tvp is not None else '<span class="mut">—</span>'),
                         _pnl(fl),
                         f'<span class="mut">{_esc(tag)}</span>' if tag else ""])

    # 策略统计
    stat_tbl = [[_esc(s["label"]), str(s["trig"]), str(s["exec"]), str(s["wait"]),
                 _pnl(s["realized"]), _pnl(s["floating"]),
                 f'{s["win"]}/{s["pair"]}' if s["pair"] else "--"]
                for s in stats]

    pend = [c for c, d in (state.get("stop_pending") or {}).items() if d == rep_date]
    warn = ""
    if pend:
        nm = {t["code"]: t["name"] for t in state["today_trades"]}
        warn += ('<div class="warn">⏸ 止损跳空豁免挂起中(待 '
                 f'{config.ST_GAP_CONFIRM} 收盘确认): '
                 + "、".join(_esc(nm.get(c, c)) for c in pend) + "</div>")
    if missing:
        warn += ('<div class="warn">本次未取到日K: ' + "、".join(missing)
                 + " —— 未出K线图, 估值按最近一次收盘价(标*)</div>")

    # 昨日快照(2026-11-09用户需求: 卡片环比/绝对值变化)
    prev = None
    for d in reversed(daily):
        if d["date"] < rep_date and (d.get("cum") is not None
                                     or d.get("cash") is not None):
            prev = d
            break
    payload = {"tt": {
        "rep_date": rep_date, "init_date": state.get("init_date"),
        "gen_time": now.strftime("%Y-%m-%d %H:%M"),
        "baseline": base.get("total_asset"), "total": round(asset, 2),
        "chg": round(acct_pnl, 2) if base else None,
        "chg_pct": round(acct_pnl / base["total_asset"] * 100, 2) if base else None,
        "day_chg": round(day_chg, 2) if day_chg is not None else None,
        "day_chg_pct": round(day_chg / prev_total * 100, 2)
                       if day_chg is not None else None,
        "cash": round(state["cash"], 2), "cum_fee": round(state.get("cum_fee", 0), 2),
        "cum_net": round(sum(account.t_pnl(
            l, px.get(c) or (state.get("last_prices") or {}).get(c, 0))
            for c, l in state["cum_ledger"].items()) - state.get("cum_fee", 0), 2),
        "prev": {k: prev.get(k) if prev else None
                 for k in ("net", "cum", "cash", "fee")},
        "n_trade": len(state["today_trades"]), "n_hold": len(held),
        "n_skip": len(state.get("today_skips") or []),
        "t_tot": t_tot, "stats": stats, "daily": daily,
        "stocks": stocks, "missing": missing,
        "ai": dict(state.get("today_ai") or {}),
        "stop_mode": config.STOP_LOSS_MODE,
        "gap_exempt": bool(getattr(config, "ST_GAP_EXEMPT_EN", False)),
        "gap_confirm": str(getattr(config, "ST_GAP_CONFIRM", "14:50")),
        "gap_pct": float(getattr(config, "ST_GAP_PCT", 1.0)),
    }}
    out = _TPL.replace("__DATA__", json.dumps(payload, ensure_ascii=False)) \
        .replace("__WARN__", warn) \
        .replace("__TBL_T__", grp_html) \
        .replace("__TBL_LEGS__", leg_html) \
        .replace("__TBL_POS__", _tbl(
            ["股票", "持股", "现价", "市值", "基准成本", "今日T盈亏", "T收益率", "浮动盈亏", "备注"],
            pos_rows)) \
        .replace("__TBL_STAT__", _tbl(
            ["策略", "触发", "执行", "观望", "已回补盈亏", "浮动", "胜率"],
            stat_tbl))
    path = path or DASH_PATH
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    day = (rep_date or datetime.date.today().strftime("%Y-%m-%d")).replace("-", "")[-6:]
    try:                                # 日归档副本(用户规则: 看板名带YYMMDD日期, 如_261028)
        ap = os.path.join(os.path.dirname(DASH_PATH), "history")
        os.makedirs(ap, exist_ok=True)
        with open(os.path.join(ap, f"dashboard_ttrade_{day}.html"),
                  "w", encoding="utf-8") as f:
            f.write(out)
    except Exception:
        pass
    return path


_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="cache-control" content="no-store">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>日内T Dashboard</title>
<style>
:root{--bg:#0f1420;--card:#171f2e;--line:#2a3446;--fg:#e8edf5;--mut:#9aa7bd;
--red:#e60000;--green:#00a800;--blue:#3f7fd0;--amber:#f0a020;--purple:#b06bd6;--hdr:#3f4a5a;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:22px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:26px 0 10px;
border-left:3px solid var(--blue);padding-left:8px}
.sub{color:var(--mut);font-size:12px;margin-bottom:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card .k{color:var(--mut);font-size:12px}.card .v{font-size:22px;font-weight:700;margin-top:4px}
.pos{color:#ff5c5c}.neg{color:#4bd94b}.mut{color:var(--mut)}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}
th{background:var(--hdr);color:#fff;font-weight:600;text-align:left;padding:9px 10px;white-space:nowrap}
td{padding:8px 10px;border-top:1px solid var(--line)}
tr:hover td{background:#1c2637}
.tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11px;font-weight:600;margin:2px}
.tag.b{background:rgba(230,0,0,.16);color:#ff6b6b}
.tag.s{background:rgba(0,168,0,.16);color:#4bd94b}
.tag.x{background:rgba(240,160,32,.18);color:#f0b64a}
.chart{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px;margin-bottom:14px;position:relative}
.tip{position:absolute;display:none;z-index:9;background:rgba(18,24,36,.97);
border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12px;
line-height:1.65;color:var(--fg);pointer-events:none;max-width:270px;
box-shadow:0 8px 20px rgba(0,0,0,.5);white-space:nowrap}
.tip .tt{color:#fff;font-weight:700;margin-bottom:3px}
.tip .mut{color:var(--mut)}
.note{font-size:12px;color:var(--mut);line-height:1.8;margin:-8px 0 10px}
.chart .t{font-size:13px;color:var(--mut);margin-bottom:6px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}
canvas{width:100%;display:block}
.warn{background:rgba(240,160,32,.1);border:1px solid rgba(240,160,32,.4);
border-radius:8px;padding:10px 12px;color:#f0c89a;font-size:13px;margin:10px 0}
.foot{color:var(--mut);font-size:11px;margin-top:20px;line-height:1.8}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:820px){.cols{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<h1>日内T Dashboard · 做T模拟盘(独立)</h1>
<div class="sub" id="sub"></div>
<div id="warn">__WARN__</div>

<h2>账户概况</h2>
<div id="cards" class="cards"></div>

<h2>今日做T总览 (总分: 个股合计 + 逐笔交易)</h2>
<div id="tblt">__TBL_T__</div>
<div id="tsum" class="note"></div>
<div class="note">止损清仓/手动同步真实操作不走AI、不走投票, 纪律优先。</div>

<h2>T腿配对明细 (总分: 个股合计 + 逐对明细, 时间正序)</h2>
<div id="legs">__TBL_LEGS__</div>
<div class="note">隔日T按“昨买价→今卖价”独立配对; 其余按当日FIFO配对; 未平腿按最新价浮动估值(未平T仓/未回补)。</div>

<h2>资金 / 每日盈亏</h2>
<div class="cols">
 <div class="chart"><div class="t"><span>每日总资产 (收盘估值)</span><span id="elg"></span></div>
 <canvas id="eq" height="200"></canvas></div>
 <div class="chart"><div class="t"><span>每日净T盈亏 (含未回补浮动, 已扣费)</span><span id="nlg"></span></div>
 <canvas id="net" height="200"></canvas></div>
</div>

<h2>持仓概览</h2>
<div id="pos">__TBL_POS__</div>

<h2>持仓标的 日K与做T操作点</h2>
<div class="note">均线: <b style="color:#b06bd6">MA5</b> · <b style="color:#f0a020">MA10</b> · <b style="color:#3f7fd0">MA20</b>　定点: <span style="color:#ff5c5c">▲低吸买入</span> / <span style="color:#4bd94b">▼高抛卖出</span> / <span style="color:#f0a020">▼止损</span>　鼠标移到K线上可查看当日做T详情</div>
<div id="stocks"></div>

<h2>策略统计(启发式)</h2>
<div id="stats">__TBL_STAT__</div>
<div class="note">口径: 触发=过线信号中该票同向且|分|≥0.3的次数; 已回补盈亏按FIFO配对扣双边费用, 归因于卖腿主导票; 胜率=已回补对子中盈利占比。空表表示今日无量化信号。</div>

<h2>AI研判</h2>
<div id="ai" class="note"></div>

<div class="foot" id="foot"></div>
</div>
<script>
const D=__DATA__, T=D.tt;
const pct1=x=>x==null?'-':(x>=0?'+':'')+(x*100).toFixed(2)+'%';
const pctQ=x=>x==null?'-':(x>=0?'+':'')+x.toFixed(2)+'%';
const clr=x=>x>=0?'pos':'neg';
const num=x=>x==null?'-':x.toLocaleString(undefined,{maximumFractionDigits:2});

// ---------- 通用: 画布悬停提示(2倍缩放坐标换算 + tip层 + 防溢出定位) ----------
function attachHover(cv,redraw,resolve,build){
 if(!cv||cv.__hoverBound)return;
 cv.__hoverBound=true;
 let tip=cv.parentElement.querySelector('.tip');
 if(!tip){tip=document.createElement('div');tip.className='tip';cv.parentElement.appendChild(tip);}
 cv.addEventListener('mousemove',ev=>{
  const rect=cv.getBoundingClientRect();
  const cx=(ev.clientX-rect.left)*(cv.width/rect.width);
  const i=resolve(cx);
  const html=(i==null)?null:build(i);
  if(i==null||!html){tip.style.display='none';redraw(null);return;}
  redraw(i);tip.innerHTML=html;tip.style.display='block';
  const wrap=cv.parentElement.getBoundingClientRect();
  let lx=ev.clientX-wrap.left+14,ly=ev.clientY-wrap.top+14;
  if(lx+290>wrap.width)lx=Math.max(4,lx-300);
  if(ly+165>wrap.height)ly=Math.max(4,ev.clientY-wrap.top-155);
  tip.style.left=lx+'px';tip.style.top=ly+'px';});
 cv.addEventListener('mouseleave',()=>{tip.style.display='none';redraw(null);});
}
const idxOf=(cx,pad,W,n)=>{const i=Math.round((cx-pad)/(W-2*pad)*(n-1));
 return (i<0||i>=n)?null:i;};

document.getElementById('sub').textContent=
 `做T模拟盘 ${T.init_date}→${T.rep_date} | 生成 ${T.gen_time} | 期初基准 ${num(T.baseline)} 元`
 +` | 持仓 ${T.n_hold} 只 | 今日成交 ${T.n_trade} 笔`
 +` | 止损模式 ${T.stop_mode}${T.gap_exempt?'(跳空豁免: 开盘深穿改收盘确认)':''}`;

const P=T.prev||{};
const dv=(cur,pv,noPct)=>{if(pv==null||cur==null)return'';
 const d=cur-pv;const p=(pv!==0&&!noPct)?` (${d>=0?'+':''}${(d/Math.abs(pv)*100).toFixed(1)}%)`:'';
 return `<br><span style="font-size:11px;font-weight:400" class="${noPct?'mut':clr(d)}">较昨${d>=0?'+':''}${num(d)}${p}</span>`;};
let cards=[
 ['当前总资产',num(T.total),''],
 [`较昨日收盘`,T.day_chg==null?'-':`${T.day_chg>=0?'+':''}${num(T.day_chg)} (${pctQ(T.day_chg_pct)})`,clr(T.day_chg||0)],
 [`较期初基准`,T.chg==null?'-':`${T.chg>=0?'+':''}${num(T.chg)} (${pctQ(T.chg_pct)})`,clr(T.chg||0)],
 ['今日净T盈亏',`${T.t_tot.net>=0?'+':''}${num(T.t_tot.net)}`,clr(T.t_tot.net)],
 ['累计净T盈亏(含浮动)',`${T.cum_net>=0?'+':''}${num(T.cum_net)}${dv(T.cum_net,P.cum)}`,clr(T.cum_net)],
 ['可用现金',num(T.cash)+dv(T.cash,P.cash,true),''],
 ['累计费用',num(T.cum_fee)+dv(T.cum_fee,P.fee,true),''],
 ['今日成交',`${T.n_trade} 笔`,''],
 ['持仓只数',`${T.n_hold} 只`,''],
];
document.getElementById('cards').innerHTML=cards.map(([k,v,c])=>
 `<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join('');

document.getElementById('tsum').innerHTML=
 `<span>日内已回补T <b class="${clr(T.t_tot.realized)}">${num(T.t_tot.realized)}</b></span> · `
 +`<span>未回补浮动 <b class="${clr(T.t_tot.floating)}">${num(T.t_tot.floating)}</b></span> · `
 +`<span>费用 <b>${num(T.t_tot.fee)}</b></span> · `
 +`<span>今日净盈亏 <b class="${clr(T.t_tot.net)}">${T.t_tot.net>=0?'+':''}${num(T.t_tot.net)}</b></span>`
 +(T.n_skip?` · <span style="color:var(--amber)">⚠️ ${T.n_skip} 次信号因资金/持股不足跳过</span>`:'');

// ---------- 每日总资产曲线 ----------
const DAILY=T.daily||[];
window.__drawEq=function(hover){const cv=document.getElementById('eq');if(!cv)return;
 cv.width=cv.clientWidth*2;cv.height=200*2;const ctx=cv.getContext('2d');
 const W=cv.width,H=cv.height,pad=52*2;ctx.clearRect(0,0,W,H);
 ctx.strokeStyle='#2a3446';ctx.lineWidth=2;
 ctx.beginPath();ctx.moveTo(pad,pad);ctx.lineTo(pad,H-pad);ctx.lineTo(W-pad,H-pad);ctx.stroke();
 if(!DAILY.length){ctx.fillStyle='#9aa7bd';ctx.font='13px sans-serif';
  ctx.fillText('暂无逐日记录(收盘战报生成后累计)',pad+10,pad+24);window.__eq_st=null;return;}
 let mx=Math.max(...DAILY.map(p=>p.total)),mn=Math.min(...DAILY.map(p=>p.total));
 const sp=(mx-mn)||Math.abs(mx)*0.01||1;mx+=sp*.06;mn-=sp*.06;
 const X=i=>pad+(W-2*pad)*(DAILY.length===1?0.5:i/(DAILY.length-1)),Y=v=>H-pad-(H-2*pad)*(v-mn)/(mx-mn);
 ctx.font='12px sans-serif';
 for(let g=0;g<=4;g++){const y=pad+(H-2*pad)*g/4;ctx.fillStyle='#7f8ca3';
  ctx.fillText(num(mx-(mx-mn)*g/4),6,y+4);}
 const step=Math.max(1,Math.ceil(DAILY.length/6));
 for(let i=0;i<DAILY.length;i+=step){ctx.fillStyle='#7f8ca3';
  ctx.fillText(DAILY[i].date.slice(5),X(i)-18,H-pad+18);}
 if(T.baseline&&T.baseline>=mn&&T.baseline<=mx){const yb=Y(T.baseline);
  ctx.strokeStyle='#8899aa';ctx.setLineDash([6,6]);ctx.lineWidth=2;
  ctx.beginPath();ctx.moveTo(pad,yb);ctx.lineTo(W-pad,yb);ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle='#8899aa';ctx.fillText('基准 '+num(T.baseline),pad+6,yb-6);}
 ctx.strokeStyle='#3f7fd0';ctx.lineWidth=3;ctx.beginPath();
 DAILY.forEach((p,i)=>{i?ctx.lineTo(X(i),Y(p.total)):ctx.moveTo(X(i),Y(p.total))});ctx.stroke();
 ctx.fillStyle='#3f7fd0';
 DAILY.forEach((p,i)=>{ctx.beginPath();ctx.arc(X(i),Y(p.total),5,0,7);ctx.fill();});
 if(hover!=null&&hover>=0&&hover<DAILY.length){const hx=X(hover),hy=Y(DAILY[hover].total);
  ctx.strokeStyle='#8899aa';ctx.lineWidth=1;ctx.setLineDash([5,5]);
  ctx.beginPath();ctx.moveTo(hx,pad);ctx.lineTo(hx,H-pad);ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle='#e8edf5';ctx.beginPath();ctx.arc(hx,hy,7,0,7);ctx.fill();}
 const last=DAILY[DAILY.length-1];
 document.getElementById('elg').innerHTML=`<span class="mut">${DAILY.length} 个交易日 · 最新 ${num(last.total)}</span>`;
 window.__eq_st={pad:pad,W:W,n:DAILY.length};};
window.__drawEq(null);
if(DAILY.length)attachHover(document.getElementById('eq'),i=>window.__drawEq(i),
 cx=>window.__eq_st?idxOf(cx,window.__eq_st.pad,window.__eq_st.W,window.__eq_st.n):null,
 i=>{const p=DAILY[i],pv=i>0?DAILY[i-1].total:null,d=pv!=null?p.total-pv:null;
  let hh=`<div class="tt">${p.date}</div><div>总资产 <b>${num(p.total)}</b></div>`;
  if(d!=null)hh+=`<div>较前日 <b class="${clr(d)}">${d>=0?'+':''}${num(d)} (${pctQ(d/pv*100)})</b></div>`;
  if(p.net!=null)hh+=`<div class="mut">当日净T盈亏 ${p.net>=0?'+':''}${num(p.net)}</div>`;
  if(p.cum!=null)hh+=`<div class="mut">累计净T ${p.cum>=0?'+':''}${num(p.cum)}</div>`;
  if(T.baseline)hh+=`<div class="mut">较基准 <span class="${clr(p.total-T.baseline)}">${p.total-T.baseline>=0?'+':''}${num(p.total-T.baseline)}</span></div>`;
  return hh;});

// ---------- 每日净T盈亏柱状 ----------
window.__drawNet=function(hover){const cv=document.getElementById('net');if(!cv)return;
 cv.width=cv.clientWidth*2;cv.height=200*2;const ctx=cv.getContext('2d');
 const W=cv.width,H=cv.height,pad=52*2;ctx.clearRect(0,0,W,H);
 ctx.strokeStyle='#2a3446';ctx.lineWidth=2;
 ctx.beginPath();ctx.moveTo(pad,pad);ctx.lineTo(pad,H-pad);ctx.lineTo(W-pad,H-pad);ctx.stroke();
 if(!DAILY.length){ctx.fillStyle='#9aa7bd';ctx.font='13px sans-serif';
  ctx.fillText('暂无逐日记录',pad+10,pad+24);window.__net_st=null;return;}
 const vs=DAILY.map(p=>p.net||0);
 const amax=Math.max(...vs.map(Math.abs))||1;
 const Y0=(pad+H-pad)/2, sc=(H-2*pad)/2/amax;
 const X=i=>pad+(W-2*pad)*(i+0.5)/DAILY.length, bw=Math.max(4,(W-2*pad)/DAILY.length*0.62);
 ctx.strokeStyle='#2a3446';ctx.lineWidth=1;
 ctx.beginPath();ctx.moveTo(pad,Y0);ctx.lineTo(W-pad,Y0);ctx.stroke();
 ctx.font='12px sans-serif';ctx.fillStyle='#7f8ca3';
 ctx.fillText('+'+num(amax),6,pad+12);ctx.fillText('0',6,Y0+4);ctx.fillText('-'+num(amax),6,H-pad);
 const step=Math.max(1,Math.ceil(DAILY.length/6));
 for(let i=0;i<DAILY.length;i+=step){ctx.fillStyle='#7f8ca3';
  ctx.fillText(DAILY[i].date.slice(5),X(i)-18,H-pad+18);}
 DAILY.forEach((p,i)=>{const v=p.net||0,h=Math.abs(v)*sc;
  ctx.fillStyle=(hover===i)?'#e8edf5':(v>=0?'#ff5c5c':'#4bd94b');
  ctx.fillRect(X(i)-bw/2,Y0-Math.max(v,0)*sc,bw,Math.max(1,h));});
 window.__net_st={pad:pad,W:W,n:DAILY.length};};
window.__drawNet(null);
if(DAILY.length)attachHover(document.getElementById('net'),i=>window.__drawNet(i),
 cx=>window.__net_st?idxOf(cx,window.__net_st.pad,window.__net_st.W,window.__net_st.n):null,
 i=>{const p=DAILY[i];
  return `<div class="tt">${p.date}</div>
   <div>净T盈亏 <b class="${clr(p.net||0)}">${(p.net||0)>=0?'+':''}${num(p.net||0)}</b></div>
   <div class="mut">累计 ${p.cum>=0?'+':''}${num(p.cum)} · 总资产 ${num(p.total)}</div>`;});

// ---------- 持仓标的日K + 做T操作点 ----------
window.__st_state={};
(function(){const S=T.stocks||{};let h='';
 for(const c in S){const s=S[c];
  const nday=s.ops.length;
  h+=`<div class="chart"><div class="t">
   <span><b>${s.name} (${c})</b>　${s.days[0].slice(5)}~${s.days[s.days.length-1].slice(5)} 区间 ${pct1(s.range_chg)}</span>
   <span>持股 <b>${s.sh}</b> · 成本 ${s.cost?s.cost.toFixed(3):'-'} · 现价 ${s.close.toFixed(3)} ·
   浮盈 <b class="${s.float_pnl>=0?'pos':'neg'}">${s.float_pnl>=0?'+':''}${num(s.float_pnl)}</b>
   (${pctQ(s.pnl_pct)}) · 区间内做T点 <b>${nday}</b></span></div>
  <canvas id="sc_${c}" height="190"></canvas>
  <div style="font-size:12px;color:var(--mut);margin-top:6px">`;
  for(const o of s.ops)
   h+=`<span class="tag ${o.kind==='BUY'?'b':(o.kind==='STOP'?'x':'s')}" title="${o.reason||''}">${o.date.slice(5)} ${o.time.slice(0,5)} ${o.side_txt.replace(/[^一-龥]/g,'')} ${o.qty}@${o.price.toFixed(3)}</span>`;
  if(!s.ops.length)h+='<span class="mut">记录区间内无做T操作</span>';
  h+=`</div><div class="tip" id="stp_${c}"></div></div>`;}
 document.getElementById('stocks').innerHTML=h||'<div class="mut">当前无持仓标的</div>';

 window.__drawS=function(c,hover){const cv=document.getElementById('sc_'+c);if(!cv)return;
  cv.width=cv.clientWidth*2;cv.height=190*2;const ctx=cv.getContext('2d');
  const W=cv.width,H=cv.height,s=S[c],pad=40*2;ctx.clearRect(0,0,W,H);
  let mn=Math.min(...s.low),mx=Math.max(...s.high);const sp=(mx-mn)||1;mn-=sp*.05;mx+=sp*.05;
  const X=i=>pad+(W-2*pad)*i/Math.max(1,s.days.length-1),Y=v=>H-pad-(H-2*pad)*(v-mn)/(mx-mn);
  ctx.strokeStyle='#232d3e';ctx.lineWidth=1;
  for(let g=0;g<=4;g++){const y=pad+(H-2*pad)*g/4;ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(W-pad,y);ctx.stroke();}
  const bw=Math.max(1.5,(W-2*pad)/s.days.length*0.62);
  for(let i=0;i<s.days.length;i++){const o=s.open[i],cln=s.close_[i],hi=s.high[i],lo=s.low[i];
   const col=cln>=o?'#e60000':'#00a800';ctx.strokeStyle=col;ctx.fillStyle=col;
   ctx.beginPath();ctx.moveTo(X(i),Y(hi));ctx.lineTo(X(i),Y(lo));ctx.stroke();
   const y1=Y(Math.max(o,cln)),y2=Y(Math.min(o,cln));ctx.fillRect(X(i)-bw/2,y1,bw,Math.max(1,y2-y1));}
  [['ma5','#b06bd6'],['ma10','#f0a020'],['ma20','#3f7fd0']].forEach(([k,col])=>{
   ctx.strokeStyle=col;ctx.lineWidth=1.6;ctx.beginPath();let st=false;
   s[k].forEach((v,i)=>{if(v==null)return;st?ctx.lineTo(X(i),Y(v)):(ctx.moveTo(X(i),Y(v)),st=true)});ctx.stroke();});
  for(const op of s.ops){const i=s.days.indexOf(op.date);if(i<0)continue;
   const x=X(i),y=Y(op.price);ctx.beginPath();
   if(op.kind==='BUY'){ctx.fillStyle='#ff5c5c';ctx.moveTo(x,y+12);ctx.lineTo(x-8,y+26);ctx.lineTo(x+8,y+26);}
   else{ctx.fillStyle=op.kind==='STOP'?'#f0a020':'#4bd94b';
    ctx.moveTo(x,y-12);ctx.lineTo(x-8,y-26);ctx.lineTo(x+8,y-26);}
   ctx.closePath();ctx.fill();}
  if(hover!=null&&hover>=0&&hover<s.days.length){const hx=X(hover),hy=Y(s.close_[hover]);
   ctx.strokeStyle='#8899aa';ctx.lineWidth=1;ctx.setLineDash([5,5]);
   ctx.beginPath();ctx.moveTo(hx,pad);ctx.lineTo(hx,H-pad);ctx.stroke();
   ctx.beginPath();ctx.moveTo(pad,hy);ctx.lineTo(W-pad,hy);ctx.stroke();
   ctx.setLineDash([]);ctx.fillStyle='#e8edf5';
   ctx.beginPath();ctx.arc(hx,hy,5,0,7);ctx.fill();}
  window.__st_state[c]={pad:pad,W:W,n:s.days.length};};

 window.__st_tip=function(c,i){const s=S[c];
  const o=s.open[i],cl=s.close_[i],hi=s.high[i],lo=s.low[i];
  const ch=(s.chg&&s.chg[i]!=null)?s.chg[i]*100:null;
  const pc=(ch!=null&&ch>-100)?cl/(1+ch/100):o;
  const cls=(ch!=null&&ch<0)?'neg':'pos';
  let hh=`<div class="tt">${s.days[i]}</div>`
   +`<div>开 ${o.toFixed(3)} · 收 <b class="${cls}">${cl.toFixed(3)}</b> · 涨跌 <b class="${cls}">${ch==null?'-':(ch>=0?'+':'')+ch.toFixed(2)+'%'}</b></div>`
   +`<div>最高 ${hi.toFixed(3)} · 最低 ${lo.toFixed(3)} · 振幅 ${(((hi-lo)/pc)*100).toFixed(2)}%</div>`;
  const dayops=(s.ops||[]).filter(p=>p.date===s.days[i]);
  if(dayops.length)hh+='<div style="margin-top:4px;border-top:1px solid #2a3446;padding-top:4px">'
   +dayops.map(p=>`<b class="${p.kind==='BUY'?'pos':'neg'}">${p.time.slice(0,5)} ${p.side_txt}</b> ${p.qty}股@${p.price.toFixed(3)} <span class="mut">${p.reason||''}</span>`).join('<br>')+'</div>';
  else hh+='<div class="mut" style="margin-top:4px">当日无做T操作</div>';
  return hh;};

 for(const c in S){window.__drawS(c,null);
  const cv=document.getElementById('sc_'+c);if(!cv)continue;
  attachHover(cv,i=>window.__drawS(c,i),
   cx=>window.__st_state[c]?idxOf(cx,window.__st_state[c].pad,window.__st_state[c].W,window.__st_state[c].n):null,
   i=>window.__st_tip(c,i));}
})();

// ---------- AI研判 ----------
(function(){const a=T.ai||{};const el=document.getElementById('ai');
 if(!a.asked){el.innerHTML='<span class="mut">今日无AI研判请求</span>';return;}
 el.innerHTML=`🤖 AI研判: <b>${a.asked}</b> 次 (建议执行 <b class="pos">${a.execute||0}</b> / `
  +`建议观望 <b class="neg">${a.wait||0}</b> / 失败 <b>${a.fail||0}</b>)`;})();

window.addEventListener('resize',()=>{
 for(const c in (T.stocks||{}))window.__drawS&&window.__drawS(c);
 if(window.__drawEq)window.__drawEq(null);
 if(window.__drawNet)window.__drawNet(null);});

document.getElementById('foot').innerHTML=
 `本页为<b>日内T模拟盘</b>独立看板(做T区间+量比+分钟级投票引擎, 分钟级成交),
 与量化独立账户看板 <b>report/dashboard_quant.html</b> 相互独立, 互不覆盖。<br>
 做T审核: 止损(模式 ${T.stop_mode})不走AI、不走投票;
 ${T.gap_exempt?`已启用<b>止损跳空豁免</b>: 开盘深穿止损线≥${T.gap_pct}%不立即清仓, 改在 ${T.gap_confirm} 收盘确认; 期间收复止损线则自动解除。`:'当前为原行为: 开盘破止损线即清仓。'}<br>
 费用口径: common/fees.py(佣金万2.5最低5 + 印花税卖出万5 + 过户费十万分之一)。数据: 东财日K klt=101 / 腾讯回退源。`;
</script></body></html>"""
