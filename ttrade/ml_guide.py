# -*- coding: utf-8 -*-
"""v3 SVM日频方向指导(2026-11-13周频上线, 2026-11-16改日频+新闻特征)

机制(闸门语义与bt_ttrade.py --ml回测档同口径): 每交易日9:00用"截至昨日收盘"的
3个月日线 + 隔夜新闻分数重训SVM(全局参数 C=0.5/rbf/gamma=auto, 不标准化,
9因子=7价格因子+2新闻因子, 标签=5个交易日后收盘更高?), 预测指导当日做T方向:
  预测涨(1) -> 今日以正T为主: 禁反T卖出腿(防卖飞, 保上行)
  预测跌(0) -> 今日以反T为主: 禁正T买入+禁网格回补(不接飞刀, 反T照卖),
              未回补腿由14:45收盘兜底闭合
周频版回测(7只持仓, 2026-09-24~11-13): 基线+2,103 vs 指导+6,696(3.2倍, 夏普0.22->0.44);
日频版将隔夜新闻分(news_radar 8:45汇总)纳入特征, 无记录=0退化为纯价格特征(失败安全)。

生成时机: 交易日9:00主循环自动重训当日预测(幂等, 8:45新闻汇总先生成)。
文件: data/ttrade/ml_day.json = {"date":"2026-11-17","generated":"...",
       "preds":{code:{pred,bars,until,trained,news}}}
回退: config.V3_ML_EN=False 即关闭SVM指导(仅基础编排); 文件缺失/日期不匹配=无指导, 按基础编排跑(失败安全)。
"""
import datetime
import json
import os

from ttrade import config_ttrade as config
from common import notify
from common import quotes
from ttrade import news_radar

_ML_PATH = os.path.join(config.TT_DATA_DIR, "ml_day.json")
_CACHE = {"mtime": None, "data": None}


# ---------------- 特征/训练(价格因子与bt_ttrade._ml_feats同源) ----------------
def _feats(bars, i, nf=(0.0, 0), n=15):
    """截至第i根(含)的n根日K -> 9因子 = 7价格因子 + (隔夜新闻分, 新闻条数)"""
    import numpy as np
    w = bars[i - n + 1:i + 1]
    close = [b["close"] for b in w]
    high = [b["high"] for b in w]
    low = [b["low"] for b in w]
    vol = [b["vol"] for b in w]
    mean = lambda xs: sum(xs) / len(xs)
    return [close[-1] / mean(close),
            vol[-1] / (mean(vol) or 1.0),
            high[-1] / mean(high),
            low[-1] / mean(low),
            float(vol[-1]),
            close[-1] / close[0],
            float(np.std(close)),
            float(nf[0]), float(nf[1])]


def _train(hist, code, nzh=None):
    """在hist(严格历史)上训练SVM; 样本i取[14, len-6], 标签close[i+5]>close[i];
    样本i的新闻特征=截至bars[i]当日(含)已生成的隔夜汇总(8:45早于收盘, 无前视)"""
    if len(hist) < 30:
        return None
    import numpy as np
    from sklearn import svm as _svm
    xs, ys = [], []
    for i in range(14, len(hist) - 5):
        nf = (news_radar.score_at(code, hist[i]["dt"][:10], hist=nzh)
              if nzh is not None else (0.0, 0))
        xs.append(_feats(hist, i, nf))
        ys.append(1 if hist[i + 5]["close"] > hist[i]["close"] else 0)
    if len(xs) < 10 or len(set(ys)) < 2:
        return None
    try:
        return _svm.SVC(C=0.5, kernel="rbf", gamma="auto").fit(
            np.array(xs), ys)
    except Exception:
        return None


def _predict(clf, hist, nf=(0.0, 0)):
    import numpy as np
    if clf is None or len(hist) < 15:
        return None
    try:
        return int(clf.predict(np.array([_feats(hist, len(hist) - 1, nf)]))[0])
    except Exception:
        return None


# ---------------- 日预测读写 ----------------
def _load():
    try:
        m = os.path.getmtime(_ML_PATH)
    except OSError:
        return {}
    if _CACHE["mtime"] != m:
        try:
            with open(_ML_PATH, encoding="utf-8") as f:
                _CACHE["data"] = json.load(f)
            _CACHE["mtime"] = m
        except Exception:
            return {}
    return _CACHE["data"] or {}


