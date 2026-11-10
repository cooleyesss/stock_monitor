# -*- coding: utf-8 -*-
"""
集合竞价自动更新 t_plan (9:26 触发)
====================================
在主进程 9:26 时刻被调用, 读取昨日日线指标 + 当日竞价结果,
对 t_plan.csv 的做T区间做小幅动态调整, 写回同一文件,
runner 下一轮 run_cycle 自然会检测到 mtime 变化并加载新计划.

核心思路: 竞价 open 是今天多空首次定价, 反映隔夜消息/情绪.
  高开(open > 昨收+1.5%) -> 低吸区上移(不给追高), 止损上移(保护入场后浮盈)
  低开(open < 昨收-1.5%) -> 低吸区下移(给超跌后企稳空间), 止损下移(但不能低于原止损)
  跳空极宽(>=2ATR)      -> 取消今日低吸(避免追高/接飞刀), 清空 buy_low 字段
  平开(±1.5%内)          -> 保持原计划不动

所有调整都是小幅偏移(ATR × 系数), 不做剧烈重算; 原 t_plan 里的 BOLL/MA 参考位
在当日日K出来后(9:30后)自动对齐, 竞价阶段只处理"昨日收盘 → 今日open"这一个变量.
"""
import os
import sys
import math
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ttrade import config_ttrade as config
from common import quotes, notify
from ttrade import state as tstate
from core.indicators import build_ind

# 偏移系数: 高开/低开 → zone/止损的调整幅度(ATR 倍数)
_HIGH_OPEN_BUY_SHIFT = 0.3    # 高开: 低吸区整体上移 ATR×0.3
_HIGH_OPEN_STOP_SHIFT = 0.2   # 高开: 止损线上移 ATR×0.2 (保护入场后浮盈)
_LOW_OPEN_BUY_SHIFT = 0.3     # 低开: 低吸区整体下移 ATR×0.3
_LOW_OPEN_STOP_FLOOR = 0.5    # 低开: 止损线下移但不得低于"昨收 - ATR×0.5"

_GAP_FACTOR = 2.0             # 跳空阈值: |open - 昨收| >= ATR×2 → 取消今日低吸
_HIGH_OPEN_TH = 0.015         # 高开判定: open >= 昨收 × 1.015
_LOW_OPEN_TH = -0.015         # 低开判定: open <= 昨收 × 0.985


def _load_last_close_atr(code):
    """昨日收盘价 + ATR (从日K缓存取, 不请求接口)"""
    try:
        bars = quotes.fetch_kline_bars(code, klt=101, lmt=60)
        if len(bars) < 21:
            return None, None
        ind = build_ind(bars)
        i = len(bars) - 1
        return bars[-1]["close"], ind["atr"][i]
    except Exception:
        return None, None


def _load_auction_open(code):
    """9:26 时刻拉竞价开盘价 (fetch_quotes 的 f2 即最新价, 9:25 后就是竞价结果)
    盘前/竞价未撮合时 f2=0.0 视为无效返回 None(防误判跳空极宽)"""
    try:
        q = quotes.fetch_quotes([code]).get(code)
        return q["price"] if q and q.get("price") else None
    except Exception:
        return None


def _shift_plan_row(row, code, name, dry=False):
    """对一只票的计划行做竞价调整, 返回 (新行dict, 变更说明list); 无变更返回 (row, [])"""
    last_close, atr = _load_last_close_atr(code)
    open_px = _load_auction_open(code)
    notes = []
    if last_close is None or atr is None or open_px is None or atr <= 0:
        return row, notes
    gap = open_px - last_close
    gap_ratio = gap / last_close
    # --- 跳空极宽: 取消今日低吸 ---
    if abs(gap) >= atr * _GAP_FACTOR:
        notes.append(f"竞价跳空{gap:+.2f}(≥ATR×{_GAP_FACTOR}), 今日低吸区置空避免追高/接飞刀")
        row = dict(row)
        row["buy_low"] = None
        row["buy_high"] = None
        return row, notes
    # --- 高开: 低吸区上移 + 止损上移 ---
    if gap_ratio >= _HIGH_OPEN_TH:
        shift = atr * _HIGH_OPEN_BUY_SHIFT
        if row.get("buy_low") is not None:
            row = dict(row)
            row["buy_low"] = round(row["buy_low"] + shift, 2)
            row["buy_high"] = round(row["buy_high"] + shift, 2)
            notes.append(f"高开{gap_ratio*100:+.1f}% 低吸区上移{shift:.2f}")
        if row.get("stop_loss"):
            cur_stop = row["stop_loss"][0] if isinstance(row["stop_loss"], list) else row["stop_loss"]
            new_stop = round(cur_stop + atr * _HIGH_OPEN_STOP_SHIFT, 2)
            new_stop = max(new_stop, row.get("buy_low") or 0, row.get("sell_low") or 0)
            row["stop_loss"] = [new_stop]
            notes.append(f"高开 止损线上移至{new_stop}")
    # --- 低开: 低吸区下移 + 止损线下移(但有地板) ---
    elif gap_ratio <= _LOW_OPEN_TH:
        shift = atr * _LOW_OPEN_BUY_SHIFT
        if row.get("buy_low") is not None:
            row = dict(row)
            row["buy_low"] = round(row["buy_low"] - shift, 2)
            row["buy_high"] = round(row["buy_high"] - shift, 2)
            notes.append(f"低开{gap_ratio*100:+.1f}% 低吸区下移{shift:.2f}")
        if row.get("stop_loss"):
            floor = round(last_close - atr * _LOW_OPEN_STOP_FLOOR, 2)
            cur_stop = row["stop_loss"][0] if isinstance(row["stop_loss"], list) else row["stop_loss"]
            new_stop = max(floor, cur_stop)   # 只上不下
            if new_stop != cur_stop:
                row["stop_loss"] = [new_stop]
                notes.append(f"低开 止损线上限保护(昨收-0.5ATR={floor}) 维持{new_stop}")
    row["_last_close"] = last_close
    row["_open"] = open_px
    row["_gap"] = round(gap_ratio * 100, 2)
    return row, notes


