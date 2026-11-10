# -*- coding: utf-8 -*-
"""量化盘中双窗口择机执行runner(2026-10-26 用户需求: 量化操作时间改造)
常驻进程(start.ps1 9:25拉起, 与T侧监控并行), 流程:
  9:25起   建当日计划(竞价确认): 遍历有pending信号的子账户 -> SELL算深跳空线,
           BUY_SCALP校验竞价空间(开盘<need确认位即取消), 推送"今日操作计划"
  9:30~10:00 早盘窗口(每QUANT_TICK_SECONDS盯价):
           SELL=反抽至分时VWAP卖 / 跌破深跳空线(昨收×(1+QUANT_GAP_SELL))立即卖
           BUY =回踩至分时VWAP且守住need确认位(无need则仅看VWAP)买入
  14:30    纪律重估: SELL现价收回level上方取消 / BUY跌破need确认位取消 /
           超时信号(无level)不取消; 推送尾盘验证结果
  14:30~14:55 尾盘窗口: 同早盘规则继续择机
  14:55    兜底: SELL全部现价强制卖出; BUY需need为None或现价>=need, 否则取消
  15:00后  自行退出(收盘日报由15:15 stop.ps1触发的quant_daily.py负责)
协调机制: state["day_plan"][date][code]为"当日已处理"唯一标记(filled/cancelled),
  15:15收盘引擎(engine._day_plan_pick)见filled/cancelled跳过开盘价补成交;
  runner未跑/中途崩溃(day_plan缺失或仍pending)时引擎按开盘价兜底(原行为, 降级安全)
簿记归属: runner对盘中成交做全套簿记(apply_fill+cost重算/清仓cost归零+last_sell
  +cum_fee+append_trade+save_state); 引擎对filled仅fills.append展示+兜底登记(幂等)
恢复: 崩溃重启从state["day_plan"][today]恢复续跑(planned/revalued/forced幂等标记);
  dry干跑: python quant_runner.py --dry --now "HH:MM" 指定时刻单轮演练(不写盘/不推送)"""
import datetime
import sys
import time as _time

import config_quant as config
from common import notify, quotes
from core import adaptive
from quant import account

_ACT_CN = {"SELL": "🔴 卖出", "BUY_TREND": "🟢 趋势买入",
           "BUY_SCALP": "🟢 超跌买入", "BUY_ADD": "🟢 加仓"}


def _is_st(name):
    """死规则(与engine._is_st同源): 一切ST股买入排除(SELL不受限)"""
    return "ST" in (name or "").upper()


def _hhmm(dt):
    return dt.strftime("%H:%M")


def _sim_now(arg):
    """--now "HH:MM" 覆写当前时刻(日期仍为真实今日, 供干跑演练各时点)"""
    h, m = arg.split(":")
    real = datetime.datetime.now()
    return real.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def _meta(state, today):
    return state.setdefault("day_plan", {}).setdefault(today, {}) \
                .setdefault("_meta", {})


def _dp_row(state, today, code):
    return (state.get("day_plan") or {}).get(today, {}).get(code)


def _dp_status(state, today, code):
    row = _dp_row(state, today, code)
    return (row or {}).get("status") or ""


def _dp_set(state, today, code, act, status, dry=False, **kw):
    """day_plan状态变更即落盘(崩溃恢复点); dry模式不写盘"""
    d = state.setdefault("day_plan", {}).setdefault(today, {})
    row = d.get(code) or {"act": act, "date": today}
    row["act"] = act
    row["status"] = status
    row.update(kw)
    row["ts"] = datetime.datetime.now().strftime("%H:%M:%S")
    d[code] = row
    account.save_state(state, dry=dry)


def _last_close(code, today):
    """昨收: 日K剔除今日形成中bar(9:25竞价阶段接口可能带今日竞价bar)"""
    try:
        bars = quotes.fetch_kline_bars(code, klt=101, lmt=10)
        bars = [b for b in bars if str(b["dt"])[:10] != today] or bars
        return bars[-1]["close"] if bars else None
    except Exception:
        return None


def _quotes_px(codes):
    """实时价(fetch_quotes f2); 失败返回空dict(本轮跳过, 下轮重试)"""
    if not codes:
        return {}
    try:
        qs = quotes.fetch_quotes(codes)
        return {c: (q or {}).get("price") or 0.0 for c, q in (qs or {}).items()}
    except Exception as e:
        print("[runner] 实时行情获取失败:", e)
        return {}


