# -*- coding: utf-8 -*-
"""当前量化账户标的池 · v2.2/v2.3 × 动态止盈止损 回测 + 报告
================================================================
口径:
  标的 = data/quant/quant_state.json 全部子账户, 排除用户指定抛弃的 ZZ1000/神奇制药
  窗口 = 近180个自然日(与日K缓存池 KLINE_CACHE_DAYS 同口径)
  资金 = 每只独立子账户, 期初钉住 BT_CAPITAL(与半年回测同基准)
  策略 = backtest/daily.py run_ps_stock, 无未来函数
对比矩阵(v2.2/v2.3 × 动态止盈参数网格):
  v2.2 = SCALE_IN_EN=False(趋势腿一笔满仓)   v2.3 = True(首笔50%+MA5加仓)
  dyn  = 动态止盈止损实验层: trail移动止盈 / be_trig浮盈保本 / tp1止盈减仓
         (仅回测实验, 生产引擎不传参=行为不变)
调参: 网格全量扫描 -> v2.3等权收益最高者为主推, 报告附完整敏感性表
输出:
  report/quant_backtest_report.html(自包含, 含权益曲线/K线MA5·MA10/买卖点悬停提示)
用法:
  python run_backtest.py --quant
  python run_backtest.py --quant --no-dashboard
"""
import datetime
import html
import json
import os
import sys

import config_common as config
import config_quant
from backtest.daily import BT_CAPITAL, run_ps_stock
from common import quotes

IDX = config.REGIME_INDEX_ALIAS
OUT = os.path.join(config.REPORT_DIR, "quant_backtest_report.html")
STATE = os.path.join(config.QT_DATA_DIR, "quant_state.json")
EXCLUDE = set(getattr(config, "BT_EXCLUDE_CODES", ()))   # 抛弃标的名单(个性化, config_local.py配置)

# 动态止盈止损参数网格(自主调参 第七轮·止盈阶梯+止损阶梯共存专项):
# 用户明确: 阶梯止盈与阶梯止损必须同时在场。第六轮紧止损组合(-6/+3/+10等)负增量-1.9~-2.7pp,
# 根因=浮盈<15%档止盈带宽6%本身已等效-6%止损, 更紧的止损线只会提前割趋势。
# 本轮只测"宽松共存": 灾难型固定止损(-8~-15%, 极少触发只兜尾部) + 宽松阶梯止损,
# 目标=找到与现行v2.4收益差距最小(代价最低)的共存组合供用户决策
_TIERS = [(0.30, 0.15), (0.15, 0.10), (0.0, 0.06)]
_DYN_REASONS = ("移动止盈", "保本止盈", "止盈减仓", "超买减仓", "超买清仓",
                "MACD死叉", "阶梯止损", "止损")
GRID = [
    ("基准(仅MA10)", None),
    ("阶梯止盈6/10/15%(现行v2.4)", {"trail_tiers": _TIERS}),
    # ---- 共存: +灾难型固定止损(越深越像保险, 触发越少) ----
    ("v2.4+固定止损-8%", {"trail_tiers": _TIERS, "stop_pct": 0.08}),
    ("v2.4+固定止损-10%", {"trail_tiers": _TIERS, "stop_pct": 0.10}),
    ("v2.4+固定止损-12%", {"trail_tiers": _TIERS, "stop_pct": 0.12}),
    ("v2.4+固定止损-15%", {"trail_tiers": _TIERS, "stop_pct": 0.15}),
    # ---- 共存: +宽松阶梯止损(线随最高浮盈上移, 只上不下) ----
    ("v2.4+阶梯止损-8/0/+8", {"trail_tiers": _TIERS,
                              "stop_tiers": [(0.30, 1.08), (0.15, 1.00), (0.0, 0.92)]}),
    ("v2.4+阶梯止损-10/0/+5", {"trail_tiers": _TIERS,
                               "stop_tiers": [(0.30, 1.05), (0.15, 1.00), (0.0, 0.90)]}),
    ("v2.4+阶梯止损-12/0/+10", {"trail_tiers": _TIERS,
                                "stop_tiers": [(0.30, 1.10), (0.15, 1.00), (0.0, 0.88)]}),
    ("v2.4+阶梯止损-10/+2/+8", {"trail_tiers": _TIERS,
                                "stop_tiers": [(0.30, 1.08), (0.15, 1.02), (0.0, 0.90)]}),
    ("v2.4+阶梯止损-12/+3/+10档25/50", {"trail_tiers": _TIERS,
                                        "stop_tiers": [(0.50, 1.10), (0.25, 1.03), (0.0, 0.88)]}),
]