def day_pred(code, td):
    """(code, td)当日的方向指导: 1=预测涨(正T为主) / 0=预测跌(反T为主) /
    None=当日无预测(数据不足/文件缺失/日期不匹配 -> 按基础编排跑, 失败安全)"""
    d = _load()
    if d.get("date") != str(td)[:10]:
        return None
    p = (d.get("preds") or {}).get(code)
    return p.get("pred") if isinstance(p, dict) else None


def _codes_of(state, tplan):
    from ttrade.runner import collect_codes      # 局部导入避免循环
    return collect_codes(state, tplan)


def _notify_preds(rec, names):
    """重训落盘后微信推送当日方向预测(涨/跌分组+闸门指导)"""
    ups, dns = [], []
    for c, v in sorted(rec["preds"].items()):
        nm = f"{names.get(c, '')}({c})"
        (ups if v.get("pred") == 1 else dns).append(nm)
    until = max((v.get("until", "") for v in rec["preds"].values()), default="")
    notify.send(
        f"🤖 v3 SVM日频方向预测·已生成 ({rec['date']})",
        "<br>".join([
            f"📊 训练数据: 每只3个月日线(截至{until}收盘)+隔夜新闻分, 无未来函数",
            notify.colored(notify.bold("📈 预测涨·今日正T为主(禁反T卖出腿): ")
                           + ("、".join(ups) or "无"), config.C_RED),
            notify.colored(notify.bold("📉 预测跌·今日反T为主(禁正T买入+禁网格回补): ")
                           + ("、".join(dns) or "无"), config.C_GREEN),
            notify.colored("🗓 明日9:00自动重训; 预测缺失的股票按基础编排跑(无SVM指导)",
                           config.C_GRAY)]))


def train_day(state, tplan, now, dry=False):
    """交易日9:00入口: 重训当日方向预测并落盘(幂等: 文件date==今日跳过)
    训练数据 = 3个月日线(dt严格早于今日) + 隔夜新闻分(news_history, 8:45已生成)"""
    if not getattr(config, "V3_ML_EN", False):
        return
    td = now.strftime("%Y-%m-%d")
    if _load().get("date") == td:
        return                          # 今日已训, 幂等
    names = news_radar.names_of(state, tplan)
    nzh = news_radar.load_history()     # 隔夜新闻汇总档(训练与预测共用一份)
    preds = {}
    for code in sorted(_codes_of(state, tplan)):
        try:
            bars = quotes._fetch_kline_raw(code, 101, config.V3_ML_DAYS)
        except Exception as e:
            print(f"[ML日预测] {code} 日线获取失败跳过:", e)
            continue
        hist = [b for b in bars if str(b["dt"])[:10] < td]
        clf = _train(hist, code, nzh)
        nf_today = news_radar.score_at(code, td, hist=nzh)   # 今日8:45隔夜分
        p = _predict(clf, hist, nf_today)
        if p is None:
            print(f"[ML日预测] {code} 训练数据不足(仅{len(hist)}根), 今日无指导")
            continue
        preds[code] = {"pred": p, "bars": len(hist),
                       "until": str(hist[-1]["dt"])[:10],
                       "news": list(nf_today),
                       "trained": now.strftime("%Y-%m-%d %H:%M")}
        print(f"[ML日预测] {code} 数据{len(hist)}根(截至{str(hist[-1]['dt'])[:10]})"
              f" 隔夜新闻分{nf_today[0]:+.0f}"
              f" -> 预测{'涨(今日正T为主)' if p else '跌(今日反T为主)'}")
    if not preds:
        return
    if dry:
        print(f"[DRY] ML日预测({td})未落盘")
        return
    rec = {"date": td, "generated": now.strftime("%Y-%m-%d %H:%M"),
           "preds": preds}
    os.makedirs(config.TT_DATA_DIR, exist_ok=True)
    with open(_ML_PATH, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    _CACHE["mtime"] = None                     # 失效内存缓存
    n_dn = sum(1 for v in preds.values() if v["pred"] == 0)
    print(f"[ML日预测] 已落盘 {_ML_PATH}: {len(preds)}只"
          f"(预测跌{n_dn}/涨{len(preds) - n_dn}), 今日生效")
    try:
        _notify_preds(rec, names)              # 微信推送当日方向预测
    except Exception as e:
        print("[ML日预测] 推送失败(不影响落盘):", e)
