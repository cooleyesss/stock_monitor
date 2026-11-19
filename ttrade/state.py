# -*- coding: utf-8 -*-
"""T 子系统状态/计划层: 做T模拟盘状态文件 + 做T计划 + 成交流水/日志 读写

持仓来源 = config_common.TT_HOLDINGS_CSV(data/ttrade/holdings.csv, 由母表 mystock_test.csv
一键分发而来), 本子系统任何时候都不读/写母表。
计划与金额解析的通用工具在 common/textparse.py; 做T盈亏纯函数在 ttrade/ledger.py。
"""
import csv
import os
import json
import datetime
from ttrade import config_ttrade as config
from common.textparse import today_str, _parse_money, _f
from ttrade.ledger import t_pnl

STATE_PATH = config.STATE_PATH   # 做T模拟盘状态文件(data/ttrade/sim_state.json)


# ---------------- 模拟盘状态 ----------------
def init_state():
    """从 T 子系统持仓CSV初始化模拟盘(两券商合并为一个账户)"""
    cash = 0.0
    positions = {}
    with open(config.TT_HOLDINGS_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("股票名称") or "").strip()
            code = (row.get("代码") or "").strip()
            if not name or "现金" in name or "可用" in name:
                v = _parse_money(row.get("市值") or "")
                if v:
                    cash += v
                continue
            if "总资产" in name or "合计" in name or code == "合计":
                continue
            shares = (row.get("持股") or "").strip()
            if not shares or not shares.isdigit() or int(shares) <= 0:
                continue                                  # 含0股"已清仓"快照行
            shares = int(shares)
            if code in config.T_EXCLUDE_CODES or name in config.T_EXCLUDE_CODES:
                print(f"[剔除] {name}({code or '--'}) 在T剔除名单, 不入模拟盘")
                continue
            cost = _parse_money(row.get("成本") or "")
            # orig_cost=整体累计成本基准(2026-11-03用户需求): 清仓再接回/做T摊薄都不改变此基准,
            # 浮动盈亏按它计算; cost仍为模拟盘摊薄成本
            pos = {"name": name, "shares": shares,
                   "cost": cost if cost else 0.0,
                   "orig_cost": cost if cost else 0.0}
            if len(code) == 6 and code.isdigit():
                positions[code] = pos                     # 普通股票
            else:
                # 代码为'--'的品种(如ZZ1000), 用名称作键, 记录静态现价
                sp = _parse_money(row.get("现价") or "")
                if sp:
                    pos["static_price"] = sp
                positions[name] = pos
    if config.INITIAL_CASH_OVERRIDE is not None:
        cash = float(config.INITIAL_CASH_OVERRIDE)
    state = {
        "version": 1,
        "init_date": today_str(),
        "baseline": None,            # 期初基准总资产(首次取到实时行情时记录)
        "cash": round(cash, 2),
        "positions": positions,
        "today": today_str(),
        "today_lot": {},             # 每股当日做T股数缓存
        "today_trades": [],          # 今日成交明细
        "today_skips": [],           # 今日信号但未执行记录(现金不足等)
        "today_ledger": {},          # 今日做T台账 {code: {bq,ba,sq,sa}}
        "cum_ledger": {},            # 累计做T台账
        "today_fee": 0.0,
        "cum_fee": 0.0,
        "daily_history": [],         # 每日收盘小结
        "eod_sent": "",
        "stop_done": {},             # 已执行止损的股 {code: date}
        "stop_pending": {},          # 跳空豁免挂起 {code: date}(待收盘确认)
        "armed": [],                 # 防重复触发 "code|buy" / "code|sell"
        "today_ai": {"asked": 0, "execute": 0, "wait": 0, "fail": 0},
        "today_vote_log": [],        # 量化投票过线信号(供EOD策略统计)
        "last_prices": {},           # 最近一次行情价(收盘报告兜底用)
        "vote_fail_time": {},        # 投票未共振上次推送时间 {f"code|side": datetime}
        "vote_fail_streak": {},      # 同方向连续投票失败计数 {f"code|side": int}
    }
    print(f"模拟盘初始化完成: 持仓{len(positions)}只, 现金{cash:,.2f}")
    return state