def _vwap(code, today, dry=False):
    """当日分时均价VWAP = sum(amount)/sum(vol×100) (5分钟K, 不走缓存);
    当日无成交根返回None(tick自动降级: SELL靠gap_line/兜底, BUY靠兜底)"""
    try:
        bars = quotes.fetch_kline_bars(code, klt=config.QUANT_VWAP_KLT, lmt=80)
        if not bars:
            return None
        d = today if not dry else str(bars[-1]["dt"])[:10]   # dry非交易日回放最近根
        bars = [b for b in bars if str(b["dt"])[:10] == d and (b.get("vol") or 0) > 0]
        if not bars:
            return None
        amt = sum(b.get("amount") or 0.0 for b in bars)
        vol = sum(b.get("vol") or 0.0 for b in bars) * 100
        return round(amt / vol, 4) if vol > 0 else None
    except Exception:
        return None


# ---------------- ① 9:25 建计划(竞价确认) ----------------

def _build_items(state, today, dry=False):
    """从pending构建当日计划items; ST只拦BUY(死规则), SELL算gap_line,
    BUY_SCALP竞价空间校验(open<need即取消, 与engine _cancel_reason同口径)"""
    items = []
    for code in sorted(state["accounts"]):
        acct = state["accounts"][code]
        p = acct.get("pending")
        if not p:
            continue
        act = p["act"]
        name = acct["name"]
        if act.startswith("BUY") and _is_st(name):
            account.append_log(f"{today} 竞价确认: {name}({code}) ST股买入硬拦截, "
                               f"{act}信号取消(死规则)", dry=dry)
            acct["pending"] = None
            _dp_set(state, today, code, act, "cancelled", dry=dry,
                    note="ST股买入硬拦截(死规则)")
            continue
        it = {"code": code, "name": name, "act": act,
              "reason": p.get("reason", ""), "level": p.get("level"),
              "need": p.get("need"), "sig_low": p.get("sig_low"),
              "sh": acct["sh"], "cash": acct["cash"]}
        it["last_close"] = _last_close(code, today)
        px = _quotes_px([code]).get(code)
        it["open"] = px
        if act == "SELL" and it["last_close"]:
            it["gap_line"] = round(it["last_close"] * (1 + config.QUANT_GAP_SELL), 4)
        if act.startswith("BUY") and p.get("need") is not None \
                and px is not None and px > 0 and px < p["need"]:
            account.append_log(f"{today} 竞价确认: {name}({code}) 开盘{px:.3f}"
                               f"<确认位{p['need']:.2f}, {act}信号取消",
                               dry=dry)
            acct["pending"] = None
            _dp_set(state, today, code, act, "cancelled", dry=dry,
                    note=f"竞价开盘{px:.3f}<确认位{p['need']:.2f}")
            continue
        items.append(it)
    return items


def _plan_rule_line(it):
    """计划行: 标的+动作+择机规则摘要"""
    act = it["act"]
    if act == "SELL":
        gap = f", 破{it['gap_line']:.2f}立即卖" if it.get("gap_line") else ""
        lv = f"(信号位{it['level']:.2f})" if it.get("level") is not None else ""
        return f"{_ACT_CN.get(act, act)} {it['name']}({it['code']}){lv}: " \
               f"反抽分时VWAP卖{gap}, 未触发14:55兜底"
    need = f"、守确认位{it['need']:.2f}" if it.get("need") is not None else ""
    return f"{_ACT_CN.get(act, act)} {it['name']}({it['code']}): " \
           f"回踩分时VWAP买{need}, 未触发14:55兜底"