def load_pool():
    """量化账户标的(排除ZZ1000/神奇制药, 持仓中优先), 返回 [{code,name,sh,status}]"""
    st = json.load(open(STATE, encoding="utf-8"))
    pool = []
    for code, a in st["accounts"].items():
        if code in EXCLUDE:
            continue
        sh = int(a.get("sh", 0))
        pool.append({"code": code, "name": a.get("name", code), "sh": sh,
                     "status": "持仓中" if sh > 0 else "已清仓",
                     "cost": a.get("cost", 0.0), "cash": a.get("cash", 0.0)})
    pool.sort(key=lambda p: (-p["sh"], p["code"]))
    return pool


def _drop_forming(bars):
    """收盘前丢弃当日形成中的K线(与量化引擎 _drop_forming_bar 同口径)"""
    now = datetime.datetime.now()
    if now.hour * 60 + now.minute < 15 * 60:
        today = now.strftime("%Y-%m-%d")
        return [b for b in bars if b["dt"][:10] < today]
    return bars


def build_uni(pool):
    """日K缓存池取数(180天) + 各序列按公共日期区间对齐(保证冰点维度可用)"""
    uni = {}
    ib = _drop_forming(quotes.fetch_kline_bars(IDX, klt=101, lmt=120))
    if ib:
        uni[IDX] = {"name": "上证指数", "bars": ib}
    for p in pool:
        try:
            b = _drop_forming(quotes.fetch_kline_bars(p["code"], klt=101, lmt=120))
        except Exception as e:
            print(f"[量化回测] {p['name']}({p['code']}) 日K获取失败: {e}")
            continue
        if b:
            uni[p["code"]] = {"name": p["name"], "bars": b}
    if len(uni) <= 1:
        return uni, None
    start = max(v["bars"][0]["dt"][:10] for v in uni.values())
    end = min(v["bars"][-1]["dt"][:10] for v in uni.values())
    for v in uni.values():
        v["bars"] = [b for b in v["bars"] if start <= b["dt"][:10] <= end]
    return uni, (start, end)


def _run_v24(uni, code, dyn):
    """v2.4(满仓, SCALE_IN_EN=False)跑一组dyn参数"""
    config_quant.SCALE_IN_EN = False
    return run_ps_stock(uni, code, BT_CAPITAL, dyn=dyn)


def run_grid(pool, uni):
    """全网格扫描: 返回 {label: rows}, row={**pool项, bh, r(v2.4结果), hits(动态触发次数)}"""
    results = {}
    for label, dyn in GRID:
        rows = []
        for p in pool:
            if p["code"] not in uni:
                continue
            r = _run_v24(uni, p["code"], dyn)
            bars = uni[p["code"]]["bars"]
            bh = bars[-1]["close"] / bars[0]["close"] - 1
            hits = sum(1 for o in r["ops"]
                       if o.get("reason", "").startswith(_DYN_REASONS)
                       and o.get("reason") != "止损")   # 排除scalp腿自带"止损"(信号低点)
            rows.append({**p, "bh": bh, "r": r, "hits": hits})
        results[label] = rows
    config_quant.SCALE_IN_EN = False
    return results


def pick_best(results):
    """主推组合: 优先在"动态规则真实触发过"的组合里选 v2.4 等权收益最高(并列取回撤小);
    若全部零触发则退回收益最高者(此时应如实报告无区别)"""
    cands = []
    for label, dyn in GRID:
        if dyn is None:
            continue
        rows = results[label]
        if not rows:
            continue
        ret = sum(r["r"]["return"] for r in rows) / len(rows)
        mdd = sum(r["r"]["mdd"] for r in rows) / len(rows)
        hits = sum(r["hits"] for r in rows)
        cands.append((label, dyn, rows, ret, mdd, hits))
    if not cands:
        return (None, None, None)
    fired = [c for c in cands if c[5] > 0]
    pool_ = fired or cands
    best = max(pool_, key=lambda c: (round(c[3], 6), -round(c[4], 6)))
    return (best[0], best[1], best[2])