def _plan_to_rows(plan):
    """把 tstate.load_t_plan() 返回的 plan dict 转为有序 list[dict]"""
    rows = []
    for code, p in sorted(plan.items()):
        r = {"code": code, "name": p.get("name", code),
             "buy_low": p.get("buy_low"), "buy_high": p.get("buy_high"),
             "buy_vr_max": p.get("buy_vr_max"),
             "sell_low": p.get("sell_low"), "sell_high": p.get("sell_high"),
             "sell_vr_min": p.get("sell_vr_min"),
             "stop_loss": (p.get("stop_loss") or [None])[0],
             "min_spread": p.get("min_spread"),
             "t_qty": p.get("t_qty")}
        rows.append(r)
    return rows


def _rows_to_plan_file(rows):
    """list[dict] -> t_plan.csv (覆盖写, 含竞价变更说明列)"""
    import csv
    path = config.T_PLAN_CSV
    backup = path + ".bak"
    try:
        if os.path.exists(path):
            import shutil
            shutil.copy2(path, backup)
    except Exception:
        pass
    headers = ["代码", "名称", "T类型", "低吸下限", "低吸上限", "低吸量比上限",
               "高抛下限", "高抛上限", "高抛量比下限", "止损线",
               "单次T价差下限", "T量", "说明"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            def _v(k): return "" if r.get(k) is None else r[k]
            w.writerow([
                _v("code"), _v("name"), _v("T类型") or "",
                _v("buy_low"), _v("buy_high"), _v("buy_vr_max"),
                _v("sell_low"), _v("sell_high"), _v("sell_vr_min"),
                _v("stop_loss"), _v("min_spread"), _v("t_qty"),
                _v("说明"),
            ])


def auto_update_tplan(now=None, dry=False):
    """9:26 竞价自动更新入口: 读现有计划 → 按竞价调整 → 写回 → 推送变更摘要"""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:
        return []
    plan = tstate.load_t_plan()
    if not plan:
        return []
    orig_rows = _plan_to_rows(plan)
    new_rows, all_notes = [], []
    updated = 0
    for r in orig_rows:
        code = r["code"]
        nr, notes = _shift_plan_row(r, code, r["name"], dry=dry)
        if notes:
            updated += 1
            nr["说明"] = (nr.get("说明") or "") + " | 竞价调整(" + ",".join(notes) + ")"
        all_notes.extend([(code, r["name"], n) for n in notes])
        new_rows.append(nr)
    if updated == 0:
        print(f"[竞价更新 {now.strftime('%H:%M:%S')}] {len(new_rows)}只票竞价在±1.5%内, 计划不变")
        return []
    if dry:
        print(f"[DRY] 竞价更新 {updated}/{len(new_rows)}只:")
        for c, n, note in all_notes:
            print(f"  {c} {n}: {note}")
        return all_notes
    _rows_to_plan_file(new_rows)
    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(config.T_PLAN_CSV))
    print(f"[竞价更新 {mtime.strftime('%H:%M:%S')}] {updated}/{len(new_rows)}只调整, 已写回 t_plan.csv")
    # 推送变更摘要
    lines = [f"9:26 竞价自动更新 · {updated}/{len(new_rows)}只调整"]
    for c, n, note in all_notes:
        lines.append(f"  • {c} {n}: {note}")
    try:
        notify.send("🔄 9:26竞价 · t_plan 自动更新", "\n".join(lines))
    except Exception as e:
        print("[竞价更新] 推送失败:", e)
    return all_notes


if __name__ == "__main__":
    dry = "--test" in sys.argv or "--dry" in sys.argv
    auto_update_tplan(dry=dry)
