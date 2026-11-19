# -*- coding: utf-8 -*-
"""SVM周频策略实验驱动: 2年数据 + 烧入训练 + 参数网格扫优 + prod同窗口对比
设计对齐用户原脚本("先用历史训练, 之后再交易"): 拉取2年日线(2024-11~2026-10),
  烧入期(2026-04-01之前)只积累训练样本不交易, 之后walk-forward周频交易;
  交易窗口 == 现有回测窗口(2026-04-01~2026-10-13), 与prod基线严格同窗对比
用法(项目根目录): python lab/run_ml.py [--html]
扫优原则: 全局参数, 不逐股调参; 数据与生产回测同源(东财日线)"""
import argparse
import itertools
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest.daily import BT_CAPITAL
from lab.base import load_universe, replay_stock
from lab.strategies import REGISTRY
from lab.strategies.ml_svm import MlSvmStrategy

# 当前 quant + ttrade 持股(母表口径, 600803为quant子账户遗留)
CODES = ["600557", "300207", "600480", "300455",
         "600879", "603169", "002009", "600803"]
BEG2, END2 = "20241101", "20261013"      # 2年数据窗口
BURN = "2026-04-01"                      # 烧入截止(交易窗口起点=标准回测窗口)
ML_CACHE = r"D:\trae_projects\_bt_tmp\ml_daily_cache.json"
HOSTS = ["https://push2his.eastmoney.com", "https://33.push2his.eastmoney.com"]

GRID = {
    "C": [0.5, 1.0, 2.0],
    "GAMMA": ["auto", "scale"],
    "TP": [1.05, 1.10],
    "SL": [0.95, 0.98],
    "SL_ANY": [False, True],
    "STD": [True, False],
    "CW": ["balanced", None],
}