# ============================== 报告 ==============================
def _pct(v, digits=2):
    return f"{v * 100:+.{digits}f}%"


def _clr(v):
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def _sma(vals, n):
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def _equity_svg(days, eq, ops, cap):
    """权益曲线(相对期初%) + 成交点; 每点带悬停提示"""
    w, h, pl, pr, pt, pb = 900, 190, 52, 12, 12, 24
    vals = [v / cap - 1 for v in eq]
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    rng = (hi - lo) or 0.01
    X = lambda i: pl + (w - pl - pr) * i / max(len(vals) - 1, 1)
    Y = lambda v: pt + (h - pt - pb) * (1 - (v - lo) / rng)
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" style="display:block">']
    for g in range(5):
        y = pt + (h - pt - pb) * g / 4
        out.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{w - pr}" y2="{y:.1f}" stroke="#2a3446"/>')
    out.append(f'<line x1="{pl}" y1="{Y(0):.1f}" x2="{w - pr}" y2="{Y(0):.1f}" stroke="#5a6478" stroke-dasharray="4 3"/>')
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
    out.append(f'<polyline points="{pts}" fill="none" stroke="#3f7fd0" stroke-width="1.6"/>')
    for i, v in enumerate(vals):
        if i % 2 and i != len(vals) - 1:
            continue
        out.append(f'<g><title>{days[i]}  权益 {eq[i]:,.2f} 元 ({v * 100:+.2f}%)</title>'
                   f'<rect x="{X(i) - 3:.1f}" y="{pt}" width="6" height="{h - pt - pb}" fill="transparent"/></g>')
    dmap = {d: i for i, d in enumerate(days)}
    for op in ops:
        i = dmap.get(op["date"][:10])
        if i is None:
            continue
        v = vals[i]
        col = "#ff5c5c" if op["side"] == "BUY" else "#4bd94b"
        out.append(f'<g><title>{op["date"]} {op["side"]} {op["qty"]}股 @{op["price"]:.3f} {op.get("reason", "")}</title>'
                   f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="3.4" fill="{col}"/></g>')
    out.append(f'<text x="{pl - 6}" y="{Y(hi) + 4:.1f}" fill="#9aa7bd" font-size="10" text-anchor="end">{hi * 100:+.0f}%</text>')
    out.append(f'<text x="{pl - 6}" y="{Y(lo) + 4:.1f}" fill="#9aa7bd" font-size="10" text-anchor="end">{lo * 100:+.0f}%</text>')
    out.append("</svg>")
    return "".join(out)


def _kline_svg(bars, ops):
    """个股K线 + MA5(黄) + MA10(蓝) + 买卖点; 每根K线与每笔操作均可悬停"""
    w, h, pl, pr, pt, pb = 900, 230, 52, 12, 12, 24
    n = len(bars)
    closes = [b["close"] for b in bars]
    ma5, ma10 = _sma(closes, 5), _sma(closes, 10)
    lo = min(b["low"] for b in bars)
    hi = max(b["high"] for b in bars)
    rng = (hi - lo) or 0.01
    X = lambda i: pl + (w - pl - pr) * i / max(n - 1, 1)
    Y = lambda v: pt + (h - pt - pb) * (1 - (v - lo) / rng)
    bw = max(1.4, (w - pl - pr) / n * 0.62)
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" style="display:block">']
    for g in range(5):
        y = pt + (h - pt - pb) * g / 4
        out.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{w - pr}" y2="{y:.1f}" stroke="#2a3446"/>')
    for i, b in enumerate(bars):
        col = "#e60000" if b["close"] >= b["open"] else "#00a800"
        out.append(f'<g><title>{b["dt"]}  开{b["open"]:.3f} 高{b["high"]:.3f} '
                   f'低{b["low"]:.3f} 收{b["close"]:.3f} 量{b["vol"]:.0f}手</title>'
                   f'<line x1="{X(i):.1f}" y1="{Y(b["high"]):.1f}" x2="{X(i):.1f}" '
                   f'y2="{Y(b["low"]):.1f}" stroke="{col}"/>'
                   f'<rect x="{X(i) - bw / 2:.1f}" y="{Y(max(b["open"], b["close"])):.1f}" '
                   f'width="{bw:.1f}" height="{max(1.0, abs(Y(b["open"]) - Y(b["close"]))):.1f}" '
                   f'fill="{col}"/></g>')
    for vals, col in ((ma5, "#ffd24a"), (ma10, "#3f7fd0")):
        seg = [(X(i), Y(v)) for i, v in enumerate(vals) if v is not None]
        if len(seg) > 1:
            out.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in seg)}" '
                       f'fill="none" stroke="{col}" stroke-width="1.4" opacity=".9"/>')
    for op in ops:
        i = next((k for k, b in enumerate(bars) if b["dt"][:10] == op["date"][:10]), None)
        if i is None:
            continue
        x, y = X(i), Y(op["price"])
        if op["side"] == "BUY":
            out.append(f'<g><title>{op["date"]} 买入 {op["qty"]}股 @{op["price"]:.3f} '
                       f'{op.get("reason", "")}</title>'
                       f'<path d="M{x:.1f},{y + 8:.1f} l-7,13 l14,0 z" fill="#ff5c5c"/></g>')
        else:
            out.append(f'<g><title>{op["date"]} 卖出 {op["qty"]}股 @{op["price"]:.3f} '
                       f'{op.get("reason", "")}</title>'
                       f'<path d="M{x:.1f},{y - 8:.1f} l-7,-13 l14,0 z" fill="#4bd94b"/></g>')
    out.append("</svg>")
    return "".join(out)


