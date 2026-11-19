# -*- coding: utf-8 -*-
"""实验策略: SVM周频机器学习(移植自用户 lab/strategies/机器学习回测实例.py, QMT平台脚本)
原策略语义:
  过去15个交易日的7因子 -> SVM(rbf)预测5个交易日后涨跌;
  每周一(空仓时)预测, 预测涨则以周一开盘价买入95%资金;
  出场: 任意日收盘较买入价 +10% 止盈 / 周五收盘 -2% 止损
lab框架对齐(无未来函数, 全局统一口径):
  入场: 原策略"周一开盘买、特征数据截至上周五" == 周五收盘decide出信号、
        下个交易日(周一)开盘成交 —— 信息集完全一致, 语义无损移植;
  出场: 收盘判定条件(浮盈/浮亏+周五) -> 次日开盘价成交(框架统一, 原策略按当日收盘价平仓);
  训练: 扩张窗口walk-forward —— 每次预测前仅用截至当前收盘的样本重训(样本: 窗口尾i,
        标签 close[i+H]>close[i]), 比原脚本"2016年一次性训练"更适应近期市场, 且严格无未来函数
参数(全局统一, 不逐股调参): C/gamma/TP止盈/SL止损/FRAC仓位比例"""
import datetime

import numpy as np
from sklearn import svm as _svm
from sklearn.preprocessing import StandardScaler

from lab.base import LabStrategy


def _weekday(dt):
    return datetime.date(int(dt[:4]), int(dt[5:7]), int(dt[8:10])).isoweekday()


def _features(bars, i, n):
    """截至第i根(含)的n根日K -> 7因子(与原脚本一致)"""
    w = bars[i - n + 1:i + 1]
    close = np.array([b["close"] for b in w])
    high = np.array([b["high"] for b in w])
    low = np.array([b["low"] for b in w])
    vol = np.array([b["vol"] for b in w])
    return [close[-1] / np.mean(close),
            vol[-1] / (np.mean(vol) or 1.0),
            high[-1] / np.mean(high),
            low[-1] / np.mean(low),
            float(vol[-1]),
            close[-1] / close[0],
            float(np.std(close))]


class MlSvmStrategy(LabStrategy):
    name = "ml_svm"
    desc = ("SVM周频(移植自用户脚本): 15日7因子扩张窗口walk-forward重训, "
            "周五收盘预测5日后涨 -> 下个交易日开盘95%仓; "
            "任意日收盘+TP%止盈 / 周五收盘-SL%止损, 次日开盘成交")

    C = 1.0
    GAMMA = "auto"
    TP = 1.10          # 任意日收盘浮盈达TP -> 次日开盘清仓
    SL = 0.98          # 收盘浮亏破SL -> 下个交易日开盘清仓
    SL_ANY = False     # True=止损任意日触发 / False=仅周五(原脚本口径)
    FRAC = 0.95        # 开仓资金比例(原脚本0.95)
    STD = True         # 特征标准化(原脚本无量纲处理, 原始成交量会淹没rbf核)
    CW = "balanced"    # SVC class_weight(原脚本None, 涨跌标签不均衡时有偏)
    N_FEAT = 15        # 特征窗口(原脚本15)
    H = 5              # 预测/标签前瞻天数(原脚本5)
    RETRAIN_EVERY = 5  # 至少隔多少根重训一次(周频节奏)
    MIN_TRAIN = 10     # 最少训练样本数, 不足则不出信号
    BURN_DATE = ""     # 训练烧入截止日(该日之前只攒样本不交易, 原脚本"先训练后交易"设计)

    def init_state(self, cash):
        return {"cash": float(cash), "sh": 0, "entry_px": 0.0,
                "clf": None, "scaler": None, "last_fit": -999}

    def decide(self, st, ind, bars, j, o, mkt_cl):
        if self.BURN_DATE and bars[j]["dt"] < self.BURN_DATE:
            return None                  # 烧入期: 只积累训练历史, 不交易
        cl = bars[j]["close"]
        # ---- 持仓: 出场判定(收盘条件 -> 次日开盘成交) ----
        if st["sh"] > 0:
            ent = st["entry_px"]
            if ent > 0:
                if cl / ent >= self.TP:
                    return {"act": "SELL", "reason": "SVM止盈"}
                if cl / ent <= self.SL and (self.SL_ANY
                                            or _weekday(bars[j]["dt"]) == 5):
                    return {"act": "SELL", "reason": "SVM止损"}
            return None
        # ---- 空仓: 周五收盘出信号(下个交易日=周一开盘成交, 对齐原策略) ----
        if j < self.N_FEAT + self.MIN_TRAIN + self.H:
            return None                  # 训练样本尚未攒够
        if _weekday(bars[j]["dt"]) != 5:
            return None
        self._fit(st, bars, j)
        if st["clf"] is None:
            return None
        x = np.array([_features(bars, j, self.N_FEAT)])
        if st["scaler"] is not None:
            x = st["scaler"].transform(x)
        try:
            pred = st["clf"].predict(x)[0]
        except Exception:
            return None
        if pred == 1:
            return {"act": "BUY_ML", "reason": "SVM预测5日后涨",
                    "frac": self.FRAC}
        return None

    def _fit(self, st, bars, j):
        """扩张窗口重训: 样本i取[14, j-H], 标签close[i+H]>close[i] —— 严格仅用历史"""
        if st["clf"] is not None and j - st["last_fit"] < self.RETRAIN_EVERY:
            return
        st["last_fit"] = j
        xs, ys = [], []
        for i in range(self.N_FEAT - 1, j - self.H + 1):
            xs.append(_features(bars, i, self.N_FEAT))
            ys.append(1 if bars[i + self.H]["close"] > bars[i]["close"] else 0)
        if len(xs) < self.MIN_TRAIN or len(set(ys)) < 2:
            return
        try:
            X = np.array(xs)
            scaler = None
            if self.STD:
                scaler = StandardScaler().fit(X)
                X = scaler.transform(X)
            clf = _svm.SVC(C=self.C, kernel="rbf", gamma=self.GAMMA,
                           class_weight=self.CW,
                           decision_function_shape="ovr")
            clf.fit(X, ys)
            st["clf"], st["scaler"] = clf, scaler
        except Exception:
            st["clf"], st["scaler"] = None, None

    def apply_fill(self, st, code, name, date, o, pending, check_open):
        from common import fees as F
        a = pending["act"]
        if a == "SELL":
            q = st["sh"] // 100 * 100
            if q < 100:
                return None
            amt = round(q * o, 2)
            f = F.sell_fee(amt, code)
            st["cash"] = st["cash"] + amt - f
            st["sh"] -= q
            st["entry_px"] = 0.0
            return {"date": date, "code": code, "name": name, "side": "SELL",
                    "qty": q, "price": round(o, 3), "amount": amt, "fee": f,
                    "reason": pending.get("reason", ""), "leg": "ml"}
        if a == "BUY_ML":
            frac = pending.get("frac", self.FRAC)
            q = int(st["cash"] * frac / o // 100 * 100)
            if q < 100:
                return None
            amt = round(q * o, 2)
            f = F.buy_fee(amt, code)
            if st["cash"] < amt + f:
                return None
            st["cash"] = st["cash"] - amt - f
            st["sh"] += q
            st["entry_px"] = o            # 不含费入场价(止盈止损基准)
            return {"date": date, "code": code, "name": name, "side": "BUY",
                    "qty": q, "price": round(o, 3), "amount": amt, "fee": f,
                    "reason": pending.get("reason", ""), "leg": "ml"}
        return None
