# -*- coding: utf-8 -*-
"""量化独立账户: 状态/流水/日志读写 + 从量化子系统持仓CSV(data/quant/holdings.csv)初始化
每股一个独立子账户(与回测per_stock口径一致): 初始cash=0+快照持股(趋势腿),
买入资金只来自本账户卖出回款; 自由现金(两账户可用)仅展示不参与买入
持仓来源 = config_common.QT_HOLDINGS_CSV(由母表 mystock_test.csv 一键分发), 本子系统不读写母表"""
import csv
import json
import os
import config_quant as config

STATE_PATH = os.path.join(config.QT_DATA_DIR, "quant_state.json")
TRADES_CSV = os.path.join(config.QT_DATA_DIR, "quant_trades.csv")
TRADES_LOG = os.path.join(config.QT_DATA_DIR, "quant_trades.log")
CSV_HEADERS = ["date", "code", "name", "side", "qty", "price", "amount",
               "fee", "reason", "leg", "cash_after"]


def _money(s):
    s = (s or "").strip().replace("约", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def init_state():
    """从量化子系统持仓CSV初始化: 股票行->子账户(真实代码键, ETF别名经ETF_CODE_MAP映射);
    '可用'行->free_cash; '合计'行->baseline_total; init_date取最新'更新日期'
    剔除名单(QT_EXCLUDE_CODES, 2026-11-05): 命中品种所在券商的资金行(可用/总资产)
    一并排除, 保证基准/选股池与账本口径一致(ZZ1000剔除后基准=中信总资产)"""
    rows = list(csv.DictReader(open(config.QT_HOLDINGS_CSV, encoding="utf-8-sig")))
    excl_brokers = {r.get("券商", "").strip() for r in rows
                    if (r.get("股票名称") or "").strip() in getattr(config, "QT_EXCLUDE_CODES", set())}
    excl_brokers.discard("")
    accounts, free_cash, baseline = {}, 0.0, None
    excl_amount = 0.0   # 被剔除券商的总资产额(从合计基准中扣除)
    init_date = ""
    for row in rows:
            name = (row.get("股票名称") or "").strip()
            code = (row.get("代码") or "").strip()
            broker = (row.get("券商") or "").strip()
            if not name:
                continue
            if "现金" in name or "可用" in name:
                if broker in excl_brokers:
                    continue   # 剔除券商的可用不进选股池(口径与账本一致)
                v = _money(row.get("市值"))
                if v:
                    free_cash += v
                continue
            if broker == "合计" or ("合计" in name and "总资产" in name):
                baseline = _money(row.get("市值"))   # 两账户总资产行
                continue
            if "总资产" in name or "合计" in name:
                if broker in excl_brokers:
                    excl_amount += _money(row.get("市值")) or 0.0
                continue
            if name in getattr(config, "QT_EXCLUDE_CODES", set()):
                print(f"[量化初始化] {name} 在剔除名单, 跳过(不入账本/不监控)")
                continue
            shares = (row.get("持股") or "").strip()
            if not shares.isdigit() or int(shares) <= 0:
                continue
            real = code if (len(code) == 6 and code.isdigit()) else \
                config.ETF_CODE_MAP.get(name, "")
            if not (len(real) == 6 and real.isdigit()):
                print(f"[量化初始化] {name}(代码'{code}')无法映射真实代码, 跳过")
                continue
            d = (row.get("更新日期") or "").strip()
            if d > init_date:
                init_date = d
            accounts[real] = {
                "name": name, "cash": 0.0, "sh": int(shares),
                "cost": _money(row.get("成本")) or 0.0,
                "px": _money(row.get("现价")) or 0.0,   # 最近收盘价(节点故障时估值回退)
                "sh_tr": True, "sig_low": 0.0, "hold_d": 0, "cool": 0,
                "pending": None, "last_date": None}
    if not accounts:
        raise RuntimeError("持仓CSV无有效股票行, 无法初始化量化账户")
    if baseline is not None and excl_amount:
        baseline = round(baseline - excl_amount, 2)   # 基准扣除被剔除券商部分
    import datetime
    state = {"version": 1,
             "init_date": init_date or datetime.datetime.now().strftime("%Y-%m-%d"),
             "baseline_total": baseline, "free_cash": round(free_cash, 2),
             "pool_cash": round(free_cash, 2),   # 选股池期初=两账户可用现金(闲钱入池)
             "cum_fee": 0.0, "accounts": accounts}
    print(f"量化账户初始化: {len(accounts)}只标的, 基准{state['init_date']} "
          f"总资产{baseline}, 选股池期初{free_cash:.2f}")
    return state


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print("量化状态文件损坏:", e)
    return None


def save_state(state, dry=False):
    if dry:
        return
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def append_trade(rec, cash_after, dry=False, via="开盘"):
    """成交流水CSV一行 + 可读日志一行; via=成交时点描述(开盘/窗口择机/尾盘兜底等)"""
    if dry or not rec:
        return
    new = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", encoding="utf-8-sig" if new else "utf-8",
              newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(CSV_HEADERS)
        w.writerow([rec["date"], rec["code"], rec["name"], rec["side"],
                    rec["qty"], f"{rec['price']:.3f}", f"{rec['amount']:.2f}",
                    f"{rec['fee']:.2f}", rec["reason"], rec["leg"],
                    f"{cash_after:.2f}"])
    side_cn = "买入" if rec["side"] == "BUY" else "卖出"
    append_log(f"{rec['date']} {via} {side_cn} {rec['name']}({rec['code']}) "
               f"{rec['qty']}股@{rec['price']:.3f} 金额{rec['amount']:.2f} "
               f"费用{rec['fee']:.2f} [{rec['reason']}|{rec['leg']}腿] "
               f"子账户现金{cash_after:.2f}", dry=dry)


def append_log(text, dry=False):
    if dry or not text:
        return
    with open(TRADES_LOG, "a", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")


# ---------------- 手动观察池(用户在holdings_editor维护, 2026-10-20) ----------------
WATCHLIST_HEADERS = ["代码", "名称", "备注"]


def load_watchlist():
    """读取手动观察池 -> list[{code,name,note}]; 无文件/空文件返回[]
    代码必须6位数字(ETF别名经ETF_CODE_MAP映射), 非法行跳过"""
    out = []
    if not os.path.exists(config.WATCHLIST_CSV):
        return out
    with open(config.WATCHLIST_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = (row.get("代码") or "").strip()
            name = (row.get("名称") or "").strip()
            note = (row.get("备注") or "").strip()
            if not code and not name:
                continue
            real = code if (len(code) == 6 and code.isdigit()) else \
                config.ETF_CODE_MAP.get(name, "")
            if not (len(real) == 6 and real.isdigit()):
                print(f"[观察池] {name}(代码'{code}')无法映射真实代码, 跳过")
                continue
            out.append({"code": real, "name": name or real, "note": note})
    return out


def save_watchlist(rows):
    """覆盖写观察池(先备份.bak); rows=[{code,name,note}]"""
    if os.path.exists(config.WATCHLIST_CSV):
        try:
            with open(config.WATCHLIST_CSV, encoding="utf-8-sig") as f:
                bak = f.read()
            with open(config.WATCHLIST_CSV + ".bak", "w", encoding="utf-8") as f:
                f.write(bak)
        except OSError as e:
            print(f"[观察池] 备份失败: {e}")
    with open(config.WATCHLIST_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(WATCHLIST_HEADERS)
        for r in rows:
            w.writerow([r.get("code", ""), r.get("name", ""), r.get("note", "")])