def _ensure_plan(state, today, now, dry):
    """建当日计划(幂等): day_plan已有今日pending项则恢复续跑; 首建推送操作计划。
    返回items(可能为空=无信号); 全无行情判休市返回None"""
    meta = _meta(state, today)
    if not meta.get("planned"):
        items = _build_items(state, today, dry=dry)
        if not items:
            # 无pending信号或全被竞价取消: 也落planned标记避免重复建
            meta["planned"] = True
            account.save_state(state, dry=dry)
            _push_plan(items, today, dry)
            return items
        meta["planned"] = True
        for it in items:    # 预登记pending行(崩溃恢复/引擎可见)
            if _dp_status(state, today, it["code"]) != "pending":
                _dp_set(state, today, it["code"], it["act"], "pending", dry=dry,
                        level=it.get("level"), need=it.get("need"),
                        gap_line=it.get("gap_line"), reason=it["reason"])
        account.save_state(state, dry=dry)
        _push_plan(items, today, dry)
        return items
    # 恢复续跑: 从day_plan重建items(仅pending), pending账户状态以磁盘state为准
    items = []
    for code, row in (state.get("day_plan") or {}).get(today, {}).items():
        if code == "_meta" or row.get("status") != "pending":
            continue
        acct = state["accounts"].get(code)
        if not acct:
            continue
        it = {"code": code, "name": acct["name"], "act": row.get("act"),
              "reason": row.get("reason", ""), "level": row.get("level"),
              "need": row.get("need"), "gap_line": row.get("gap_line"),
              "sh": acct["sh"], "cash": acct["cash"],
              "last_close": _last_close(code, today), "open": None}
        items.append(it)
    return items


def _push_plan(items, today, dry):
    """9:26 推送今日操作计划(无信号也推, 明确告知)"""
    lines = [f"📅 量化今日操作计划 {today}",
             f"窗口: 早盘{config.QUANT_WIN1[0]}~{config.QUANT_WIN1[1]} / "
             f"尾盘{config.QUANT_WIN2[0]}~{config.QUANT_WIN2[1]}(14:55兜底)"]
    if items:
        for it in items:
            lines.append("• " + _plan_rule_line(it))
    else:
        lines.append("今日无待执行信号(空仓候选/无退出触发)")
    body = "\n".join(lines)
    if dry:
        print(f"[DRY] 推送计划:\n{body}")
        return
    try:
        notify.send("📋 量化 9:26 今日操作计划", body)
    except Exception as e:
        print("[runner] 计划推送失败:", e)


# ---------------- ② 窗口tick择机 + ③ 成交簿记 ----------------

def _fill(state, it, px, today, dry, via, note):
    """统一成交入口: apply_fill(实际价) + 全套簿记 + day_plan落盘。
    apply_fill返回None(不足一手/现金不足)则取消信号"""
    acct = state["accounts"][it["code"]]
    plan = {"act": it["act"], "reason": it["reason"], "need": it.get("need"),
            "sig_low": it.get("sig_low"), "o_checked": False}
    rec = adaptive.apply_fill(acct, it["code"], it["name"], today, px, plan,
                              check_open=False)
    if not rec:
        acct["pending"] = None
        _dp_set(state, today, it["code"], it["act"], "cancelled", dry=dry,
                note=f"{via}放弃: 数量/资金不足一手")
        account.append_log(f"{today} {via} 取消 {it['name']}({it['code']}) "
                           f"{it['act']}: 数量或资金不足一手", dry=dry)
        print(f"[runner] {via} 放弃 {it['name']}({it['code']}) {it['act']}: "
              f"数量或资金不足一手")
        return None
    old_sh = acct["sh"] - rec["qty"] if rec["side"] == "BUY" else 0
    if rec["side"] == "BUY" and acct["sh"] > 0:
        acct["cost"] = round((acct.get("cost", 0.0) * old_sh + rec["amount"]
                              + rec["fee"]) / acct["sh"], 4)
    elif rec["side"] == "SELL" and acct["sh"] == 0:
        acct["cost"] = 0.0
        state.setdefault("last_sell", {})[it["code"]] = today
    state["cum_fee"] = state.get("cum_fee", 0.0) + rec["fee"]
    acct["pending"] = None
    _dp_set(state, today, it["code"], it["act"], "filled", dry=dry, rec=rec,
            price=px, note=f"{via}: {note}")
    account.append_trade(rec, acct["cash"], dry=dry, via=via)
    print(f"[runner] {via} {rec['side']} {it['name']}({it['code']}) "
          f"{rec['qty']}股@{px:.3f} {note}")
    return rec


def _cancel(state, it, today, dry, note):
    acct = state["accounts"][it["code"]]
    acct["pending"] = None
    _dp_set(state, today, it["code"], it["act"], "cancelled", dry=dry, note=note)
    account.append_log(f"{today} 取消 {it['name']}({it['code']}) {it['act']}: {note}",
                       dry=dry)
    print(f"[runner] 取消 {it['name']}({it['code']}) {it['act']}: {note}")