def load_state():
    if os.path.exists(config.STATE_PATH):
        try:
            with open(config.STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
            state.setdefault("today_ai", {"asked": 0, "execute": 0, "wait": 0, "fail": 0})
            state.setdefault("touch_ai", {})
            state.setdefault("t_hist", {})
            state.setdefault("zone_since", {})
            state.setdefault("today_vote_log", [])
            # 剔除名单清理(2026-10-30): 兼容已建状态中存在被剔除品种(如ZZ1000),
            # 从持仓/台账移除并同步扣减期初基准, 保证"较期初"口径连续
            exc = getattr(config, "T_EXCLUDE_CODES", set())
            dropped = [k for k in list(state.get("positions", {})) if k in exc]
            if dropped:
                lp = state.get("last_prices") or {}
                for k in dropped:
                    pos = state["positions"].pop(k)
                    val = pos["shares"] * (lp.get(k) or pos.get("static_price")
                                           or pos.get("cost") or 0)
                    if state.get("baseline") and state["baseline"].get("total_asset"):
                        state["baseline"]["total_asset"] = round(
                            state["baseline"]["total_asset"] - val, 2)
                    state.get("cum_ledger", {}).pop(k, None)
                    state.get("today_ledger", {}).pop(k, None)
                    print(f"[剔除] {pos.get('name', k)} 已从T模拟盘移除"
                          f"(估值{val:,.2f}, 期初基准同步扣减)")
            # 基准成本迁移(2026-11-03): 老状态补orig_cost, 优先取持仓CSV真实成本,
            # 兜底当前摊薄cost(一次性, 之后execute_buy/sell不再改动orig_cost)
            if any("orig_cost" not in p for p in state.get("positions", {}).values()):
                basis = {}
                try:
                    with open(config.TT_HOLDINGS_CSV, encoding="utf-8-sig") as f:
                        for row in csv.DictReader(f):
                            c = (row.get("代码") or "").strip()
                            v = _parse_money(row.get("成本") or "")
                            if len(c) == 6 and c.isdigit() and v:
                                basis[c] = v
                except FileNotFoundError:
                    pass
                for c, p in state.get("positions", {}).items():
                    if "orig_cost" not in p:
                        p["orig_cost"] = basis.get(c) or p.get("cost") or 0.0
                        print(f"[基准成本] {p.get('name', c)} orig_cost="
                              f"{p['orig_cost']}(整体累计口径)")
            state.setdefault("cleared_cost", {})   # 清仓时暂存基准成本(接回时恢复)
            # v2→v3策略改名迁移(2026-11-17): 老状态v2_*字段重命名为v3_*
            # (v2_carry为正T隔日腿, 跨日保留, 必须迁移; 其余日内字段为陈旧残留, 直接清理)
            _carry = state.pop("v2_carry", None)
            if _carry and not state.get("v3_carry"):
                state["v3_carry"] = _carry
                print("[迁移] v2_carry -> v3_carry(正T隔日腿跨日保留)")
            for _k in ("v2_env", "v2_legs", "v2_last_open", "v2_idle"):
                state.pop(_k, None)
            return state
        except Exception as e:
            print("状态文件损坏, 重新初始化:", e)
    return None


def save_state(state, dry=False):
    if dry:
        return
    tmp = config.STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, default=str)  # default=str兜底datetime等不可序列化对象, 防"Object of type datetime is not JSON serializable"反复失败丢簿记
    os.replace(tmp, config.STATE_PATH)


def rollover(state, now, dry=False):
    """新交易日: 归档昨日数据并重置日内状态"""
    td = today_str(now)
    if state["today"] == td:
        return
    yd = state["today"]
    # 昨日收盘报告漏发(进程昨晚不在) -> 用最后行情价补记历史(不补推送)
    if state["today_trades"] and state.get("eod_sent") != yd:
        prices = state.get("last_prices", {})
        gross = sum(t_pnl(l, prices.get(c, 0)) for c, l in
                    state["today_ledger"].items())
        state["daily_history"].append({
            "date": yd, "n_trades": len(state["today_trades"]),
            "t_pnl_gross": round(gross, 2), "fees": state["today_fee"],
            "t_pnl_net": round(gross - state["today_fee"], 2),
            "total_asset": None, "account_pnl": None})
        print(f"[补记] {yd} 收盘报告漏发, 已补记历史")
    state["today"] = td
    state["today_lot"] = {}
    state["today_trades"] = []
    state["today_skips"] = []
    state["today_ledger"] = {}
    state["today_fee"] = 0.0
    state["stop_done"] = {}
    state["stop_pending"] = {}   # 跳空豁免挂起(待收盘确认), 每日重置
    state["armed"] = []
    state["today_ai"] = {"asked": 0, "execute": 0, "wait": 0, "fail": 0}
    state["touch_ai"] = {}   # 到位提醒AI结论缓存(去重), 每日重置
    state["t_hist"] = {}     # 做T成交时间(限频), 每日重置
    state["zone_since"] = {} # 低吸区进入时间(超时豁免), 每日重置
    state["today_vote_log"] = []   # 量化投票信号日志, 每日重置
    state["vote_fail_time"] = {}   # 投票未共振推送冷却, 每日重置
    state["vote_fail_streak"] = {} # 连续失败计数, 每日重置
    # v3策略(反T编排): 当日环境/腿/冷却重置; 正T隔日腿v3_carry跨日保留(次日开盘出场)
    state.pop("v3_env", None)
    for _c, _lg in (state.pop("v3_legs", {}) or {}).items():
        if isinstance(_lg, dict) and _lg.get("qty", 0) > 0:
            print(f"[v3] {_lg.get('name', _c)} 未回补卖出腿残留{_lg['qty']}股"
                  f"(14:45兜底未成交, 多为现金不足), 残腿放弃回补")
    state.pop("v3_last_open", None)
    state.pop("v3_idle", None)   # v3未成交原因记录(2026-11-11), 每日重置
    for pos in state["positions"].values():
        pos.pop("today_bought", None)   # T+1: 隔夜今仓变昨仓
    save_state(state, dry)
    print(f"[新交易日] {td} 日内状态已重置")