def _dyn_desc(dyn):
    """动态参数人类可读描述"""
    if not dyn:
        return "无"
    parts = []
    if dyn.get("trail"):
        parts.append(f"移动止盈{dyn['trail'] * 100:.0f}%")
    if dyn.get("trail_tiers"):
        tt = "/".join(f"{t[1] * 100:.0f}%" for t in reversed(dyn["trail_tiers"]))
        parts.append(f"阶梯止盈{tt}")
    if dyn.get("stop_pct"):
        parts.append(f"固定止损-{dyn['stop_pct'] * 100:.0f}%")
    if dyn.get("stop_tiers"):
        st = "/".join(f"{(t[1] - 1) * 100:+.0f}%" for t in reversed(dyn["stop_tiers"]))
        parts.append(f"阶梯止损{st}")
    if dyn.get("ma_exit") is False:
        parts.append("替代MA10退出")
    if dyn.get("be_trig"):
        parts.append(f"浮盈{dyn['be_trig'] * 100:.0f}%后保本线{dyn.get('be_stop', 1.0) * 100:.0f}%")
    if dyn.get("tp1_pct"):
        parts.append(f"浮盈{dyn['tp1_pct'] * 100:.0f}%减仓{dyn.get('tp1_frac', 0.5) * 100:.0f}%")
    return " + ".join(parts)