def _tick(state, items, today, now, dry, tag):
    """窗口择机一轮: SELL=px>=vwap反抽卖/px<=gap_line跳空卖;
    BUY=px<=vwap回踩买且(need为None或px>=need守住确认位)"""
    pend = [it for it in items
            if _dp_status(state, today, it["code"]) == "pending"]
    if not pend:
        return
    px_map = _quotes_px([it["code"] for it in pend])
    hit = False
    for it in pend:
        px = px_map.get(it["code"]) or 0.0
        if px <= 0:
            continue
        vwap = _vwap(it["code"], today, dry)
        act = it["act"]
        if act == "SELL":
            if it.get("gap_line") and px <= it["gap_line"]:
                _fill(state, it, px, today, dry, via=f"{tag}择机",
                      note=f"深跳空破线{it['gap_line']:.2f}立即卖")
                hit = True
            elif vwap and px >= vwap:
                _fill(state, it, px, today, dry, via=f"{tag}择机",
                      note=f"反抽至分时VWAP{vwap:.2f}")
                hit = True
        elif act.startswith("BUY"):
            if vwap and px <= vwap and (it.get("need") is None
                                        or px >= it["need"]):
                _fill(state, it, px, today, dry, via=f"{tag}择机",
                      note=f"回踩分时VWAP{vwap:.2f}买入"
                           + (f"(守确认位{it['need']:.2f})"
                              if it.get("need") is not None else ""))
                hit = True
    if not hit:
        left = [f"{it['name']}" for it in items
                if _dp_status(state, today, it["code"]) == "pending"]
        print(f"[runner] {tag} {_hhmm(now)} 盯价中, 待触发: {left or '无'}")


# ---------------- ④ 14:30 纪律重估 ----------------

def _revalue(state, items, today, now, dry):
    """尾盘验证(纪律重估): SELL现价收回level上方取消 / BUY跌破need确认位取消 /
    超时(无level)不取消; 对仍pending的信号继续执行并推送验证结果"""
    pend = [it for it in items
            if _dp_status(state, today, it["code"]) == "pending"]
    px_map = _quotes_px([it["code"] for it in pend]) if pend else {}
    lines = [f"📉 量化尾盘验证 14:30 · {today}"]
    for it in items:
        st = _dp_status(state, today, it["code"])
        label = f"{_ACT_CN.get(it['act'], it['act'])} {it['name']}({it['code']})"
        if st == "filled":
            lines.append(f"✔ {label}: 窗口内已成交({_dp_row(state, today, it['code']).get('note') or ''})")
            continue
        if st == "cancelled":
            lines.append(f"✖ {label}: 已取消({_dp_row(state, today, it['code']).get('note') or ''})")
            continue
        px = px_map.get(it["code"]) or 0.0
        act = it["act"]
        if act == "SELL" and it.get("level") is not None and px > it["level"]:
            _cancel(state, it, today, dry,
                    f"现价{px:.2f}收回信号位{it['level']:.2f}上方, 纪律重估取消")
            lines.append(f"✖ {label}: 现价{px:.2f}收回{it['level']:.2f}上方, 取消卖出")
        elif act.startswith("BUY") and it.get("need") is not None \
                and px > 0 and px < it["need"]:
            _cancel(state, it, today, dry,
                    f"现价{px:.2f}跌破确认位{it['need']:.2f}, 纪律重估取消")
            lines.append(f"✖ {label}: 现价{px:.2f}跌破确认位{it['need']:.2f}, 取消买入")
        else:
            lines.append(f"▶ {label}: 继续执行, 现价{px:.2f}, 14:55兜底")
    body = "\n".join(lines)
    if dry:
        print(f"[DRY] 尾盘验证:\n{body}")
        return
    try:
        notify.send("📉 量化 14:30 尾盘验证", body)
    except Exception as e:
        print("[runner] 验证推送失败:", e)


# ---------------- ⑤ 14:55 兜底 ----------------

