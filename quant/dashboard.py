# -*- coding: utf-8 -*-
"""量化 Dashboard(HTML, 实盘+回测合并单页):
  实盘: 账户概况 + 权益曲线 + 当日持仓变化(昨收→开盘成交→收盘) + 持股日K操作点 +
        本次成交 + 智能选股
  回测: 分层资金曲线/对比表 + 月度收益 + 参数稳健性 + 6股个股独立策略操作点
由 quant_daily.py 收盘后生成, 输出 report/dashboard_quant.html
回测数据来自 backtest/daily.py(本地缓存, <1秒); 回测不可用时自动降级为纯实盘页
"""
import csv
import datetime
import json
import os

import config_quant as config
from common import quotes
from core.indicators import build_ind
from quant import account

KLT_DAILY, BARS_LMT = 101, 120
# 日后操作计划表头(2026-11-21结构化7列; engine日报渲染与前端plans_head共用同一定义)
PLAN_HEADERS = ["标的", "明日动作", "出场线/事项", "触发价", "触发卖出",
                "预计回笼", "触发条件与分档明细"]
DASH_PATH = os.path.join(config.REPORT_DIR, "quant", "dashboard_quant.html")
os.makedirs(os.path.dirname(DASH_PATH), exist_ok=True)   # report/quant/ 子目录(2026-10-28用户要求与T侧日报分离)