def build_report(results, uni, win, best):
    """best = (label, dyn, rows最优组合); 基准锚=现行v2.4(阶梯止盈6/10/15%)"""
    blabel, bdyn, brows = best
    cur_label = "阶梯止盈6/10/15%(现行v2.4)"
    base_rows = results["基准(仅MA10)"]       # 仅MA10退出(无动态)
    cur_rows = results[cur_label]             # 现行v2.4
    days = [b["dt"][:10] for b in uni[IDX]["bars"]]

    def avg(rows, key):
        return sum(r["r"][key] for r in rows) / len(rows) if rows else 0

    a_base, a_cur, a_best = avg(base_rows, "return"), avg(cur_rows, "return"), avg(brows, "return")
    avg_bh = sum(r["bh"] for r in brows) / len(brows) if brows else 0
    hold = [r for r in brows if r["sh"] > 0]
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    cards = [
        ("标的数量", f"{len(brows)} 只", ""),
        ("基准(仅MA10)", _pct(a_base), _clr(a_base)),
        ("现行v2.4(6/10/15%)", _pct(a_cur), _clr(a_cur)),
        (f"主推: {blabel[:14]}", _pct(a_best), _clr(a_best)),
        ("Δ vs 现行v2.4", f"{(a_best - a_cur) * 100:+.2f}pp", _clr(a_best - a_cur)),
        ("等权买入持有", _pct(avg_bh), _clr(avg_bh)),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{html.escape(k)}</div>'
        f'<div class="v {c}">{html.escape(v)}</div></div>' for k, v, c in cards)

    # ---- 参数敏感性表 ----
    sens = ['<table><tr><th>止盈止损组合(v2.4满仓)</th><th>等权收益</th>'
            '<th>动态触发</th><th>均回撤</th><th>Δ vs 现行v2.4</th></tr>']
    for label, dyn in GRID:
        rows = results[label]
        if not rows:
            continue
        a = avg(rows, "return")
        mdd = avg(rows, "mdd")
        hits = sum(r["hits"] for r in rows)
        star = ' <b style="color:#f0a020">★主推</b>' if label == blabel else ""
        cur_tag = ' <span style="color:#7aa2d8">(现行)</span>' if label == cur_label else ""
        hit_txt = f"{hits}次" if hits else '<span style="color:#667">0(未生效)</span>'
        sens.append(f'<tr><td>{html.escape(label)}{star}{cur_tag}</td>'
                    f'<td class="{_clr(a)}"><b>{_pct(a)}</b></td>'
                    f'<td>{hit_txt}</td>'
                    f'<td>{mdd * 100:.1f}%</td>'
                    f'<td class="{_clr(a - a_cur)}">{(a - a_cur) * 100:+.2f}pp</td></tr>')
    sens.append("</table>")

    # ---- 逐标的表(基准MA10 vs 现行v2.4 vs 主推) ----
    cmap = {r["code"]: r for r in cur_rows}
    bmap = {r["code"]: r for r in brows}
    th = ("<tr><th>标的</th><th>状态</th><th>区间涨跌</th><th>基准(仅MA10)</th>"
          "<th>现行v2.4</th><th>主推组合</th><th>Δ(vs现行)</th><th>回撤</th><th>成交</th></tr>")
    trs = []
    for r in base_rows:
        c, b = cmap[r["code"]], bmap[r["code"]]
        trs.append(
            f'<tr><td>{html.escape(r["name"])}({r["code"]})</td><td>{r["status"]}</td>'
            f'<td class="{_clr(r["bh"])}">{_pct(r["bh"])}</td>'
            f'<td class="{_clr(r["r"]["return"])}">{_pct(r["r"]["return"])}</td>'
            f'<td class="{_clr(c["r"]["return"])}">{_pct(c["r"]["return"])}</td>'
            f'<td class="{_clr(b["r"]["return"])}"><b>{_pct(b["r"]["return"])}</b></td>'
            f'<td class="{_clr(b["r"]["return"] - c["r"]["return"])}">'
            f'{(b["r"]["return"] - c["r"]["return"]) * 100:+.2f}pp</td>'
            f'<td>{b["r"]["mdd"] * 100:.1f}%</td>'
            f'<td>{b["r"]["trades"]}笔</td></tr>')
    table = f'<table>{th}{"".join(trs)}</table>'

    # ---- 图表卡片(主推组合 v2.4) ----
    charts = []
    for r in brows:
        code = r["code"]
        bars = uni[code]["bars"]
        chart = _kline_svg(bars, r["r"]["ops"])
        eq = _equity_svg([b["dt"][:10] for b in bars[1:]],
                         r["r"]["equity"], r["r"]["ops"], BT_CAPITAL)
        tags = "".join(
            f'<span class="tag {"b" if o["side"] == "BUY" else "s"}" title="{html.escape(o.get("reason", ""))}">'
            f'{o["date"][5:10]} {"买" if o["side"] == "BUY" else "卖"}{o["qty"]}@{o["price"]:.2f}'
            f'·{html.escape(o.get("reason", ""))}</span>'
            for o in r["r"]["ops"][-16:]) or '<span style="color:#667">无操作</span>'
        charts.append(
            f'<div class="chart"><div class="t"><span>{html.escape(r["name"])} ({code}) '
            f'· {r["status"]}</span><span>主推组合 <b class="{_clr(r["r"]["return"])}">'
            f'{_pct(r["r"]["return"])}</b> · 回撤 {r["r"]["mdd"] * 100:.1f}% · '
            f'{r["r"]["trades"]}笔</span></div>'
            f'<div class="legend">K线：红涨绿跌　均线：<span class="y">━ MA5</span>　'
            f'<span class="b">━ MA10</span>　三角：▲买入 ▼卖出</div>'
            f'{chart}{eq}<div style="font-size:12px;color:var(--mut);margin-top:4px">'
            f'上：个股K线与操作点（悬停查看当日行情）　下：权益曲线（悬停查看逐日权益）</div>'
            f'<div style="margin-top:6px">{tags}</div></div>')

    # ---- 成交明细(主推 v2.4) ----
    ops_rows = []
    for r in brows:
        for o in r["r"]["ops"]:
            ops_rows.append(
                f'<tr><td>{o["date"]}</td><td>{html.escape(r["name"])}({r["code"]})</td>'
                f'<td class="{"pos" if o["side"] == "BUY" else "neg"}">'
                f'{"买入" if o["side"] == "BUY" else "卖出"}</td>'
                f'<td>{o["qty"]}</td><td>{o["price"]:.3f}</td>'
                f'<td>{html.escape(o.get("reason", ""))}</td></tr>')
    ops_table = ('<table><tr><th>日期</th><th>标的</th><th>方向</th><th>数量</th>'
                 '<th>价格</th><th>原因</th></tr>' + "".join(ops_rows) + "</table>") \
        if ops_rows else '<div class="warn">窗口内无成交</div>'

    verdict = (f'等权 基准(仅MA10) <b>{_pct(a_base)}</b> → 现行v2.4(阶梯止盈6/10/15%) '
               f'<b>{_pct(a_cur)}</b> → 主推 {html.escape(blabel)} '
               f'<b class="{_clr(a_best)}">{_pct(a_best)}</b>'
               f'（较现行 <b class="{_clr(a_best - a_cur)}">{(a_best - a_cur) * 100:+.2f}pp</b>）；'
               f'相对等权买入持有 {_pct(avg_bh)} 的超额为 '
               f'<b class="{_clr(a_best - avg_bh)}">{(a_best - avg_bh) * 100:+.2f}pp</b>。')
    if a_best - a_cur > 0:
        verdict += f' 主推参数：{_dyn_desc(bdyn)}，待您确认后晋升生产。'
    else:
        verdict += ' 本轮无组合跑赢现行v2.4，建议维持 6/10/15% 不动。'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>量化账户 · v2.4 阶梯止盈止损调参报告</title>