def _force(state, items, today, now, dry):
    """尾盘兜底: SELL全部现价强制卖出(纪律优先); BUY需need为None或现价>=need,
    否则取消(确认位破位不追); 无行情保留pending交引擎15:15开盘价兜底"""
    pend = [it for it in items
            if _dp_status(state, today, it["code"]) == "pending"]
    if not pend:
        return
    px_map = _quotes_px([it["code"] for it in pend])
    for it in pend:
        px = px_map.get(it["code"]) or 0.0
        if it["act"] == "SELL":
            if px > 0:
                _fill(state, it, px, today, dry, via="尾盘兜底",
                      note="14:55现价强制卖出")
            else:
                print(f"[runner] 兜底无行情 {it['name']}, 保留信号交引擎兜底")
        elif it["act"].startswith("BUY"):
            if px > 0:
                if it.get("need") is None or px >= it["need"]:
                    _fill(state, it, px, today, dry, via="尾盘兜底",
                          note="14:55现价兜底买入")
                else:
                    _cancel(state, it, today, dry,
                            f"兜底现价{px:.2f}跌破确认位{it['need']:.2f}")
            else:
                print(f"[runner] 兜底无行情 {it['name']}, 保留信号交引擎兜底")


# ---------------- 主驱动 ----------------

def _in_win(t, win):
    return win[0] <= t <= win[1]


def _drive(state, items, today, now, dry):
    """执行"当前时刻"应做的阶段(常驻循环每轮调用/dry单轮调用)"""
    t = _hhmm(now)
    meta = _meta(state, today)
    if _in_win(t, config.QUANT_WIN1):
        _tick(state, items, today, now, dry, "早盘窗口")
    if t >= "14:30" and t < "15:00" and not meta.get("revalued") \
            and t < config.QUANT_FORCE_TIME:
        _revalue(state, items, today, now, dry)
        meta["revalued"] = True
        account.save_state(state, dry=dry)
    if _in_win(t, config.QUANT_WIN2):
        _tick(state, items, today, now, dry, "尾盘窗口")
    if t >= config.QUANT_FORCE_TIME and t < "15:00" and not meta.get("forced"):
        _force(state, items, today, now, dry)
        meta["forced"] = True
        account.save_state(state, dry=dry)


def _sleep_to(now, target_hhmm, cap=600):
    """睡到目标时刻(上限cap秒), 供窗口外空转节能"""
    h, m = target_hhmm.split(":")
    tgt = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    dt = (tgt - now).total_seconds()
    if dt > 0:
        _time.sleep(min(dt, cap))


def main():
    dry = "--dry" in sys.argv
    now_arg = None
    if "--now" in sys.argv:
        now_arg = sys.argv[sys.argv.index("--now") + 1]
    if dry:
        notify.DRY = True
    real_now = datetime.datetime.now()
    if not (dry and now_arg) and real_now.weekday() >= 5:
        print("[runner] 周末休市, 退出")
        return 0
    state = account.load_state()
    if not state:
        print("[runner] 量化状态不存在(未初始化), 退出")
        return 1
    today = real_now.strftime("%Y-%m-%d")
    print(f"[runner] 盘中双窗口启动 {real_now.strftime('%Y-%m-%d %H:%M:%S')}"
          f"{' [DRY]' if dry else ''}")
    now = _sim_now(now_arg) if now_arg else real_now
    # ① 建计划(实盘等到9:25竞价结束; dry+--now直接建)
    if not now_arg:
        while _hhmm(datetime.datetime.now()) < "09:25":
            _time.sleep(10)
        now = datetime.datetime.now()
    items = _ensure_plan(state, today, now, dry)
    if items is None:
        return 0
    if dry and now_arg:                     # 单轮演练: 执行该时点阶段即退出
        _drive(state, items, today, now, dry)
        print("[DRY] 单轮演练完成")
        return 0
    # 常驻主循环
    while True:
        now = datetime.datetime.now()
        t = _hhmm(now)
        _drive(state, items, today, now, dry)
        if t >= "15:00":
            print("[runner] 今日盘中任务完成, 退出(日报由15:15 quant_daily负责)")
            return 0
        if _in_win(t, config.QUANT_WIN1) or _in_win(t, config.QUANT_WIN2):
            _time.sleep(config.QUANT_TICK_SECONDS)
        elif t < config.QUANT_WIN1[0]:
            _sleep_to(now, config.QUANT_WIN1[0], cap=60)
        elif t < "14:30":
            _sleep_to(now, "14:30", cap=600)
        else:
            _time.sleep(15)


if __name__ == "__main__":
    sys.exit(main() or 0)