def _fetch(code, beg, end):
    if code == "999999":
        secid = "1.000001"              # 上证指数在东财的secid
    else:
        secid = ("1." if code[0] in "56" else "0.") + code
    params = {"secid": secid, "fields1": "f1,f2,f3",
              "fields2": "f51,f52,f53,f54,f55,f56,f57",
              "klt": 101, "fqt": 1, "beg": beg, "end": end}
    for _ in range(12):
        try:
            r = requests.get(HOSTS[0] + "/api/qt/stock/kline/get",
                             params=params, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"})
            k = (r.json().get("data") or {}).get("klines") or []
            if k:
                bars = []
                for line in k:
                    f = line.split(",")
                    bars.append({"dt": f[0], "open": float(f[1]),
                                 "close": float(f[2]), "high": float(f[3]),
                                 "low": float(f[4]), "vol": float(f[5]),
                                 "amount": float(f[6])})
                return bars
        except Exception:
            pass
        time.sleep(3)
    return []


def ensure_ml_universe():
    """2年扩展缓存(独立文件, 不动 bt_daily_cache.json); 网络偶发断连自动重试"""
    d = None
    if os.path.exists(ML_CACHE):
        try:
            d = json.load(open(ML_CACHE, encoding="utf-8"))
            if d.get("beg") == BEG2 and d.get("end") == END2 \
                    and all(c in d["data"] for c in CODES + ["999999"]):
                return d["data"]
        except Exception:
            d = None
    d = d or {"beg": BEG2, "end": END2, "data": {}}
    data = d["data"]
    names = {"600557": "康缘药业", "300207": "欣旺达", "600480": "凌云股份",
             "300455": "航天智装", "600879": "航天电子", "603169": "兰石重装",
             "002009": "天奇股份", "600803": "新奥股份", "999999": "上证指数"}
    for code in CODES + ["999999"]:
        if code in data and data[code].get("bars"):
            continue
        bars = _fetch(code, BEG2, END2)
        assert bars, f"日线获取失败: {code}"
        data[code] = {"name": names[code], "bars": bars}
        print(f"  [ml] {code} {names[code]} {bars[0]['dt']}~{bars[-1]['dt']}"
              f" n={len(bars)}")
    json.dump({"beg": BEG2, "end": END2, "data": data},
              open(ML_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return data


def sweep(uni):
    keys = ["C", "GAMMA", "TP", "SL", "SL_ANY", "STD", "CW"]
    combos = list(itertools.product(*(GRID[k] for k in keys)))
    rows = []
    for ci, vals in enumerate(combos):
        s = MlSvmStrategy()
        s.BURN_DATE = BURN
        for k, v in zip(keys, vals):
            setattr(s, k, v)
        rs = [replay_stock(uni, c, BT_CAPITAL, s) for c in CODES]
        avg = sum(r["return"] for r in rs) / len(rs)
        mdd = sum(r["mdd"] for r in rs) / len(rs)
        tr = sum(r["trades"] for r in rs)
        rows.append({"avg": avg, "mdd": mdd, "trades": tr,
                     **{k: v for k, v in zip(keys, vals)}, "rs": rs})
        if (ci + 1) % 12 == 0:
            print(f"  [扫优进度] {ci + 1}/{len(combos)} "
                  f"当前最好 {max(x['avg'] for x in rows) * 100:+.2f}%",
                  flush=True)
    rows.sort(key=lambda x: -x["avg"])
    hdr = (f"{'C':>4} {'gamma':>6} {'TP':>5} {'SL':>5} {'SL任意':>6} "
           f"{'标准化':>6} {'均衡':>6} {'平均收益':>9} {'平均回撤':>8} {'总笔数':>6}")
    print("[扫优] " + hdr)
    print("-" * len(hdr))
    for r in rows[:15]:
        print(f"{r['C']:>4} {r['GAMMA']:>6} {r['TP']:>5.2f} {r['SL']:>5.2f}"
              f"{'是' if r['SL_ANY'] else '否':>7}"
              f"{'是' if r['STD'] else '否':>7}"
              f"{'是' if r['CW'] else '否':>7}"
              f"{r['avg'] * 100:>+8.2f}%{r['mdd'] * 100:>7.1f}%{r['trades']:>6}")
    print(f"[扫优] 共{len(rows)}组合; 最差 {rows[-1]['avg'] * 100:+.2f}%")
    return rows[0]


def main():
    ap = argparse.ArgumentParser(prog="run_ml")
    ap.add_argument("--html", action="store_true", help="生成dashboard_lab.html")
    ap.add_argument("--capital", type=float, default=BT_CAPITAL)
    a = ap.parse_args()
    print(f"[ml] 标的{len(CODES)}只 | 期初{a.capital:,.2f}元 | 2年数据 "
          f"烧入至{BURN}后交易(交易窗=标准回测窗) | SVM扩张窗口walk-forward")
    print("[ml] 拉取2年扩展数据...")
    ext = ensure_ml_universe()

    # 参考基准: 交易窗口内买入持有(等权)
    bh = []
    for c in CODES:
        bars = [b for b in ext[c]["bars"] if b["dt"] >= BURN]
        bh.append(bars[-1]["close"] / bars[0]["close"] - 1 if bars else 0)
    print(f"[ml] 参考: 同窗买入持有等权 {sum(bh) / len(bh) * 100:+.2f}%")

    best = sweep(ext)

    cls = REGISTRY["ml_svm"]
    for k in ("C", "GAMMA", "TP", "SL", "SL_ANY", "STD", "CW"):
        setattr(cls, k, best[k])
    cls.BURN_DATE = BURN
    print(f"\n[ml] 最优参数 C={best['C']} gamma={best['GAMMA']} TP={best['TP']} "
          f"SL={best['SL']}(任意日={best['SL_ANY']}) 标准化={best['STD']} "
          f"均衡权重={best['CW']} -> prod(标准缓存窗) vs ml_svm(2年数据同窗) 对比:")
    from lab import runner
    from lab.strategies.prod import ProdStrategy
    uni_std = load_universe()
    names = ["prod", "ml_svm"]
    results = {
        "prod": {c: replay_stock(uni_std, c, a.capital, ProdStrategy())
                 for c in CODES},
        "ml_svm": {c: replay_stock(ext, c, a.capital, cls())
                   for c in CODES},
    }
    runner.print_compare(names, results, CODES)
    if a.html:
        p = runner.build_html(names, results, CODES, a.capital, runner.REPORT)
        print(f"[ml] HTML报告 -> {p}")
    print("[ml] 实验与生产隔离; 采纳需人工确认后走晋升流程")
    return 0


if __name__ == "__main__":
    sys.exit(main())