<style>
:root{{--bg:#0f1420;--card:#171f2e;--line:#2a3446;--fg:#e8edf5;--mut:#9aa7bd;
--red:#e60000;--green:#00a800;--blue:#3f7fd0;--amber:#f0a020;--hdr:#3f4a5a;}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1200px;margin:0 auto;padding:22px}}
h1{{font-size:20px;margin:0 0 4px}}h2{{font-size:16px;margin:26px 0 10px;
border-left:3px solid var(--blue);padding-left:8px}}
.sub{{color:var(--mut);font-size:12px;margin-bottom:16px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.card .k{{color:var(--mut);font-size:12px}}.card .v{{font-size:21px;font-weight:700;margin-top:4px}}
.pos{{color:#ff5c5c}}.neg{{color:#4bd94b}}
table{{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}}
th{{background:var(--hdr);color:#fff;font-weight:600;text-align:left;padding:9px 10px}}
td{{padding:8px 10px;border-top:1px solid var(--line)}}
tr:hover td{{background:#1c2637}}
.tag{{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11px;font-weight:600;margin:2px}}
.tag.b{{background:rgba(230,0,0,.16);color:#ff6b6b}}
.tag.s{{background:rgba(0,168,0,.16);color:#4bd94b}}
.chart{{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px;margin-bottom:14px}}
.chart .t{{font-size:13px;color:var(--mut);margin-bottom:6px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}}
.legend{{font-size:11px;color:var(--mut);margin-bottom:4px}}
.legend .y{{color:#ffd24a}}.legend .b{{color:#3f7fd0}}
.verdict{{background:rgba(63,127,208,.12);border:1px solid rgba(63,127,208,.5);
border-radius:8px;padding:10px 12px;font-size:13px;margin:12px 0}}
.warn{{background:rgba(240,160,32,.1);border:1px solid rgba(240,160,32,.4);
border-radius:8px;padding:10px 12px;color:#f0c89a;font-size:13px;margin:10px 0}}
.foot{{color:var(--mut);font-size:11px;margin-top:20px;line-height:1.8}}
</style></head><body><div class="wrap">
<h1>量化账户 · v2.4 阶梯止盈止损调参报告</h1>
<div class="sub">窗口 {win[0]} ~ {win[1]}（{len(days)}个交易日）｜标的 {len(brows)} 只（已剔除 ZZ1000、神奇制药、风华高科）｜
期初资金 {BT_CAPITAL:,.2f} 元/只｜生成于 {now}｜数据：日K缓存池（180天，增量更新）</div>
<div class="cards">{cards_html}</div>
<div class="verdict">{verdict}</div>

<h2>动态止盈止损 · 参数敏感性（全网格扫描）</h2>
{''.join(sens)}

<h2>逐标的对比（基准 vs 主推动态组合）</h2>
{table}

<h2>个股K线·均线·操作点 & 权益曲线（v2.4 · {html.escape(blabel)}）</h2>
{''.join(charts)}

<h2>成交明细（v2.4 · {html.escape(blabel)}）</h2>
{ops_table}

<div class="foot">
口径：严格无未来函数——第 i 日收盘只用截至 i-1 日（含）的指标做决策，成交按当日开盘价；含佣金万2.5（最低5元）、
印花税卖出万5、过户费十万分之一。v2.4 = 趋势腿一笔满仓 + 阶梯移动止盈（最高收盘回撤达带宽清仓，浮盈&lt;15%带宽6%/
15~30%带10%/≥30%带15%）与 MA10 破位双退出；scalp/冰点腿半仓（自带中轨止盈/信号低点止损）。
阶梯止损 = 止损线随持仓最高浮盈取档上移（只上不下），收盘破线清仓；全部动态规则为止盈止损实验层参数
（run_ps_stock 的 dyn 入参），生产引擎现行参数=DYN_TRAIL_TIERS 6/10/15%；样本为当前量化账户标的近180日表现，
窗口较短且部分标的已清仓，结果仅作策略体检，不构成收益承诺。
</div></div></body></html>"""


def main():
    pool = load_pool()
    print(f"[量化回测] 标的 {len(pool)} 只（持仓中 {sum(1 for p in pool if p['sh'] > 0)} 只，"
          f"已剔除: ZZ1000、神奇制药、风华高科）")
    uni, win = build_uni(pool)
    if not win:
        print("[量化回测] 行情不足，无法回测")
        return 1
    print(f"[量化回测] 窗口 {win[0]} ~ {win[1]}")
    results = run_grid(pool, uni)
    blabel, bdyn, brows = pick_best(results)
    cur_label = "阶梯止盈6/10/15%(现行v2.4)"
    base = results["基准(仅MA10)"]
    cur = results[cur_label]

    def avg(rows, key):
        return sum(r["r"][key] for r in rows) / len(rows) if rows else 0

    a_cur = avg(cur, "return")
    print(f"\n[网格扫描] {'参数组合':<26}{'v2.4等权':>10}{'触发':>5}{'均回撤':>8}{'Δvs现行':>10}")
    for label, _ in GRID:
        rows = results[label]
        if not rows:
            continue
        a = avg(rows, "return")
        mdd = avg(rows, "mdd")
        hits = sum(r["hits"] for r in rows)
        mark = "  ← 主推" if label == blabel else ("  (现行)" if label == cur_label else "")
        print(f"  {label:<26}{a * 100:>+9.2f}%"
              f"{hits:>4}次{mdd * 100:>7.1f}%{(a - a_cur) * 100:>+9.2f}pp{mark}")
    a_best = avg(brows, "return")
    print(f"\n[等权] 基准(仅MA10) {avg(base, 'return') * 100:+.2f}% -> 现行v2.4 {a_cur * 100:+.2f}%"
          f" -> 主推[{blabel}] {a_best * 100:+.2f}% (较现行 {(a_best - a_cur) * 100:+.2f}pp)")
    print("\n[逐标的] 标的 / 买入持有 / 基准MA10 / 现行v2.4 / 主推")
    cmap = {r["code"]: r for r in cur}
    bmap = {r["code"]: r for r in brows}
    for r in base:
        c, b = cmap[r["code"]], bmap[r["code"]]
        print(f"  {r['name'] + '(' + r['code'] + ')':<16}{r['bh'] * 100:>+8.2f}%"
              f"{r['r']['return'] * 100:>+9.2f}%{c['r']['return'] * 100:>+9.2f}%"
              f"{b['r']['return'] * 100:>+9.2f}%")
    if "--no-dashboard" not in sys.argv:
        os.makedirs(config.REPORT_DIR, exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(build_report(results, uni, win, (blabel, bdyn, brows)))
        print(f"报告 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