def _load_ops():
    """成交流水 -> {code: [{date,side,qty,price,reason,leg}]}"""
    out = {}
    if not os.path.exists(account.TRADES_CSV):
        return out
    try:
        with open(account.TRADES_CSV, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = (row.get("code") or "").strip()
                if not code:
                    continue
                try:
                    op = {"date": (row.get("date") or "")[:10],
                          "side": (row.get("side") or "").strip(),
                          "qty": int(float(row.get("qty") or 0)),
                          "price": float(row.get("price") or 0),
                          "reason": row.get("reason") or "",
                          "leg": row.get("leg") or ""}
                except ValueError:
                    continue
                out.setdefault(code, []).append(op)
    except Exception as e:
        print("操作点读取失败", e)
    return out


def _load_today_fills(rep_date):
    """当日成交(单一事实来源=成交流水CSV): 按 date==rep_date 过滤, 含名称/金额/费用
    注意不能用_load_ops(它丢弃amount/fee且按code分组)"""
    out = []
    if not rep_date or not os.path.exists(account.TRADES_CSV):
        return out
    try:
        with open(account.TRADES_CSV, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (row.get("date") or "")[:10] != rep_date:
                    continue
                try:
                    out.append({
                        "date": rep_date, "code": (row.get("code") or "").strip(),
                        "name": row.get("name") or "",
                        "side": (row.get("side") or "").strip(),
                        "qty": int(float(row.get("qty") or 0)),
                        "price": float(row.get("price") or 0),
                        "amount": float(row.get("amount") or 0),
                        "fee": float(row.get("fee") or 0),
                        "reason": row.get("reason") or "",
                        "leg": row.get("leg") or ""})
                except ValueError:
                    continue
    except Exception as e:
        print("当日成交读取失败", e)
    return out


def _sig_text(acct):
    p = acct.get("pending")
    if not p:
        return "持有" if acct["sh"] > 0 else "观望"
    label = {"SELL": "卖出", "BUY_TREND": "趋势买入",
             "BUY_SCALP": "超跌买入"}.get(p["act"], p["act"])
    if p["act"] == "BUY_SCALP" and p.get("need") is not None and not p.get("o_checked"):
        label += f"(开盘≥{p['need']:.2f}确认)"
    return (label + " " + p.get("reason", "")).strip()


def _stock_payload(code, a, bars, ops, rep_date):
    ind = build_ind(bars)                       # 均线用全量历史计算
    since = getattr(config, "DASH_SINCE", "")   # 展示窗口: 默认自10.8起(均线值保留, 只切显示)
    k0 = next((k for k, b in enumerate(bars) if b["dt"][:10] >= since), 0)
    win = bars[k0:]
    wdates = {b["dt"][:10] for b in win}
    # 本策略成交恒为"收盘判定, 次日开盘价成交" -> 时间统一标注开盘09:30
    in_win = [dict(op, time=op.get("time") or "09:30")
              for op in ops if op["date"] in wdates]
    cl = bars[-1]["close"]
    pnl_pct = ((cl / a["cost"] - 1) * 100) if a.get("cost") else None
    # 悬停提示用: 每日涨跌幅(首根用窗口前一日收盘兜底) + 量/额
    prev0 = bars[k0 - 1]["close"] if k0 > 0 else None
    chg = []
    for i, b in enumerate(win):
        pc = win[i - 1]["close"] if i > 0 else prev0
        chg.append(round(b["close"] / pc - 1, 4) if pc else None)
    return {
        "name": a["name"], "sh": a["sh"], "cost": a.get("cost") or 0.0,
        "close": cl, "pnl_pct": pnl_pct,
        "leg": "趋势腿" if a.get("sh_tr") else "超跌腿",
        "hold_d": a.get("hold_d") or 0,
        "cash": round(a["cash"], 2), "sig": _sig_text(a),
        "pool": bool(a.get("pool")),
        "days": [b["dt"][:10] for b in win],
        "open": [b["open"] for b in win], "close_": [b["close"] for b in win],
        "high": [b["high"] for b in win], "low": [b["low"] for b in win],
        "vol": [round(b.get("vol") or 0, 1) for b in win],
        "amount": [round(b.get("amount") or 0, 2) for b in win],
        "chg": chg,
        "ma5": ind["ma5"][k0:], "ma10": ind["ma10"][k0:], "ma20": ind["ma20"][k0:],
        "ops": in_win,
        "range_chg": round(cl / win[0]["close"] - 1, 4) if win else 0,
    }


# ============================== 回测数据(本地缓存, 失败降级None) ==============================
_BT_CACHE = [None]


def _backtest_payload():
    """跑回测取看板数据(缓存窗口, 纯本地缓存<1秒); 任何异常返回None, 页面降级为纯实盘"""
    if _BT_CACHE[0] is not None:
        return _BT_CACHE[0]
    try:
        from backtest import daily as bt
        uni = bt.load_universe()
        codes = sorted(bt.POOL)
        days = [b["dt"] for b in uni[codes[0]]["bars"]]
        capital = bt.BT_CAPITAL
        layers = {}
        for lyr in ("bh", "hold", "trend", "rot"):
            layers[lyr] = bt.run_bt(uni, bt.BASE_P, lyr, capital)
        stocks = {c: {"name": bt.POOL[c],
                      "days": [x["dt"] for x in uni[c]["bars"]],
                      "open": [x["open"] for x in uni[c]["bars"]],
                      "close": [x["close"] for x in uni[c]["bars"]],
                      "high": [x["high"] for x in uni[c]["bars"]],
                      "low": [x["low"] for x in uni[c]["bars"]]} for c in codes}
        rob = bt.sweep_rot(uni, capital, 10)
        ps = bt.per_stock_bt(uni, capital)
        cand = {k: v["return"] for k, v in layers.items() if k != "bh"}
        bk = max(cand, key=cand.get)
        payload = {
            "days": days, "capital": capital, "best": bk,
            "layers": {k: {"return": v["return"], "final": v["final_asset"],
                           "mdd": v["mdd"], "trades": v["n_trades"],
                           "fees": v["fees"], "n_stop": v.get("n_stop", 0),
                           "equity": v["equity"], "ops": v["ops"],
                           "monthly": bt.monthly_returns(v)}
                       for k, v in layers.items()},
            "stocks": stocks, "rob": rob, "per_stock": ps,
        }
        _BT_CACHE[0] = payload
        return payload
    except Exception as e:
        print("[dashboard] 回测数据获取失败, 降级为纯实盘页:", e)
        return None


# ============================== 当日持仓变化(昨收→开盘成交→收盘) ==============================
def _holdings_change(before, state, fills, rep_date, stocks, bars_map):
    """构造当日持仓变化表; before=None(无推进前快照)时返回None
    口径: 本策略所有成交均在次日开盘价, 盘中股数不变;
    昨收持股(before.sh) → 今日开盘成交(fills) → 收盘持股(after.sh)"""
    if not before:
        return None
    fills_by = {}
    for r in fills or []:
        fills_by.setdefault(r["code"], []).append(r)
    codes = set(before.get("accounts", {})) | set(state["accounts"])
    rows = []
    for code in sorted(codes):
        b = before.get("accounts", {}).get(code)
        a = state["accounts"].get(code)
        acts = fills_by.get(code, [])
        bsh = b.get("sh", 0) if b else 0
        ash = a.get("sh", 0) if a else 0
        acash = a.get("cash", 0.0) if a else 0.0
        # 今日收盘价: 优先持仓payload, 其次bars_map末根
        close = None
        if code in stocks:
            close = stocks[code]["close"]
        elif code in bars_map and bars_map[code]:
            close = bars_map[code][-1]["close"]
        # 昨收价(before估值): bars_map倒数第2根; 取不到用before.px兜底
        prev_close = None
        if code in bars_map and len(bars_map[code]) >= 2:
            prev_close = bars_map[code][-2]["close"]
        elif b and b.get("px"):
            prev_close = b["px"]
        if not b and not a and not acts:
            continue
        # 只保留: 昨收有持仓 / 今日有成交 / 收盘有持仓或现金(待买) 的账户
        if bsh == 0 and ash == 0 and not acts and acash <= 0:
            continue
        rows.append({
            "code": code, "name": (a or b).get("name", code),
            "new": bsh == 0 and ash > 0,            # 今日新开仓: 昨收无持仓, 收盘有持仓
            "before_sh": bsh,
            "acts": [{"side": r["side"], "qty": r["qty"], "price": r["price"],
                      "reason": r.get("reason", "")} for r in acts],
            "after_sh": ash,
            "close": close,
            "pct": (round((close / prev_close - 1) * 100, 2)
                    if close is not None and prev_close else None),   # 当日涨跌幅
            "mv": round(ash * close, 2) if close is not None else None,
            "cash": round(acash, 2),
            "sig": _sig_text(a) if a else "",
            "pool": bool(a and a.get("pool")),
            "_b_mv": round(bsh * prev_close, 2) if prev_close is not None else None,
        })
    # 排序: 继续持有 → 今日清仓 → 新入选/待买 → 其他
    def _rank(r):
        if r["after_sh"] > 0:
            return 0
        if r["before_sh"] > 0:
            return 1
        if r["new"] or r["cash"] > 0:
            return 2
        return 3
    rows.sort(key=lambda r: (_rank(r), -max(r["before_sh"], r["after_sh"])))

    # 汇总: 昨收总资产 / 收盘总资产 / 成交回笼支出 / 费用
    pool_b = before.get("pool_cash", 0.0)
    before_total = sum((b.get("cash", 0.0) if b else 0.0)
                       for b in [before["accounts"].get(c) for c in codes]) + pool_b
    miss_mv = False
    for r in rows:
        if r["before_sh"] > 0:
            if r["_b_mv"] is None:
                miss_mv = True
            else:
                before_total += r["_b_mv"]
    sell_amt = sum(r["amount"] for r in (fills or []) if r["side"] == "SELL")
    buy_amt = sum(r["amount"] for r in (fills or []) if r["side"] == "BUY")
    fee_amt = sum(r.get("fee", 0) for r in (fills or []))
    return {
        "rep_date": rep_date,
        "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
        "n_before_hold": sum(1 for c, b in before.get("accounts", {}).items()
                             if b.get("sh", 0) > 0),
        "n_after_hold": sum(1 for a in state["accounts"].values() if a.get("sh", 0) > 0),
        "before_total": None if miss_mv else round(before_total, 2),
        "sell_amt": round(sell_amt, 2), "buy_amt": round(buy_amt, 2),
        "fee_amt": round(fee_amt, 2),
    }


def build(state, bars_map=None, fills=None, rep_date=None, sel=None, now=None,
          path=None, before=None, plans=None, rebal_info=None, obs=None, menv=None,
          idle_reason=None):
    """生成 dashboard_quant.html(实盘+回测合并); 返回文件路径
    before: 引擎推进前的状态深拷贝(用于当日持仓变化); None则不出该区
    menv: 市场环境快照(engine._mkt_env, 信息层展示, 不参与决策)
    idle_reason: 选股池现金未入场原因(engine._idle_cash_reason, 总览展示)"""
    now = now or datetime.datetime.now()
    rep_date = rep_date or max(
        (a.get("last_date") or "" for a in state["accounts"].values()), default="")
    ops_by_code = _load_ops()
    bars_map = dict(bars_map or {})
    held, missing, stocks = [], [], {}
    for code in sorted(state["accounts"]):
        a = state["accounts"][code]
        if a["sh"] <= 0:
            continue                                # 清仓标的: 不出图(流水可查)
        held.append((code, a))
        bars = bars_map.get(code)
        if not bars:
            try:
                bars = quotes.fetch_kline_bars(code, klt=KLT_DAILY, lmt=BARS_LMT)
            except Exception as e:
                print(f"[dashboard] {a['name']}({code}) 日K获取失败: {e}")
                bars = None
        if bars and bars[-1]["dt"][:10] == now.strftime("%Y-%m-%d") and now.hour < 15:
            bars = bars[:-1]
        if not bars:
            missing.append(f"{a['name']}({code})")
            continue
        stocks[code] = _stock_payload(code, a, bars, ops_by_code.get(code, []),
                                      rep_date)
    pool = state.get("pool_cash", 0.0)
    # 总资产=所有子账户(现金+持仓市值), 含待买入账户现金(sh=0但cash>0); 持仓市值用最新收盘价,
    # 未取到K线的用状态里记录的上次收盘价px兜底
    quant_total = sum(
        a["cash"] + a["sh"] * (stocks[c]["close"] if c in stocks else (a.get("px") or 0))
        for c, a in state["accounts"].items())
    total = quant_total + pool
    base = state.get("baseline_total")
    chg = (total - base) if base else None
    # ── 当日成交口径: 以成交流水CSV为单一事实来源(运行时fills兜底), 卡片/表格/持仓变化同源 ──
    day_fills = _load_today_fills(rep_date) or list(fills or [])
    chg_p = _holdings_change(before, state, day_fills, rep_date, stocks, bars_map)
    # ── 当日盈亏: 唯一口径=权益曲线(今日收盘总资产 − 昨日收盘总资产) ──
    # 不可用before_total兜底: 它=今日开盘成交后的持仓按昨收价估值, 会漏掉今日建仓的成交差价
    daily_pnl = None
    daily_pnl_pct = None
    _eq = state.get("equity") or []
    if len(_eq) >= 2 and _eq[-1].get("date") == rep_date:
        _p0, _p1 = _eq[-2].get("total"), _eq[-1].get("total")
        if _p0 and _p1 is not None:
            daily_pnl = round(_p1 - _p0, 2)
            daily_pnl_pct = round(daily_pnl / _p0 * 100, 2)
    # ── 判断今日是否有新开仓(持仓变化表中 new=true 的行) ──
    has_new_pos = bool(chg_p) and any(r.get("new") for r in chg_p["rows"])
    # ── 回测: 仅当有新开仓时才加载(省时间 + 用户明确要求) ──
    bt_payload = _backtest_payload() if has_new_pos else None
    # 池内入池天数口径与邮件一致(交易日): 用权益记录日期序列近似交易日历
    _eq_days = [x.get("date") for x in (state.get("equity") or [])]
    payload = {
        "quant": {
            "rep_date": rep_date, "init_date": state.get("init_date"),
            "gen_time": now.strftime("%Y-%m-%d %H:%M"),
            "baseline": base, "total": round(total, 2),
            "quant_total": round(quant_total, 2),
            "pool": round(pool, 2), "chg": round(chg, 2) if chg is not None else None,
            "chg_pct": round(chg / base * 100, 2) if base else None,
            "daily_pnl": daily_pnl, "daily_pnl_pct": daily_pnl_pct,
            "idle_reason": idle_reason,
            "has_new_pos": has_new_pos,
            "cum_fee": round(state.get("cum_fee", 0), 2),
            "n_hold": len(stocks), "missing": missing,
            "equity": state.get("equity") or [],
            "stocks": stocks,
            "fills": day_fills,
            "chg_pos": chg_p,
            "plans": (plans or {}).get("rows") or [],
            "plans_cards": (plans or {}).get("cards") or [],
            "plans_head": PLAN_HEADERS,
            "rebal": rebal_info,
            "sel": ({"rep_date": sel.get("rep_date"),
                     "boards": [{"kind_cn": b["kind_cn"], "name": b["name"],
                                 "pct": b["pct"], "mom": b["mom"], "amount": b["amount"]}
                                for b in sel["boards"]],
                     "passing": [{"code": p["code"], "name": p["name"], "close": p["close"],
                                  "mom": p["mom"],
                                  "board": p.get("board_disp")
                                  or f"{p['board_kind_cn']}·{p['board_name']}",
                                  "board_mom": p["board_mom"]} for p in sel["passing"][:8]],
                     "invested": sel.get("invested") or [],
                     "cand_n": sel["cand_n"], "skip": sel["skip"]}
                    if sel else None),
            "obs_pool": ({"zt_on": obs[2],
                      "best": [{"name": e["name"], "mom": e["mom"]}
                               for e in (obs[1] or [])],
                      "rows": [{"code": e["code"], "name": e["name"],
                                "board": e.get("board_disp")
                                or f"{e['board_kind_cn']}·{e['board_name']}",
                                "mom": e["mom"], "close": e["close"],
                                "hits": e["hits"],
                                "chg_in": (round((e["close"] / e["first_close"] - 1)
                                                 * 100, 1)
                                           if e.get("first_close") else None),  # 进池后涨幅
                                "in_days": (len(_eq_days) - _eq_days.index(e["first_date"])
                                            if e.get("first_date") in _eq_days
                                            and rep_date in _eq_days else None),
                                "extreme": e.get("extreme", False),
                                "held": state["accounts"].get(e["code"], {})
                                        .get("sh", 0) > 0}
                               for e in (obs[0] or [])]}
                     if obs and (obs[0] or obs[1]) else None),
            "menv": menv,
        },
        "bt": bt_payload,
    }
    path = path or DASH_PATH
    with open(path, "w", encoding="utf-8") as f:
        f.write(_TPL.replace("__DATA__", json.dumps(payload, ensure_ascii=False)))
    day = (rep_date or now.strftime("%Y-%m-%d")).replace("-", "")[-6:]
    try:                             # 日归档副本(用户规则: 看板名带YYMMDD日期, 如_261028)
        ap = os.path.join(os.path.dirname(DASH_PATH), "history")
        os.makedirs(ap, exist_ok=True)
        with open(os.path.join(ap, f"dashboard_quant_{day}.html"),
                  "w", encoding="utf-8") as f:
            f.write(_TPL.replace("__DATA__",
                                 json.dumps(payload, ensure_ascii=False)))
    except Exception:
        pass
    return path


_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>量化策略 Dashboard · 实盘+回测</title>
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
.tag.h{background:rgba(176,107,214,.18);color:#c79ae0}
.chart{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px;margin-bottom:14px;position:relative}
.tip{position:absolute;display:none;z-index:9;background:rgba(18,24,36,.97);
border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12px;
line-height:1.65;color:var(--fg);pointer-events:none;max-width:270px;
box-shadow:0 8px 20px rgba(0,0,0,.5);white-space:nowrap}
.tip .tt{color:#fff;font-weight:700;margin-bottom:3px}
.tip .mut{color:var(--mut)}
.note{font-size:12px;color:var(--mut);line-height:1.8;margin:-8px 0 10px}
.plantbl td{vertical-align:top}
.plantbl tr.grp td{border-top:2px solid #46536b}
.plantbl td table{width:auto;background:transparent}
.plantbl td table td{border-top:none}
.plantbl td table tr:hover td{background:transparent}
.plancard{background:var(--card)}
.plancard table{margin:4px 0}
.plancard table td,.plancard table th{background:transparent}
.plancard tr:hover td{background:transparent}
.chart .t{font-size:13px;color:var(--mut);margin-bottom:6px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}
canvas{width:100%;display:block}
.warn{background:rgba(240,160,32,.1);border:1px solid rgba(240,160,32,.4);
border-radius:8px;padding:10px 12px;color:#f0c89a;font-size:13px;margin:10px 0}
.ok{background:rgba(0,168,0,.12);border:1px solid rgba(0,168,0,.5);color:#8fe88f}
.foot{color:var(--mut);font-size:11px;margin-top:20px;line-height:1.8}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:820px){.cols{grid-template-columns:1fr}}
.sumbar{display:flex;flex-wrap:wrap;gap:8px 22px;font-size:13px;margin:4px 2px 10px;color:var(--mut)}
.sumbar b{color:var(--fg)}
</style></head><body><div class="wrap">
<h1>量化策略 Dashboard · 实盘账户 + 回测</h1>
<div class="sub" id="sub"></div>

<h2>总览</h2>
<div id="cards" class="cards"></div>
<div id="verdict"></div>
<div id="warn"></div>

<h2>资金 / 权益曲线</h2>
<div class="cols">
 <div class="chart"><div class="t"><span>实盘账户权益 (逐日)</span><span id="qlg"></span></div>
 <canvas id="qeq" height="220"></canvas></div>
 <div class="chart btsec"><div class="t"><span>回测分层资金曲线 (全期)</span><span id="blg"></span></div>
 <canvas id="beq" height="220"></canvas></div>
</div>

<h2>当日持仓变化 (昨收 → 开盘成交 → 收盘)</h2>
<div id="chgpos"></div>

<h2>日后操作计划（每只持仓的后继策略）</h2>
<div class="note"><b>💰=止盈线（红）｜🛑=止损/破位线（绿）</b>；持仓按出场线逐条列出，触发价最高者最可能先触发。
三线<b>同时在场、先到先卖，任一触发即全清该股</b>；下方「阶梯止盈分档」给出各档触发线、卖出股数与约回笼金额。</div>
<div id="plans"></div>

<h2>实盘 · 持仓标的 日K与买卖操作点</h2>
<div class="note">均线: <b style="color:#b06bd6">MA5</b> · <b style="color:#f0a020">MA10</b> · <b style="color:#3f7fd0">MA20</b>　定点: <span style="color:#ff5c5c">▲买入</span> / <span style="color:#4bd94b">▼卖出</span>（均为"收盘出信号、次日09:30开盘价成交"）　鼠标移到K线上可查看当日详情</div>
<div id="qstocks"></div>

<h2>实盘 · 当日成交</h2>
<div id="fills"></div>

<h2>实盘 · 智能选股</h2>
<div id="sel"></div>

<h2>实盘 · 候选观察池(每日累积 · 调仓日择优建仓)</h2>
<div id="pool"></div>

<h2>实盘 · 市场环境 (大盘与持仓板块 · 信息层)</h2>
<div id="menv"></div>

<h2 class="btsec">回测 · 分层结果对比</h2>
<table class="btsec" id="btbl"></table>

<h2 class="btsec">回测 · 月度收益 (权益环比 %)</h2>
<div class="btsec" id="mtb" style="padding:0;margin-bottom:14px"></div>

<h2 class="btsec">回测 · 参数稳健性检验</h2>
<div class="btsec" id="rob" style="padding:0;margin-bottom:14px"></div>

<h2 class="btsec">回测 · 个股独立策略操作点 (自适应双模)</h2>
<div class="btsec" id="ps"></div>

<div class="foot" id="foot"></div>
</div>
<script>
const D=__DATA__;
const Q=D.quant, B=D.bt;
const pct=x=>x==null?'-':(x*100>=0?'+':'')+(x*100).toFixed(2)+'%';
const pctQ=x=>x==null?'-':(x>=0?'+':'')+x.toFixed(2)+'%';
const pct1=x=>(x>=0?'+':'')+(x*100).toFixed(1)+'%';
const clr=x=>x>=0?'pos':'neg';
const num=x=>x==null?'-':x.toLocaleString(undefined,{maximumFractionDigits:2});
// 策略腿中文名: trend=趋势腿(趋势跟随, 收盘破MA10出场); scalp=超跌腿(超跌反弹, 中轨止盈)
const legCN=x=>({trend:'趋势腿',scalp:'超跌腿',ice:'冰点左侧',vfan:'恐慌V反'}[x]||x||'-');

// ---------- 通用: 画布悬停提示(2倍缩放坐标换算 + 自动tip层 + 防溢出定位) ----------
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

// ---------- sub ----------
document.getElementById('sub').textContent=
 `实盘 ${Q.init_date}→${Q.rep_date||''} | 生成 ${Q.gen_time} | 期初基准 ${num(Q.baseline)} 元 | 实盘持仓 ${Q.n_hold} 只`
 +(B?` | 回测窗口 ${B.days[0]}~${B.days[B.days.length-1]} (${B.days.length}交易日), 期初 ${B.capital.toLocaleString()} 元`:'')
 +(Q.idle_reason&&Q.pool>=300?` | 💰 选股池现金未入场: ${Q.idle_reason}`:'');

// ---------- cards ----------
let cards=[
 ['实盘总资产',num(Q.total),''],
 [`实盘较基准(${Q.init_date})`,Q.chg==null?'-':`${Q.chg>=0?'+':''}${num(Q.chg)} (${pctQ(Q.chg_pct)})`,clr(Q.chg||0)],
 ['当日盈亏',Q.daily_pnl==null?'-':`${Q.daily_pnl>=0?'+':''}${num(Q.daily_pnl)} (${pctQ(Q.daily_pnl_pct)})`,clr(Q.daily_pnl||0),'口径: 今日收盘总资产 − 昨日收盘总资产(含今日开盘建仓的成交损益), 数据源=权益曲线'],
 ['选股池现金',num(Q.pool),''],
 ['累计费用',num(Q.cum_fee),''],
 ['当日成交',`${(Q.fills||[]).length} 笔`,''],
];
if(B){const lay=B.layers,bk=B.best,best=lay[bk];
 cards.push([`回测最优: 动量轮动 ★`,pct(best.return),clr(best.return)]);
 cards.push(['回测最大回撤',(best.mdd*100).toFixed(1)+'%','neg']);
 cards.push(['回测成交',best.trades+' 笔','']);}
document.getElementById('cards').innerHTML=cards.map(([k,v,c,ti])=>
 `<div class="card"${ti?` title="${ti}"`:''}><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join('');
if(B){const lay=B.layers,bk=B.best,best=lay[bk],hit=best.return>=0.20;
 const NAMES={bh:'等权持有',hold:'L1做T',trend:'L2趋势择时',rot:'L2动量轮动'};
 document.getElementById('verdict').innerHTML=`<div class="warn ${hit?'ok':''}">
 回测目标 20~50% → 最优层 <b>${NAMES[bk]}</b> 全期 <b>${pct(best.return)}</b> ${hit?'✅ 达标':'❌ 未达标'}
 ｜ 期末 ${num(best.final)} · 回撤 ${(best.mdd*100).toFixed(1)}% · ${best.trades}笔</div>`;}
if((Q.missing||[]).length)
 document.getElementById('warn').innerHTML=
  `<div class="warn">本次未取到日K: ${Q.missing.join('、')} —— 未出图, 总资产按最近一次收盘价估值</div>`;

// ---------- 实盘权益曲线(Y轴自适应 + 悬停查看当日) ----------
window.__drawQEq=function(hover){const eq=Q.equity||[],cv=document.getElementById('qeq');
 if(!cv)return;
 cv.width=cv.clientWidth*2;cv.height=220*2;const ctx=cv.getContext('2d');
 const W=cv.width,H=cv.height,pad=52*2;
 ctx.clearRect(0,0,W,H);
 ctx.strokeStyle='#2a3446';ctx.lineWidth=2;
 ctx.beginPath();ctx.moveTo(pad,pad);ctx.lineTo(pad,H-pad);ctx.lineTo(W-pad,H-pad);ctx.stroke();
 if(!eq.length){ctx.fillStyle='#9aa7bd';ctx.font='13px sans-serif';
  ctx.fillText('暂无权益记录',pad+10,pad+24);window.__qeq_st=null;return;}
 // Y轴自动适应: 取数据范围并留6%上下边距(基准虚线仅在范围内绘制, 不被无关锚点拉宽)
 let mx=Math.max(...eq.map(p=>p.total)),mn=Math.min(...eq.map(p=>p.total));
 const sp=(mx-mn)||Math.abs(mx)*0.01||1;mx+=sp*.06;mn-=sp*.06;
 const X=i=>pad+(W-2*pad)*(eq.length===1?0.5:i/(eq.length-1)),Y=v=>H-pad-(H-2*pad)*(v-mn)/(mx-mn);
 ctx.font='12px sans-serif';
 for(let g=0;g<=4;g++){const y=pad+(H-2*pad)*g/4;ctx.fillStyle='#7f8ca3';
  ctx.fillText(num(mx-(mx-mn)*g/4),6,y+4);}
 const step=Math.max(1,Math.ceil(eq.length/6));
 for(let i=0;i<eq.length;i+=step){ctx.fillStyle='#7f8ca3';
  ctx.fillText(eq[i].date.slice(5),X(i)-18,H-pad+18);}
 if(Q.baseline&&Q.baseline>=mn&&Q.baseline<=mx){const yb=Y(Q.baseline);
  ctx.strokeStyle='#8899aa';ctx.setLineDash([6,6]);ctx.lineWidth=2;
  ctx.beginPath();ctx.moveTo(pad,yb);ctx.lineTo(W-pad,yb);ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle='#8899aa';ctx.fillText('基准 '+num(Q.baseline),pad+6,yb-6);}
 ctx.strokeStyle='#3f7fd0';ctx.lineWidth=3;ctx.beginPath();
 eq.forEach((p,i)=>{i?ctx.lineTo(X(i),Y(p.total)):ctx.moveTo(X(i),Y(p.total))});ctx.stroke();
 ctx.fillStyle='#3f7fd0';
 eq.forEach((p,i)=>{ctx.beginPath();ctx.arc(X(i),Y(p.total),5,0,7);ctx.fill();});
 if(hover!=null&&hover>=0&&hover<eq.length){          // 十字线+当日点高亮
  const hx=X(hover),hy=Y(eq[hover].total);
  ctx.strokeStyle='#8899aa';ctx.lineWidth=1;ctx.setLineDash([5,5]);
  ctx.beginPath();ctx.moveTo(hx,pad);ctx.lineTo(hx,H-pad);ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle='#e8edf5';ctx.beginPath();ctx.arc(hx,hy,7,0,7);ctx.fill();}
 document.getElementById('qlg').innerHTML=`<span class="mut">${eq.length} 个交易日 · 最新 ${num(eq[eq.length-1].total)} (${pct(eq[eq.length-1].total/(Q.baseline||eq[eq.length-1].total)*100-100)})</span>`;
 window.__qeq_st={pad:pad,W:W,n:eq.length};};
window.__drawQEq(null);
(function(){const eq=Q.equity||[];if(!eq.length)return;
 attachHover(document.getElementById('qeq'),i=>window.__drawQEq(i),
  cx=>window.__qeq_st?idxOf(cx,window.__qeq_st.pad,window.__qeq_st.W,window.__qeq_st.n):null,
  i=>{const p=eq[i],pv=i>0?eq[i-1].total:null,d=pv!=null?p.total-pv:null;
   let hh=`<div class="tt">${p.date}</div><div>总资产 <b>${num(p.total)}</b></div>`;
   if(d!=null)hh+=`<div>较前日 <b class="${clr(d)}">${d>=0?'+':''}${num(d)} (${pctQ(d/pv*100)})</b></div>`;
   if(p.quant!=null)hh+=`<div class="mut">策略市值 ${num(p.quant)} · 选股池 ${num(p.pool||0)}</div>`;
   if(Q.baseline)hh+=`<div class="mut">较基准 <span class="${clr(p.total-Q.baseline)}">${p.total-Q.baseline>=0?'+':''}${num(p.total-Q.baseline)} (${pctQ((p.total-Q.baseline)/Q.baseline*100)})</span></div>`;
   return hh;});
})();

// ---------- 当日持仓变化 ----------
(function(){const C=Q.chg_pos;
 const el=document.getElementById('chgpos');
 if(!C){el.innerHTML='<div class="mut">无推进前快照(手动补出页面), 次日起自动显示</div>';return;}
 let bar=`<div class="sumbar">
  <span>持仓只数 <b>${C.n_before_hold}</b> → <b>${C.n_after_hold}</b></span>
  <span>今日成交 <b>${(Q.fills||[]).length}</b> 笔</span>
  <span>卖出回笼 <b class="neg">${num(C.sell_amt)}</b></span>
  <span>买入支出 <b class="pos">${num(C.buy_amt)}</b></span>
  <span>费用 <b>${num(C.fee_amt)}</b></span>`;
 if(C.before_total!=null){const d=Q.total-C.before_total;
  bar+=`<span>总资产 昨收 <b>${num(C.before_total)}</b> → 收盘 <b>${num(Q.total)}</b>
   <b class="${clr(d)}">${d>=0?'+':''}${num(d)}</b></span>`;}
 bar+='</div>';
 let h='<tr><th>标的</th><th>昨收持股</th><th>今日开盘成交</th><th>收盘持股</th><th>今收价</th><th>涨跌幅</th><th>收盘市值</th><th title="每股为独立子账户: 卖出回款(或拨款的买入零头)暂存于此, 不参与其他标的买入; 调仓日归集到选股池">子账户现金</th><th>明日信号</th></tr>';
 for(const r of C.rows){
  const act=r.acts.map(a=>`<span class="tag ${a.side==='BUY'?'b':'s'}">${a.side==='BUY'?'买入':'卖出'} ${a.qty}@${a.price.toFixed(3)} ${a.reason}</span>`).join(' ')||'<span class="mut">—</span>';
  const shCls=r.before_sh>0&&r.after_sh===0?'neg':(r.before_sh===0&&r.after_sh>0?'pos':'');
  h+=`<tr><td>${r.new?'<span class="tag h">新入选</span> ':''}<b>${r.name}</b>(${r.code})</td>
   <td>${r.before_sh||'<span class="mut">0</span>'}</td>
   <td>${act}</td>
   <td class="${shCls}"><b>${r.after_sh||0}</b></td>
   <td>${r.close==null?'-':r.close.toFixed(3)}</td>
   <td>${r.pct==null?'<span class="mut">-</span>':`<span class="${clr(r.pct)}"><b>${r.pct>=0?'+':''}${r.pct.toFixed(2)}%</b></span>`}</td>
   <td>${r.mv==null?'-':num(r.mv)}</td>
   <td>${r.cash?num(r.cash):'<span class="mut">0</span>'}</td>
   <td>${r.sig||'<span class="mut">—</span>'}</td></tr>`;}
 el.innerHTML=bar+`<table>${h}</table>`
  +`<div style="font-size:12px;color:var(--mut);margin-top:8px;line-height:1.7">
显示持仓与今日变动口径说明 · <b>子账户现金</b>: 本系统给每只标的分配<b>独立子账户</b>,
该现金 = 这只股票卖出回款(或选股池拨款的买入零头)尚未再买入的余额,
<b>只能用于本标的的后续买入</b>, 不会被别的标的占用; 调仓日会被归集进"选股池现金"统一建新仓。
例: 新华文轩 2,174.06 = 池内拨款约145,373 − 买入6300股143,199 − 费用37.23 的零头。</div>`;
})();

// ---------- 日后操作计划(持仓后继策略) ----------
(function(){const C=Q.plans_cards||[];const P=Q.plans||[];const el=document.getElementById('plans');
 if(!C.length&&!P.length){el.innerHTML='<div class="mut">暂无计划</div>';return;}
 let h=C.map(c=>`<div class="plancard">${c}</div>`).join('');
 if(P.length){const H=Q.plans_head||['标的','明日动作','计划与触发条件'];
  h+='<table class="plantbl"><tr>'+H.map(x=>`<th>${x}</th>`).join('')+'</tr>';
  for(const r of P){h+=`<tr class="${r[0]?'grp':''}">`
   +H.map((_,i)=>`<td>${r[i]===undefined||r[i]===null?'':r[i]}</td>`).join('')+'</tr>';}
  h+='</table>';}
 el.innerHTML=h;})();

// ---------- 回测分层资金曲线 ----------
function drawBtEq(){if(!B)return;
 const NAMES={bh:'等权持有',hold:'L1做T',trend:'L2趋势择时',rot:'L2动量轮动'};
 const order=['bh','hold','trend','rot'].filter(k=>B.layers[k]);
 const COLORS={bh:'#8899aa',hold:'#3f7fd0',trend:'#f0a020',rot:'#b06bd6'};
 const lay=B.layers;
 const cv=document.getElementById('beq'),ctx=cv.getContext('2d');
 cv.width=cv.clientWidth*2;cv.height=220*2;
 const W=cv.width,H=cv.height,pad=46*2;
 let mn=1e18,mx=-1e18;
 for(const k of order)for(const v of lay[k].equity){mn=Math.min(mn,v);mx=Math.max(mx,v)}
 if(mn===mx){mn*=.99;mx*=1.01}
 const X=i=>pad+(W-2*pad)*i/(B.days.length-1),Y=v=>H-pad-(H-2*pad)*(v-mn)/(mx-mn);
 ctx.strokeStyle='#2a3446';ctx.lineWidth=2;ctx.beginPath();
 ctx.moveTo(pad,pad);ctx.lineTo(pad,H-pad);ctx.lineTo(W-pad,H-pad);ctx.stroke();
 for(const k of order){ctx.strokeStyle=COLORS[k];ctx.lineWidth=2;
  ctx.beginPath();lay[k].equity.forEach((v,i)=>{i?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v))});ctx.stroke();}
 document.getElementById('blg').innerHTML=order.map(k=>`<span style="color:${COLORS[k]};margin-left:10px">■ ${NAMES[k]} ${pct(lay[k].return)}</span>`).join('');

 // 分层表
 let h='<tr><th>策略层</th><th>全期收益</th><th>期末资产</th><th>最大回撤</th><th>成交笔数</th><th>止损</th><th>费用</th></tr>';
 for(const k of order){const v=lay[k];
  h+=`<tr><td>${NAMES[k]}${k===B.best?' ★':''}</td><td class="${clr(v.return)}"><b>${pct(v.return)}</b></td>
  <td>${num(v.final)}</td><td>${(v.mdd*100).toFixed(1)}%</td><td>${v.trades}</td>
  <td>${v.n_stop||0}</td><td>${num(v.fees)}</td></tr>`;}
 document.getElementById('btbl').innerHTML=h;

 // 月度
 (function(){const best=lay[B.best];const months=Object.keys(best.monthly||{}).sort();
  let m='<tr><th>策略层</th>'+months.map(x=>`<th>${x.slice(2)}</th>`).join('')+'</tr>';
  for(const k of order){const mm=lay[k].monthly||{};
   m+=`<tr><td>${NAMES[k]}</td>`+months.map(x=>{const v=mm[x];
    return `<td class="${v==null?'':clr(v)}">${v==null?'--':pct(v)}</td>`;}).join('')+'</tr>';}
  document.getElementById('mtb').innerHTML=`<table>${m}</table>`;})();

 // 稳健性
 (function(){const R=B.rob||[];
  let h='<tr><th>参数组合 (N / K / 调仓 / MA过滤)</th><th>全期收益</th><th>最大回撤</th><th>成交笔数</th></tr>';
  for(const r of R)h+=`<tr><td>N=${r.N} · K=${r.K} · ${r.rb}日调仓 · MA${r.ma?r.ma:'无'}过滤</td>
   <td class="${clr(r.ret)}"><b>${pct(r.ret)}</b></td><td>${(r.mdd*100).toFixed(1)}%</td><td>${r.trades}</td></tr>`;
  const pos=R.filter(r=>r.ret>=0.20).length;
  document.getElementById('rob').innerHTML=`<table>${h}</table>
   <div style="padding:8px 10px;color:var(--mut);font-size:12px">扫描 ${R.length} 组参数中，${pos} 组全期收益 ≥ 20% —— 参数稳健区间宽度。</div>`;})();

 // 个股独立策略
 (function(){const PS=B.per_stock||{};let h='';
  for(const c in PS){const s=PS[c],st=B.stocks[c],ops=s.ops||[];
   const last=st.close[st.close.length-1],first=st.close[0];
   h+=`<div class="chart"><div class="t"><span>${st.name} (${c})　标的区间涨跌 ${pct(last/first-1)}</span>
    <span>独立策略 <b class="${clr(s.return)}">${pct(s.return)}</b> · 回撤 ${(s.mdd*100).toFixed(1)}% · ${s.trades}笔</span></div>
   <canvas id="ps_${c}" height="180"></canvas>
   <div style="font-size:12px;color:var(--mut);margin-top:4px">`;
   for(const o of ops.slice(-24))h+=`<span class="tag ${o.side==='BUY'?'b':o.side==='SELL'?'s':'x'}" title="${o.reason||''}">${o.date.slice(5)} ${o.side} ${o.qty}@${o.price.toFixed(2)}</span>`;
   if(!ops.length)h+='<span style="color:#667">无操作</span>';
   h+=`</div></div>`;}
  document.getElementById('ps').innerHTML=h;
  window.__drawPS=function(c,hover){const cv=document.getElementById('ps_'+c);if(!cv)return;
   cv.width=cv.clientWidth*2;cv.height=180*2;const ctx=cv.getContext('2d');
   const W=cv.width,H=cv.height,s=B.stocks[c],ps=PS[c],pad=40*2;
   const mn=Math.min(...s.low),mx=Math.max(...s.high);
   const X=i=>pad+(W-2*pad)*i/(s.close.length-1),Y=v=>H-pad-(H-2*pad)*(v-mn)/(mx-mn);
   ctx.strokeStyle='#232d3e';ctx.lineWidth=1;
   for(let g=0;g<=4;g++){const y=pad+(H-2*pad)*g/4;ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(W-pad,y);ctx.stroke();}
   const bw=Math.max(2,(W-2*pad)/s.close.length*0.62);
   for(let i=0;i<s.close.length;i++){const o=s.open[i],cln=s.close[i],hi=s.high[i],lo=s.low[i];
    const col=cln>=o?'#e60000':'#00a800';ctx.strokeStyle=col;ctx.fillStyle=col;
    ctx.beginPath();ctx.moveTo(X(i),Y(hi));ctx.lineTo(X(i),Y(lo));ctx.stroke();
    const y1=Y(Math.max(o,cln)),y2=Y(Math.min(o,cln));ctx.fillRect(X(i)-bw/2,y1,bw,Math.max(1,y2-y1));}
   for(const op of (ps.ops||[])){const i=s.days.indexOf(op.date);if(i<0)continue;const x=X(i),y=Y(op.price);
    ctx.beginPath();
    if(op.side==='BUY'){ctx.fillStyle='#ff5c5c';ctx.moveTo(x,y+13);ctx.lineTo(x-9,y+27);ctx.lineTo(x+9,y+27);}
    else{ctx.fillStyle=op.side==='STOP'?'#f0a020':'#4bd94b';ctx.moveTo(x,y-13);ctx.lineTo(x-9,y-27);ctx.lineTo(x+9,y-27);}
    ctx.closePath();ctx.fill();}
   if(hover!=null&&hover>=0&&hover<s.close.length){      // 十字线+当日收盘高亮
    const hx=X(hover),hy=Y(s.close[hover]);
    ctx.strokeStyle='#8899aa';ctx.lineWidth=1;ctx.setLineDash([5,5]);
    ctx.beginPath();ctx.moveTo(hx,pad);ctx.lineTo(hx,H-pad);ctx.stroke();ctx.setLineDash([]);
    ctx.fillStyle='#e8edf5';ctx.beginPath();ctx.arc(hx,hy,6,0,7);ctx.fill();}};
  for(const c in PS){window.__drawPS(c,null);
   attachHover(document.getElementById('ps_'+c),i=>window.__drawPS(c,i),
    cx=>idxOf(cx,40*2,document.getElementById('ps_'+c).width,B.stocks[c].close.length),
    i=>{const s=B.stocks[c],ps=PS[c],o=s.open[i],cl=s.close[i],hi=s.high[i],lo=s.low[i];
     const ch=i>0?cl/s.close[i-1]-1:null,cl2=(ch!=null&&ch<0)?'neg':'pos';
     let hh=`<div class="tt">${s.days[i]}</div>
      <div>开 ${o.toFixed(3)} · 收 <b class="${cl2}">${cl.toFixed(3)}</b> · 涨跌 <b class="${cl2}">${ch==null?'-':(ch>=0?'+':'')+(ch*100).toFixed(2)+'%'}</b></div>
      <div>最高 ${hi.toFixed(3)} · 最低 ${lo.toFixed(3)}</div>`;
     const dayops=(ps.ops||[]).filter(p=>p.date===s.days[i]);
     if(dayops.length)hh+='<div style="margin-top:4px;border-top:1px solid #2a3446;padding-top:4px">'
      +dayops.map(p=>`<b class="${p.side==='BUY'?'pos':'neg'}">${p.side==='BUY'?'买入':p.side==='SELL'?'卖出':'止损'}</b> ${p.qty}股@${p.price.toFixed(3)} <span class="mut">${p.reason||''}</span>`).join('<br>')+'</div>';
     return hh;});}
 })();
}

// ---------- 实盘持仓 K线+MA+操作点(鼠标悬停查看当日详情) ----------
window.__qs_state={};
(function(){
 const S=Q.stocks;let h='';
 for(const c in S){const s=S[c];
  h+=`<div class="chart"><div class="t">
   <span><b>${s.name} (${c})</b>${s.pool?' <span class="tag h">选股</span>':''}　
   ${s.days[0].slice(5)}~${s.days[s.days.length-1].slice(5)} 区间 ${pct1(s.range_chg)}</span>
   <span>持股 <b>${s.sh}</b> · 成本 ${s.cost?s.cost.toFixed(3):'-'} · 现价 ${s.close.toFixed(3)} ·
   浮盈 <b class="${s.pnl_pct==null?'mut':clr(s.pnl_pct)}">${s.pnl_pct==null?'-':pctQ(s.pnl_pct)}</b> ·
   ${s.leg}${s.hold_d?'('+s.hold_d+'日)':''} · 信号 <b>${s.sig}</b></span></div>
  <canvas id="qc_${c}" height="190"></canvas>
  <div style="font-size:12px;color:var(--mut);margin-top:6px">`;
  for(const o of s.ops.slice(-20))
   h+=`<span class="tag ${o.side==='BUY'?'b':'s'}" style="cursor:pointer" title="点击定位到该日K线 · ${o.reason}|${legCN(o.leg)}|${o.time}开盘成交" onclick="window.__qs_showTipDate('${c}','${o.date}',event.clientX,event.clientY)">${o.date.slice(5)} 开盘${o.side==='BUY'?'买入':'卖出'} ${o.qty}@${o.price.toFixed(3)}</span>`;
  if(!s.ops.length)h+='<span class="mut">记录区间内无操作</span>';
  h+=`</div><div class="tip" id="qtip_${c}"></div></div>`;}
 document.getElementById('qstocks').innerHTML=h||'<div class="mut">当前无持仓标的</div>';

 window.__drawQS=function(c,hover){const cv=document.getElementById('qc_'+c);if(!cv)return;
  cv.width=cv.clientWidth*2;cv.height=190*2;const ctx=cv.getContext('2d');
  const W=cv.width,H=cv.height,s=S[c],pad=40*2;
  ctx.clearRect(0,0,W,H);
  let mn=Math.min(...s.low),mx=Math.max(...s.high);
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
   if(op.side==='BUY'){ctx.fillStyle='#ff5c5c';ctx.moveTo(x,y+12);ctx.lineTo(x-8,y+26);ctx.lineTo(x+8,y+26);}
   else{ctx.fillStyle='#4bd94b';ctx.moveTo(x,y-12);ctx.lineTo(x-8,y-26);ctx.lineTo(x+8,y-26);}
   ctx.closePath();ctx.fill();}
  if(hover!=null&&hover>=0&&hover<s.days.length){        // 十字准线+当日收盘高亮
   const hx=X(hover),hy=Y(s.close_[hover]);
   ctx.strokeStyle='#8899aa';ctx.lineWidth=1;ctx.setLineDash([5,5]);
   ctx.beginPath();ctx.moveTo(hx,pad);ctx.lineTo(hx,H-pad);ctx.stroke();
   ctx.beginPath();ctx.moveTo(pad,hy);ctx.lineTo(W-pad,hy);ctx.stroke();
   ctx.setLineDash([]);ctx.fillStyle='#e8edf5';
   ctx.beginPath();ctx.arc(hx,hy,5,0,7);ctx.fill();}
  window.__qs_state[c]={pad:pad,W:W,n:s.days.length};};

 window.__qs_hide=function(c){const el=document.getElementById('qtip_'+c);
  if(el)el.style.display='none';window.__drawQS(c,null);};

 // 构造某日悬停浮层HTML(十字线定位/标签点击共用)
 window.__qs_tip=function(c,i){const s=S[c];
  const d=s.days[i],o=s.open[i],cl=s.close_[i],hi=s.high[i],lo=s.low[i];
  const ch=(s.chg&&s.chg[i]!=null)?s.chg[i]*100:null;
  const pc=(ch!=null&&ch>-100)?cl/(1+ch/100):o;
  const cls=(ch!=null&&ch<0)?'neg':'pos';
  let hh=`<div class="tt">${d}</div>`
   +`<div>开 ${o.toFixed(3)} · 收 <b class="${cls}">${cl.toFixed(3)}</b> · 涨跌 <b class="${cls}">${ch==null?'-':(ch>=0?'+':'')+ch.toFixed(2)+'%'}</b></div>`
   +`<div>最高 ${hi.toFixed(3)} · 最低 ${lo.toFixed(3)} · 振幅 ${(((hi-lo)/pc)*100).toFixed(2)}%</div>`;
  const v=s.vol?s.vol[i]:null,amt=s.amount?s.amount[i]:null;
  if(v)hh+=`<div class="mut">成交 ${(v/10000).toFixed(2)}万手${amt?` · ${(amt/1e8).toFixed(2)}亿`:''}</div>`;
  if(s.ma10&&s.ma10[i]!=null)hh+=`<div class="mut">MA5 ${s.ma5&&s.ma5[i]!=null?s.ma5[i].toFixed(3):'-'} · MA10 ${s.ma10[i].toFixed(3)} · MA20 ${s.ma20[i]==null?'-':s.ma20[i].toFixed(3)}</div>`;
  const dayops=(s.ops||[]).filter(p=>p.date===d);
  if(dayops.length)hh+='<div style="margin-top:4px;border-top:1px solid #2a3446;padding-top:4px">'
   +dayops.map(p=>`<b class="${p.side==='BUY'?'pos':'neg'}">${p.side==='BUY'?'买入':'卖出'}</b> ${p.time||'09:30'} 开盘 ${p.qty}股@${p.price.toFixed(3)} <span class="mut">${p.reason}</span>`).join('<br>')+'</div>';
  return hh;};
 // 在索引i处画十字线并显示浮层(坐标用视口clientX/clientY)
 window.__qs_showTip=function(c,i,x,y){const cv=document.getElementById('qc_'+c);
  const s=S[c];if(!cv||!s||i<0||i>=s.days.length)return;
  window.__drawQS(c,i);
  const el=document.getElementById('qtip_'+c);
  el.innerHTML=window.__qs_tip(c,i);el.style.display='block';
  const wrap=cv.parentElement.getBoundingClientRect();
  let lx=x-wrap.left+14,ly=y-wrap.top+14;
  if(lx+285>wrap.width)lx=Math.max(4,lx-300);
  if(ly+150>wrap.height)ly=Math.max(4,y-wrap.top-140);
  el.style.left=lx+'px';el.style.top=ly+'px';};
 // 点击下方操作标签: 按日期定位到对应K线
 window.__qs_showTipDate=function(c,date,x,y){
  const s=S[c];if(!s)return;
  const i=s.days.indexOf(date);window.__qs_showTip(c,i,x,y);};

 window.__qs_hover=function(c,ev){const cv=document.getElementById('qc_'+c);
  const st=window.__qs_state[c],s=S[c];if(!cv||!st)return;
  const rect=cv.getBoundingClientRect();
  const cx=(ev.clientX-rect.left)*(cv.width/rect.width);   // canvas为2倍缩放,需换算
  const i=Math.max(0,Math.min(st.n-1,Math.round((cx-st.pad)/(st.W-2*st.pad)*(st.n-1))));
  window.__qs_showTip(c,i,ev.clientX,ev.clientY);};

 for(const c in S){window.__drawQS(c,null);
  const cv=document.getElementById('qc_'+c);if(!cv)continue;
  cv.addEventListener('mousemove',e=>window.__qs_hover(c,e));
  cv.addEventListener('mouseleave',()=>window.__qs_hide(c));}
})();

// ---------- 实盘成交表 ----------
(function(){const F=Q.fills||[];
 let h='<tr><th>日期</th><th>股票</th><th>方向</th><th>数量</th><th>价格</th><th>金额</th><th>费用</th><th title="该笔成交所属策略腿: 趋势腿(趋势跟随, 收盘破MA10出场) / 超跌腿(超跌反弹, 中轨止盈)">腿</th><th>原因</th></tr>';
 for(const r of F)h+=`<tr><td>${r.date}</td><td>${r.name}(${r.code})</td>
  <td class="${r.side==='BUY'?'pos':'neg'}">${r.side==='BUY'?'买入':'卖出'}</td>
  <td>${r.qty}</td><td>${r.price.toFixed(3)}</td><td>${num(r.amount)}</td>
  <td>${r.fee.toFixed(2)}</td><td>${legCN(r.leg)}</td><td>${r.reason}</td></tr>`;
 if(!F.length)h+='<tr><td colspan="9" class="mut">当日无成交</td></tr>';
 document.getElementById('fills').innerHTML=`<table>${h}</table>`
  +`<div class="note"><b>腿</b>=该笔成交所属的策略腿: <b>趋势腿</b> 趋势跟随(收盘跌破MA10出场); <b>超跌腿</b> 超跌反弹(中轨止盈/破信号低点止损)。成交口径: 收盘出信号、<b>次日09:30开盘价</b>成交。</div>`;})();

// ---------- 实盘智能选股 ----------
(function(){const S2=Q.sel,R=Q.rebal||{};
 if(!S2){
  let why='';
  if(R.pick_enabled===false)why='智能选股未启用(<b>PICK_ENABLED=False</b>)';
  else{
   const lr=R.last_rebal||'—',iv=R.rebal_interval||'-',dl=R.days_left;
   let rule=`常规每 <b>${iv}</b> 个交易日调仓 · 持股目标 <b>${R.min_hold||3}~${R.max_hold||5}</b> 只 · 好机会可临时建仓`;
   if(R.n_hold!=null&&R.min_hold!=null&&R.n_hold<R.min_hold)
     rule+=` · <span class="pos">当前持股 ${R.n_hold} 只不足下限, 将提前调仓补仓</span>`;
   else if(dl!=null) rule+=` · 距下次约 <b>${dl}</b> 个交易日`;
   why=`非调仓日 · 上次调仓 <b>${lr||'暂无'}</b> · `+rule;}
  document.getElementById('sel').innerHTML=
   `<div class="mut">${why}　|　选股池现金 <b>${num(R.pool_cash||0)}</b> 元　|　卖出标的冷却 <b>${R.cooldown||'-'}</b> 个交易日禁再买</div>`;
  return;}
 let b='';
 if(S2.boards.length){b+='<table><tr><th>类别</th><th>热门板块</th><th>当日涨幅</th><th>20日动量</th><th>板块成交额</th></tr>';
  for(const x of S2.boards)b+=`<tr><td>${x.kind_cn}</td><td>${x.name}</td><td>${pctQ(x.pct)}</td>
   <td class="pos">${pct(x.mom*100)}</td><td>${(x.amount/1e8).toFixed(1)}亿</td></tr>`;
  b+='</table>';}
 const inv=c=>S2.invested.find(i=>i.code===c);
 if(S2.passing.length){b+='<table style="margin-top:10px"><tr><th>候选</th><th>所属板块</th><th>板块动量</th><th>个股20日动量</th><th>现价</th><th>处理</th></tr>';
  for(const p of S2.passing){const v=inv(p.code);
   b+=`<tr><td>${p.name}(${p.code})</td><td>${p.board}</td><td>${pct(p.board_mom*100)}</td>
    <td class="pos">${pct(p.mom*100)}</td><td>${p.close.toFixed(3)}</td>
    <td>${v?`<span class="pos">入选 ${num(v.alloc)}元</span>`:'<span class="mut">候选</span>'}</td></tr>`;}b+='</table>';}
 const sk=S2.skip;
 b+=`<div style="color:var(--mut);font-size:12px;margin-top:8px">调仓日 ${S2.rep_date}${S2.trig_txt?' · '+S2.trig_txt:''} |
  候选 ${S2.cand_n} 只(已剔除无权限板块) | 剔除: 无权限(科创/北交) <b>${sk['无权限板块']||0}</b> / ST ${sk['ST']} / 日K不足 ${sk['日K不足']} / 低价 ${sk['低价']} /
  流动性不足 ${sk['流动性不足']} / 行情失败 ${sk['行情失败']} / 趋势未达标 ${sk['趋势未达标']} /
  冷却中 ${sk['冷却中']}</div>`;
 document.getElementById('sel').innerHTML=b;})();

// ---------- 候选观察池(每日累积, 调仓日池内择优建仓) ----------
(function(){const PL=Q.obs_pool,el=document.getElementById('pool');
 if(!PL){el.innerHTML='<div class="mut">候选池为空(今日无过门槛候选或功能未启用)</div>';return;}
 let b=`<div class="mut" style="margin-bottom:6px">排序口径: `
  +(PL.zt_on?'<b style="color:var(--red)">连板行情 · 妖股/连板股优先</b>'
             :'非连板行情 · 极端标的沉底')
  +`; 趋势股规则不受影响 · 调仓日按池内择优实际建仓</div>`;
 if(PL.best&&PL.best.length)
  b+=`<div style="margin-bottom:8px">当前池内择优: <b class="pos">`
   +PL.best.map(x=>`${x.name}(${pct(x.mom*100)})`).join('、')+'</b></div>';
 if(PL.rows&&PL.rows.length){
  b+='<table><tr><th>候选(按动量)</th><th>所属板块</th><th>20日动量</th><th>现价</th><th title="入池首日收盘 -> 最近刷新日收盘">进池后涨幅</th><th>上榜</th><th>标记</th></tr>';
  for(const e of PL.rows){
   const ci=(e.chg_in==null)?'<span class="mut">-</span>'
    :`<span class="${e.chg_in>=0?'pos':'neg'}"><b>${e.chg_in>=0?'+':''}${e.chg_in.toFixed(1)}%</b></span>`
     +(e.in_days!=null?`<span class="mut"> ${e.in_days}日</span>`:'');
   b+=`<tr><td>${e.name}(${e.code})${e.held?' <span class="mut">[已持仓]</span>':''}</td>
    <td>${e.board}</td><td class="pos">${pct(e.mom*100)}</td><td>${e.close.toFixed(3)}</td>
    <td>${ci}</td>
    <td>${e.hits}</td><td>${e.extreme?'<span style="color:var(--amber)">⚠连板极端</span>':'<span class="mut">-</span>'}</td></tr>`;}
  b+='</table>';}
 el.innerHTML=b;})();

// ---------- 市场环境(信息层: 大盘均线+持仓板块, 仅辅助人工决策不参与自动买卖) ----------
(function(){const M=Q.menv,el=document.getElementById('menv');
 if(!M||(!M.idx&&!(M.rows||[]).length)){
  el.innerHTML='<div class="mut">市场环境数据不可用(指数/板块行情缺失), 不影响策略运行</div>';return;}
 let b='<div class="note" style="margin:0 0 8px">大盘与持仓<b>所属行业板块指数</b>的位置与动量, '
  +'仅供人工决策参考(2026-10-21 决策: 14组门控回测无一跑赢v2.5基准, 引擎不接入该维度)。'
  +'板块口径: 行业指数收盘 vs MA20 + 近5日动量。</div>';
 if(M.idx){const I=M.idx;
  const cell=v=>v==null?'<span class="mut">-</span>':
   `<span class="${I.close>=v?'pos':'neg'}">${I.close>=v?'▲':'▼'}${(I.close-v).toFixed(1)}</span>`;
  b+=`<table><tr><th>指数</th><th>收盘</th><th>MA10</th><th>MA20</th><th>MA60</th></tr>
   <tr><td><b>上证指数</b> <span class="mut">${I.date}</span></td><td><b>${I.close.toFixed(2)}</b></td>
   <td>${I.ma10==null?'-':I.ma10.toFixed(1)+' '+cell(I.ma10)}</td>
   <td>${I.ma20==null?'-':I.ma20.toFixed(1)+' '+cell(I.ma20)}</td>
   <td>${I.ma60==null?'-':I.ma60.toFixed(1)+' '+cell(I.ma60)}</td></tr></table>`;}
 if(M.rows&&M.rows.length){
  b+='<table style="margin-top:10px"><tr><th>股票</th><th>所属行业</th><th>板块代码</th><th>板块收盘</th><th>板块MA20</th><th>板块5日动量</th><th>板块状态</th></tr>';
  for(const r of M.rows){
   const st=r.above==null?'<span class="mut">-</span>':
    r.above?'<span class="pos">▲ 板块在MA20上方</span>':'<span class="neg">▼ 板块在MA20下方</span>';
   b+=`<tr><td>${r.name}(${r.code})</td><td>${r.industry||'-'}</td><td>${r.bk||'-'}</td>
    <td>${r.close==null?'-':r.close.toFixed(1)}</td>
    <td>${r.ma20==null?'-':r.ma20.toFixed(1)}</td>
    <td class="${r.mom5==null?'':clr(r.mom5)}">${r.mom5==null?'-':pct(r.mom5)}</td>
    <td>${st}</td></tr>`;}
  b+='</table>';}
 if(M.miss&&M.miss.length)
  b+=`<div class="note">⚠ 板块数据缺失: ${M.miss.join('、')}</div>`;
 el.innerHTML=b;})();

// ---------- 回测绘制 / 无回测时降级隐藏 ----------
if(B){drawBtEq();
 // 回测分层资金曲线: 悬停查看当日各层净值(重绘只画曲线, 不重建下方表格)
 const _N={bh:'等权持有',hold:'L1做T',trend:'L2趋势择时',rot:'L2动量轮动'};
 const _C={bh:'#8899aa',hold:'#3f7fd0',trend:'#f0a020',rot:'#b06bd6'};
 const _order=['bh','hold','trend','rot'].filter(k=>B.layers[k]);
 window.__drawBEq=function(hover){const cv=document.getElementById('beq');if(!cv)return;
  cv.width=cv.clientWidth*2;cv.height=220*2;const ctx=cv.getContext('2d');
  const W=cv.width,H=cv.height,pad=46*2;
  let mn=1e18,mx=-1e18;
  for(const k of _order)for(const v of B.layers[k].equity){mn=Math.min(mn,v);mx=Math.max(mx,v)}
  if(mn===mx){mn*=.99;mx*=1.01}
  const X=i=>pad+(W-2*pad)*i/(B.days.length-1),Y=v=>H-pad-(H-2*pad)*(v-mn)/(mx-mn);
  ctx.strokeStyle='#2a3446';ctx.lineWidth=2;ctx.beginPath();
  ctx.moveTo(pad,pad);ctx.lineTo(pad,H-pad);ctx.lineTo(W-pad,H-pad);ctx.stroke();
  for(const k of _order){ctx.strokeStyle=_C[k];ctx.lineWidth=2;
   ctx.beginPath();B.layers[k].equity.forEach((v,i)=>{i?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v))});ctx.stroke();}
  if(hover!=null&&hover>=0&&hover<B.days.length){
   const hx=X(hover);ctx.strokeStyle='#8899aa';ctx.lineWidth=1;ctx.setLineDash([5,5]);
   ctx.beginPath();ctx.moveTo(hx,pad);ctx.lineTo(hx,H-pad);ctx.stroke();ctx.setLineDash([]);}};
 attachHover(document.getElementById('beq'),i=>window.__drawBEq(i),
  cx=>idxOf(cx,46*2,document.getElementById('beq').width,B.days.length),
  i=>{let hh=`<div class="tt">${B.days[i]}</div>`;
   for(const k of _order){const e=B.layers[k].equity;
    hh+=`<div><span style="color:${_C[k]}">■</span> ${_N[k]} <b>${num(e[i])}</b> <span class="mut">(${pct(e[i]/e[0]-1)})</span></div>`;}
   return hh;});
}
else{document.querySelectorAll('.btsec').forEach(e=>e.style.display='none');}

window.addEventListener('resize',()=>{
 for(const c in Q.stocks)window.__drawQS&&window.__drawQS(c);
 if(B&&window.__drawPS)for(const c in B.per_stock)window.__drawPS(c);
 if(window.__drawQEq)window.__drawQEq(null);            // 权益曲线随窗口自适应重绘
 if(B&&window.__drawBEq)window.__drawBEq(null);         // 回测分层曲线同步重绘
});

document.getElementById('foot').innerHTML=
 `实盘: 每股独立子账户, 趋势腿满仓/超跌腿半仓, 收盘判定信号次日开盘价成交(无未来函数); 权益曲线自 ${Q.init_date} 起逐日记录; 只展示有持股的标的(流水见 quant_trades.csv).<br>`
 +(B?`回测: 严格无未来函数, 期初=${B.capital.toLocaleString()}元, 窗口 ${B.days[0]}~${B.days[B.days.length-1]}, 最优层全期 ${pct(B.layers[B.best].return)} (回撤${(B.layers[B.best].mdd*100).toFixed(1)}%); `
  :`回测: 本次回测数据不可用, 已降级为纯实盘页; `)
 +`费用口径: common/fees.py(佣金万2.5最低5 + 印花税卖出万5 + 过户费十万分之一). 数据: 东财日K klt=101 / 腾讯回退源.`;
</script></body></html>"""