# ---------------- 做T计划 ----------------
def load_t_plan():
    """读取做T计划(每轮重读); 表头名驱动解析, 自动适配列顺序变化
    支持列: 代码,名称,[T类型],低吸下限,低吸上限,低吸量比上限,
            高抛下限,高抛上限,高抛量比下限,止损线,[单次T价差下限],[T量],[说明]
    """
    plan = {}
    try:
        with open(config.T_PLAN_CSV, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    except FileNotFoundError:
        print("未找到做T计划文件:", config.T_PLAN_CSV)
        return plan
    if not rows:
        return plan
    head = [h.strip() for h in rows[0]]
    idx = {h: i for i, h in enumerate(head)}
    header_mode = "代码" in idx   # 新表头结构(2026-10起含T类型/T量等列)

    def _g(fields, col):
        i = idx.get(col)
        return fields[i] if i is not None and i < len(fields) else ""

    for fields in rows[1:]:
        fields = [x.strip() for x in fields]
        if header_mode:
            code = _g(fields, "代码")
            name = _g(fields, "名称") or code
            bl = _f(_g(fields, "低吸下限"))
            bh = _f(_g(fields, "低吸上限"))
            bv = _f(_g(fields, "低吸量比上限"))
            sl = _f(_g(fields, "高抛下限"))
            sh = _f(_g(fields, "高抛上限"))
            sv = _f(_g(fields, "高抛量比下限"))
            stop = _f(_g(fields, "止损线"))
            spread = _f(_g(fields, "单次T价差下限"))
            tq = _f(_g(fields, "T量"))
        else:
            # 旧结构回退: 位置解析(代码,名称,低吸下限..止损线 9列)
            if len(fields) < 2 or len(fields[0]) != 6 or not fields[0].isdigit():
                continue
            code, name = fields[0], fields[1] or fields[0]
            rest = (fields[2:] + [""] * 7)[:7]
            bl, bh, bv = _f(rest[0]), _f(rest[1]), _f(rest[2])
            sl, sh, sv = _f(rest[3]), _f(rest[4]), _f(rest[5])
            stop, spread, tq = _f(rest[6]), None, None
        if len(code) != 6 or not code.isdigit():
            continue
        p = {"name": name}
        if bl is not None:
            p["buy_low"] = bl
            p["buy_high"] = bh if bh is not None else bl
            p["buy_vr_max"] = bv
        if sl is not None:
            p["sell_low"] = sl
            p["sell_high"] = sh if sh is not None else sl
            p["sell_vr_min"] = sv
        if stop is not None:
            p["stop_loss"] = [stop]
        if tq is not None and tq > 0:
            p["t_qty"] = int(tq // 100 * 100)   # T量(百股取整)
        if spread is not None and spread > 0:
            p["min_spread"] = spread            # 单次T价差下限(供AI参考)
        # 低吸量比上限全局覆盖(全局规则, 不逐股调参): 由 config.T_BUY_VR_MAX 统一指定
        if "buy_low" in p and config.T_BUY_VR_MAX is not None:
            p["buy_vr_max"] = config.T_BUY_VR_MAX
        if len(p) > 1:
            plan[code] = p
    return plan


# ---------------- 成交流水 ----------------
def append_trades_csv(rec, headers, dry=False):
    """追加成交流水; 表头结构变化时自动迁移旧文件(缺失列补空后重写)"""
    if dry:
        return
    need_header = True
    if os.path.exists(config.TRADES_CSV):
        try:
            with open(config.TRADES_CSV, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
        except OSError:
            rows = []
        if rows:
            need_header = False
            if rows[0] != headers:
                old_n = len(rows[0])
                for r in rows:
                    r += [""] * (len(headers) - len(r))
                rows[0] = headers
                tmp = config.TRADES_CSV + ".tmp"
                with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
                    csv.writer(f).writerows(rows)
                os.replace(tmp, config.TRADES_CSV)
                print(f"[流水迁移] sim_trades.csv表头 {old_n}列→{len(headers)}列, "
                      f"历史行缺失列已补空")
    with open(config.TRADES_CSV, "a",
              encoding="utf-8-sig" if need_header else "utf-8", newline="") as f:
        w = csv.writer(f)
        if need_header:
            w.writerow(headers)
        w.writerow(rec)


def append_trade_log(text, dry=False):
    """追加成交/收盘记录到成交日志 trades.log(可读格式)"""
    if dry or not text:
        return
    with open(config.TRADES_LOG, "a", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")
