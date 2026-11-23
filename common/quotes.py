# -*- coding: utf-8 -*-
"""东方财富实时行情 + K线MACD (多级容错: 东财 -> 腾讯 -> 新浪)"""
import datetime
import json
import os
import time
import requests
import config_common as config
from common import notify

MACD_CACHE = {}   # code -> (时间戳, {macd:{...}, rsi, prsi, rsi6, prsi6})  兼容旧名
_LAST_KREQ = [0.0]   # K线请求节流时间戳


def _throttle():
    """相邻K线请求最小间隔, 防东财限流"""
    dt = time.time() - _LAST_KREQ[0]
    if dt < config.KLINE_MIN_INTERVAL:
        time.sleep(config.KLINE_MIN_INTERVAL - dt)
    _LAST_KREQ[0] = time.time()


def secid(code):
    """东财secid: 指数别名999999=上证指数; 含'.'直通; 沪市(6/5开头)前缀1, 深市前缀0
    注意: 裸'000001'是深市平安银行, 上证指数必须走999999别名, 绝不能进个股代码集合"""
    if code == config.REGIME_INDEX_ALIAS:
        return "1.000001"
    if "." in code:
        return code
    return ("1." if code[0] in "56" else "0.") + code


def fetch_quotes(codes):
    """批量拉取实时行情(价格/涨跌幅/量比/行情时间戳)
    返回 {code: {name,price,pct,vr,ts}}"""
    if not codes:
        return {}
    params = {
        "secids": ",".join(secid(c) for c in codes),
        "fields": "f2,f3,f10,f12,f14,f124",
        "fltt": 2,
    }
    last_err = None
    for host in config.QUOTE_HOSTS:
        try:
            r = requests.get(host + "/api/qt/ulist.np/get", params=params,
                             timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            diff = (r.json().get("data") or {}).get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            quotes = {}
            for d in diff:
                p = d.get("f2")
                if not isinstance(p, (int, float)):   # 停牌/无数据
                    continue
                vr = d.get("f10")
                quotes[str(d.get("f12"))] = {
                    "name": d.get("f14", ""),
                    "price": float(p),
                    "pct": d.get("f3") if isinstance(d.get("f3"), (int, float)) else 0,
                    "vr": float(vr) if isinstance(vr, (int, float)) else None,
                    "ts": d.get("f124"),
                }
            return quotes
        except Exception as e:
            last_err = e
            time.sleep(1)
            continue
    # 东财全部节点失败 -> 新浪批量兜底(2026-11-19)
    try:
        qs = _fetch_quotes_sina(codes)
        if qs:
            print(f"[行情] 东财实时节点均失败, 新浪兜底{len(qs)}只")
            return qs
    except Exception as e:
        last_err = last_err or e
    raise last_err if last_err else RuntimeError("所有行情节点均失败")


def _fetch_quotes_sina(codes):
    """新浪实时行情兜底(东财全部节点故障时): hq.sinajs.cn批量(必须带Referer头, GBK)
    无量比字段(vr=None由调用方按东财停牌口径处理); 涨跌幅按昨收推算; ts转epoch对齐f124
    代码映射与腾讯同构(999999->sh000001); 返回 {code: {name,price,pct,vr,ts}}"""
    pairs = [(c, _tx_code(c)) for c in codes]
    pairs = [(c, s) for c, s in pairs if s]
    rev = {s: c for c, s in pairs}                      # sh600036 -> 原始code(含999999别名)
    out = {}
    for i in range(0, len(pairs), config.SINA_HQ_BATCH):
        batch = [s for _, s in pairs[i:i + config.SINA_HQ_BATCH]]
        r = requests.get(config.SINA_HQ_HOST + "/list=" + ",".join(batch),
                         timeout=10, headers={"User-Agent": "Mozilla/5.0",
                                             "Referer": "https://finance.sina.com.cn"})
        r.encoding = "gbk"
        for ln in r.text.splitlines():
            try:
                head, body = ln.split('="', 1)
                sym = head.split("hq_str_", 1)[1].strip()
                code = rev.get(sym)
                f = body.rstrip('";\r').split(",")
                if not code or len(f) < 32:
                    continue
                price = float(f[3])
                prev = float(f[2])
                if price <= 0 or prev <= 0:             # 停牌/无数据
                    continue
                ts = int(time.mktime(time.strptime(f"{f[30]} {f[31]}", "%Y-%m-%d %H:%M:%S")))
                out[code] = {"name": f[0], "price": price,
                             "pct": round((price / prev - 1) * 100, 2),
                             "vr": None, "ts": ts}
            except (IndexError, ValueError, KeyError):
                continue
    if not out:
        raise RuntimeError("新浪实时行情为空")
    return out


def fetch_kline_closes(code, klt=None, lmt=120):
    """拉取K线收盘价序列(最后一根为当前形成中的bar)
    复用 fetch_kline_bars 的多源容错(东财->腾讯), 保证指标层不掉线"""
    bars = fetch_kline_bars(code, klt=klt, lmt=lmt)
    return [b["close"] for b in bars]


_EM_STATE = [0, 0.0]   # [东财K线连续失败次数, 熔断截止时间戳]


def _fetch_kline_bars_em(code, klt, lmt, rounds=None, beg=None):
    """东财K线(多节点多轮重试); 失败抛异常由调用方转腾讯回退
    rounds: 尝试轮数, 默认KLINE_RETRY; 熔断后探测时只给1轮(省时)
    beg: 增量起点(YYYYMMDD); 给定时按beg~end区间取数, 否则按lmt取最近N根"""
    params = {
        "secid": secid(code),
        "fields1": "f1,f2,f3",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": klt, "fqt": 1,
        "end": "20500101",
    }
    if beg:
        params["beg"] = beg
    else:
        params["lmt"] = lmt
    last_err = None
    for attempt in range(rounds or config.KLINE_RETRY):
        for host in config.KLINE_HOSTS:
            _throttle()
            try:
                r = requests.get(host + "/api/qt/stock/kline/get",
                                 params=params, timeout=10,
                                 headers={"User-Agent": "Mozilla/5.0"})
                klines = r.json().get("data", {}).get("klines") or []
                bars = []
                for k in klines:
                    f = k.split(",")
                    if len(f) < 7:
                        continue
                    try:
                        bars.append({"dt": f[0], "open": float(f[1]),
                                     "close": float(f[2]), "high": float(f[3]),
                                     "low": float(f[4]), "vol": float(f[5]),
                                     "amount": float(f[6])})
                    except ValueError:
                        continue
                if not bars:
                    raise RuntimeError("东财K线为空")
                return bars
            except Exception as e:
                last_err = e
                continue
        if attempt < config.KLINE_RETRY - 1:
            time.sleep(config.KLINE_RETRY_SLEEP)
    raise last_err if last_err else RuntimeError("东财K线节点均失败")


def _tx_code(code):
    """东财代码 -> 腾讯代码: 999999->sh000001; 1.xxxxxx->sh; 0.xxxxxx->sz
    板块指数(90.BKxxxx)腾讯无对应, 返回None(仍只能走东财)"""
    if code == config.REGIME_INDEX_ALIAS:
        return "sh000001"
    if code.startswith("90."):
        return None
    if "." in code:
        mkt, c = code.split(".", 1)
        return ("sh" if mkt == "1" else "sz") + c
    return ("sh" if code[0] in "56" else "sz") + code


_TX_KIND = {"101": "day", "1": "m1", "5": "m5", "15": "m15", "30": "m30", "60": "m60"}


def _tx_dt(raw):
    """腾讯时间 -> 统一dt格式: 日K 'YYYY-MM-DD', 分钟K 'YYYYMMDDHHMM'->'YYYY-MM-DD HH:MM'"""
    raw = str(raw)
    if len(raw) == 10:
        return raw
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}"


def _fetch_kline_bars_tx(code, klt, lmt):
    """腾讯K线(东财故障时的回退源): 日K走fqkline(前复权qfq, 与东财fqt=1口径一致),
    分钟K走mkline(不复权原始价); 腾讯不提供成交额, 用 量(手)*100*均价 近似(仅供流动性门槛/量能票使用)
    日K多节点轮询(2026-11-04): 主站ifzq.gtimg.cn被WAF(501)时降级
    proxy.finance.qq.com/ifzqgtimg / web.ifzq.gtimg.cn(回测期实测两备用节点稳定)
    返回格式与东财一致: {dt,open,close,high,low,vol,amount}"""
    txc = _tx_code(code)
    if txc is None:
        raise RuntimeError("腾讯源不支持该标的(板块指数仅东财提供)")
    kind = _TX_KIND.get(str(klt))
    if kind is None:
        raise RuntimeError(f"腾讯源不支持klt={klt}")
    if kind == "day":
        urls = [f"{h}/appstock/app/fqkline/get?param={txc},day,,,{lmt},qfq"
                for h in config.TX_KLINE_HOSTS]
    else:
        urls = [f"{config.TX_MKLINE_HOST}/appstock/app/kline/mkline"
                f"?param={txc},{kind},,{lmt}"]
    last_err = None
    for url in urls:
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            d = (r.json().get("data") or {}).get(txc) or {}
            rows = d.get("qfqday") or d.get("day") or d.get(kind) or []
            bars = []
            for row in rows:
                try:
                    o, c, h, l, v = (float(row[1]), float(row[2]), float(row[3]),
                                     float(row[4]), float(row[5]))
                except (ValueError, IndexError):
                    continue
                bars.append({"dt": _tx_dt(row[0]), "open": o, "close": c, "high": h,
                             "low": l, "vol": v,
                             "amount": round(v * 100 * (h + l + c) / 3, 2)})
            if not bars:
                raise RuntimeError("腾讯K线为空")
            return bars
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("腾讯K线节点均失败")


_SINA_KIND = {"101": 240, "1": 1, "5": 5, "15": 15, "30": 30, "60": 60}   # klt -> 新浪scale(分钟)


def _fetch_kline_bars_sina(code, klt, lmt):
    """新浪K线(第三级兜底: 东财/腾讯均故障时, 2026-11-19)
    代码映射与腾讯同构(复用_tx_code); 日K为不复权原始价(东财fqt=1/腾讯qfq为前复权,
    除权日口径会漂移, k180合并的重叠校验会检测并整段重建自愈); 成交量单位为股,
    /100转手对齐东财/腾讯; 分钟K自带成交额, 日K无额按量价近似(与腾讯回退同法)
    注意: 日K不含当日盘中形成bar(腾讯含), 盘中降级时末根为上一交易日完整bar
    返回格式与东财一致: {dt,open,close,high,low,vol,amount}"""
    sc = _tx_code(code)
    if sc is None:
        raise RuntimeError("新浪源不支持该标的(板块指数仅东财提供)")
    scale = _SINA_KIND.get(str(klt))
    if scale is None:
        raise RuntimeError(f"新浪源不支持klt={klt}")
    try:
        r = requests.get(config.SINA_KLINE_URL,
                         params={"symbol": sc, "scale": scale, "ma": "no",
                                 "datalen": int(lmt)},
                         timeout=10, headers={"User-Agent": "Mozilla/5.0",
                                              "Referer": "https://finance.sina.com.cn"})
        rows = r.json()
        bars = []
        for row in rows:
            try:
                o, c = float(row["open"]), float(row["close"])
                h, l = float(row["high"]), float(row["low"])
                v = float(row["volume"]) / 100.0            # 股 -> 手
                a = float(row.get("amount") or 0) or round(v * 100 * (h + l + c) / 3, 2)
            except (ValueError, KeyError, TypeError):
                continue
            dt = str(row.get("day", ""))
            if scale < 240:                                 # 分钟K 'YYYY-MM-DD HH:MM:SS'
                dt = dt[:16]
            bars.append({"dt": dt, "open": o, "close": c, "high": h, "low": l,
                         "vol": v, "amount": a})
        if not bars:
            raise RuntimeError("新浪K线为空")
        return bars
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"新浪K线请求失败: {e}")


def _fetch_kline_raw(code, klt, lmt, beg=None):
    """真实网络拉取(东财优先 -> 腾讯回退 -> 新浪兜底), 无缓存
    beg: 仅日K增量使用(东财按区间取数); 腾讯/新浪不支持区间, 按lmt取最近N根"""
    if time.time() >= _EM_STATE[1]:
        rounds = 1 if _EM_STATE[0] >= config.EM_FAIL_BREAK else config.KLINE_RETRY
        try:
            bars = _fetch_kline_bars_em(code, klt, lmt, rounds=rounds, beg=beg)
            _EM_STATE[0] = 0
            return bars
        except Exception as e:
            _EM_STATE[0] += 1
            if _EM_STATE[0] >= config.EM_FAIL_BREAK:
                _EM_STATE[1] = time.time() + config.EM_DOWN_SEC
                print(f"[行情] 东财K线连续失败{_EM_STATE[0]}次, 熔断{config.EM_DOWN_SEC}秒改用腾讯源")
            else:
                print(f"[行情] 东财K线失败({e}), 改用腾讯源")
    try:
        return _fetch_kline_bars_tx(code, klt, lmt)
    except Exception as e:
        print(f"[行情] 腾讯K线失败({e}), 改用新浪源")
        return _fetch_kline_bars_sina(code, klt, lmt)


def fetch_kline_bars(code, klt=None, lmt=None):
    """拉取K线完整OHLCV(时间/开/收/高/低/量/额), 升序, 末根为当前形成中bar
    返回 list[dict]: {"dt":"2026-10-13 09:31", "open","close","high","low",
                       "vol"(手), "amount"(元)}; code支持999999指数别名
    日K(klt=101)走180天增量缓存池: 命中直接返回, 缺失只补拉增量, 大幅降低接口调用;
    分钟K保持实时拉取(做T信号不能吃缓存延迟)
    数据源: 东财优先(push2his, 多轮重试) -> 连续失败EM_FAIL_BREAK次则本进程内熔断
            EM_DOWN_SEC秒, 期间直接走腾讯(实测东财节点拒连时腾讯稳定可用)
    注意: 板块指数(90.BKxxxx)腾讯无对应, 东财熔断期间将直接失败"""
    klt, lmt = klt or config.KLINE_KLT, lmt or config.KLINE_LMT
    if config.KLINE_CACHE_EN and str(klt) == "101":
        return _fetch_daily_cached(code, int(lmt))
    return _fetch_kline_raw(code, klt, lmt)


# ---------------- 日K 180天增量缓存池 ----------------
# 结构: 每标的一个JSON(按需读写, 避免整池大文件反复落盘)
#   {"code","klt","bars":[...],"last_dt","updated","fresh_until"}
# 增量: 以缓存末根日期为beg请求东财区间数据, 按dt合并; 腾讯回退时按根数拉取后合并
# 口径变化保护: 重叠bar收盘价不一致(除权除息导致前复权整段重算) -> 自动整段重建
_K180_MEM = {}        # "101:code" -> 记录dict(进程内缓存, 免重复读盘)
_K180_PRUNED = [False]


def _k180_path(code):
    return os.path.join(config.KLINE_CACHE_DIR,
                        "101_" + str(code).replace(".", "_") + ".json")


def _k180_load(code):
    key = "101:" + str(code)
    if key in _K180_MEM:
        return _K180_MEM[key]
    try:
        with open(_k180_path(code), encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        rec = {}
    _K180_MEM[key] = rec
    return rec


def _k180_save(code, rec):
    _K180_MEM["101:" + str(code)] = rec
    try:
        os.makedirs(config.KLINE_CACHE_DIR, exist_ok=True)
        p = _k180_path(code)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception as e:
        print("日K缓存池写入失败", e)


def _k180_prune():
    """清理超过保留期的缓存文件(每进程一次), 控制磁盘占用"""
    if _K180_PRUNED[0]:
        return
    _K180_PRUNED[0] = True
    try:
        cutoff = time.time() - config.KLINE_CACHE_DAYS * 86400
        for fn in os.listdir(config.KLINE_CACHE_DIR):
            if fn.endswith(".json"):
                p = os.path.join(config.KLINE_CACHE_DIR, fn)
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
    except Exception:
        pass


def _k180_trim(bars):
    """只保留最近KLINE_CACHE_DAYS个自然日"""
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=config.KLINE_CACHE_DAYS)).strftime("%Y-%m-%d")
    return [b for b in bars if str(b.get("dt", ""))[:10] >= cutoff]


def _k180_merge(old, new):
    """按dt合并(新覆盖旧); 返回(bars, adjusted)
    adjusted=True: 重叠bar收盘价不一致 -> 前复权口径已重算, 需整段重建"""
    m = {b["dt"]: b for b in old}
    adjusted = False
    for b in new:
        o = m.get(b["dt"])
        if o and abs(float(o.get("close", 0)) - float(b["close"])) > max(
                0.005, float(b["close"]) * 0.001):
            adjusted = True
        m[b["dt"]] = b
    return [m[k] for k in sorted(m)], adjusted


def _k180_fresh(rec, nowdt):
    """缓存是否可直接复用: 新鲜期内, 或收盘后已拿到当日完整K线(锁定至次日)
    锁定复用仅限"15:00后写入的当日bar"(eod_saved); 盘中写入的当日bar是未完成数据,
    收盘后必须强制增量刷新, 不能拿盘中价冒充收盘价(2026-10-22日报失准根因)"""
    if not rec.get("bars"):
        return False
    if time.time() < rec.get("fresh_until", 0):
        if rec.get("forming_last") and nowdt.hour >= 15:
            return False                        # 盘中bar跨过15:00: 强制刷新拿收盘完整bar
        return True
    today = nowdt.strftime("%Y-%m-%d")
    return (rec.get("last_dt") == today and nowdt.hour >= 15
            and bool(rec.get("eod_saved")))


def _fetch_daily_cached(code, lmt, refresh=False):
    """日K增量缓存读取: 命中返回缓存尾段; 未命中只拉缺失区间并合并
    refresh=True: 无视新鲜期强制走一次增量(仍不重复拉全量)"""
    lmt = max(int(lmt or config.KLINE_LMT), 1)
    nowdt = datetime.datetime.now()
    rec = {} if refresh else _k180_load(code)
    bars = rec.get("bars") or []
    want = max(lmt, config.KLINE_CACHE_BARS)
    healed = False
    # 窗口扩容自愈(2026-11-04, 180->280自然日): 池内根数不足want且池头晚于
    # "窗口起点+14天宽限"(历史被旧窗口截断; 增量只补尾部永远补不齐历史) ->
    # 全量重建回填一次(重建已含最新bar, 跳过增量直接落盘);
    # 重建无改善(次新股上市日就在窗口内)则沿用旧池
    if bars and len(bars) < want:
        grace = (nowdt.date() - datetime.timedelta(
            days=config.KLINE_CACHE_DAYS + 14)).strftime("%Y-%m-%d")
        if str(bars[0].get("dt", ""))[:10] > grace:
            try:
                rebuilt = _k180_trim(_fetch_kline_raw(code, 101, want))
                if len(rebuilt) > len(bars):
                    bars = rebuilt
                    healed = True
            except Exception:
                pass                    # 重建失败: 沿用旧池走增量
    if bars and not healed and not refresh and _k180_fresh(rec, nowdt):
        return bars[-lmt:]
    if config.KLINE_CACHE_EN:
        _k180_prune()
    if not bars:                                    # 空缓存: 首次全量建池
        try:
            bars = _k180_trim(_fetch_kline_raw(code, 101, want))
        except Exception:
            return []                               # 首次失败: 交由调用方容错
    elif not healed:                                # 自愈重建已含最新bar, 免增量
        beg = str(bars[-1].get("dt", ""))[:10].replace("-", "")
        try:
            new = _fetch_kline_raw(code, 101, want, beg=beg)   # 增量(含末根刷新)
        except Exception:
            new = []                                # 无新增(非交易日/接口波动)沿用缓存
        if new:
            merged, adjusted = _k180_merge(bars, new)
            if adjusted:                            # 复权口径变化 -> 整段重建
                try:
                    merged = _fetch_kline_raw(code, 101, want)
                except Exception:
                    pass
            bars = _k180_trim(merged)
        elif (nowdt.hour >= 15
              and str(bars[-1].get("dt", ""))[:10] == nowdt.strftime("%Y-%m-%d")
              and not rec.get("eod_saved")):
            # 收盘后增量刷新失败, 且缓存末根是今日盘中写入的未确认bar:
            # 拒绝以盘中价冒充收盘价, 抛错交由调用方按"行情缺失"降级
            raise RuntimeError(
                f"{code} 收盘后日K刷新失败, 缓存末根为盘中未确认bar, 拒绝用作收盘价")
    if not bars:
        return []
    last_dt = str(bars[-1]["dt"])[:10]
    today = nowdt.strftime("%Y-%m-%d")
    eod = nowdt.hour * 60 + nowdt.minute >= 15 * 60
    is_today = (last_dt == today)
    end_of_day = (datetime.datetime.combine(nowdt.date() + datetime.timedelta(days=1),
                                            datetime.time.min).timestamp())
    rec = {"code": code, "klt": 101, "bars": bars, "last_dt": last_dt,
           "updated": nowdt.strftime("%Y-%m-%d %H:%M:%S"),
           "forming_last": bool(is_today and not eod),   # 末根为今日盘中未完成bar
           "eod_saved": bool(is_today and eod),          # 末根为今日收盘完整bar
           "fresh_until": end_of_day if (is_today and eod)
           else time.time() + config.KLINE_CACHE_TTL}
    _k180_save(code, rec)
    return bars[-lmt:]



K1_CACHE = {}   # code -> (时间戳, bars) 1分钟K线缓存


def get_bars_1m(code, lmt=None):
    """1分钟K线(K1_CACHE_SEC缓存); 失败返回None由调用方降级(技术票弃权)"""
    now = time.time()
    c = K1_CACHE.get(code)
    if c and now - c[0] < config.K1_CACHE_SEC and c[2] == (lmt or config.K1_LMT):
        return c[1]
    try:
        bars = fetch_kline_bars(code, klt=config.K1_KLT, lmt=lmt or config.K1_LMT)
        K1_CACHE[code] = (now, bars, lmt or config.K1_LMT)
        return bars
    except Exception as e:
        print("1分钟K线获取失败", code, e)
        return c[1] if c else None


_INDEX_QUOTE = [0.0, None]   # [时间戳, 行情]上证指数缓存


def get_index_quote():
    """上证指数实时行情(60秒缓存, 与主循环对齐): {name,price,pct,vr,ts}; 失败返回None"""
    now = time.time()
    if _INDEX_QUOTE[1] and now - _INDEX_QUOTE[0] < 60:
        return _INDEX_QUOTE[1]
    params = {"secids": secid(config.REGIME_INDEX_ALIAS),
              "fields": "f2,f3,f10,f12,f14,f124", "fltt": 2}
    last_err = None
    for host in config.QUOTE_HOSTS:
        try:
            r = requests.get(host + "/api/qt/ulist.np/get", params=params,
                             timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            diff = (r.json().get("data") or {}).get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            for d in diff:
                p = d.get("f2")
                if isinstance(p, (int, float)):
                    q = {"name": d.get("f14", ""), "price": float(p),
                         "pct": (d.get("f3") if isinstance(d.get("f3"), (int, float)) else 0),
                         "vr": (d.get("f10") if isinstance(d.get("f10"), (int, float)) else None),
                         "ts": d.get("f124")}
                    _INDEX_QUOTE[0], _INDEX_QUOTE[1] = now, q
                    return q
            return _INDEX_QUOTE[1]
        except Exception as e:
            last_err = e
            continue
    # 东财全部节点失败 -> 新浪兜底(sh000001指数别名同样支持, 2026-11-19)
    try:
        qs = _fetch_quotes_sina([config.REGIME_INDEX_ALIAS])
        if qs.get(config.REGIME_INDEX_ALIAS):
            _INDEX_QUOTE[0], _INDEX_QUOTE[1] = now, qs[config.REGIME_INDEX_ALIAS]
            return qs[config.REGIME_INDEX_ALIAS]
    except Exception:
        pass
    print("指数行情获取失败", last_err)
    return _INDEX_QUOTE[1]


def fetch_trends_bars(code, ndays=5):
    """历史分时(trends2): 最近ndays个交易日的1分钟bar(含当日实时)
    东财限制: ndays>5返回空; 每日241根含09:30集合竞价bar(已剔除对齐klt=1口径)
    注意: 历史日的f52开盘价恒为0(仅当日实时bar有真开盘价), 用前bar收盘价近似
    字段: f51时间,f52开,f53收,f54高,f55低,f56量(手),f57额,f58均价
    返回 list[dict] 同fetch_kline_bars格式(供回测用)"""
    params = {
        "secid": secid(code),
        "fields1": "f1,f2,f3,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ndays": ndays, "iscr": "0",
    }
    last_err = None
    for host in config.KLINE_HOSTS:
        try:
            r = requests.get(host + "/api/qt/stock/trends2/get",
                             params=params, timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"})
            tr = (r.json().get("data") or {}).get("trends") or []
            bars = []
            prev_close = None
            for t in tr:
                f = t.split(",")
                if len(f) < 8:
                    continue
                try:
                    b = {"dt": f[0], "open": float(f[1]), "close": float(f[2]),
                         "high": float(f[3]), "low": float(f[4]),
                         "vol": float(f[5]), "amount": float(f[6])}
                except ValueError:
                    continue
                if b["dt"].endswith(" 09:30"):   # 剔除集合竞价bar
                    continue
                if b["open"] <= 0:   # 历史日开盘价为0 -> 前bar收盘价近似
                    b["open"] = prev_close if prev_close else b["close"]
                prev_close = b["close"]
                bars.append(b)
            return bars
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("分时节点均失败")


def _ema_series(values, n):
    out, e, k = [], None, 2.0 / (n + 1)
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def _rsi_series(closes, n):
    """Wilder平滑RSI序列"""
    if len(closes) < n + 2:
        return []
    k = 1.0 / n
    ag = al = None
    rsis = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        ag = g if ag is None else g * k + ag * (1 - k)
        al = l if al is None else l * k + al * (1 - k)
        rsis.append(100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al))
    return rsis


def get_indicators(code):
    """技术指标(带缓存, 一次K线请求): 15分钟线 MACD + RSI(14) + RSI(6)
    返回 {macd:{dif,dea,hist,pdif,pdea,phist}, rsi, prsi, rsi6, prsi6}"""
    now = time.time()
    c = MACD_CACHE.get(code)
    if c and now - c[0] < config.IND_CACHE_SEC:
        return c[1]
    try:
        closes = fetch_kline_closes(code, lmt=config.KLINE_LMT)
        if len(closes) < 35:
            return c[1] if c else None
        e12 = _ema_series(closes, 12)
        e26 = _ema_series(closes, 26)
        dif = [a - b for a, b in zip(e12, e26)]
        dea = _ema_series(dif, 9)
        hist = [2 * (d - e) for d, e in zip(dif, dea)]
        r14 = _rsi_series(closes, config.RSI_PERIOD)
        r6 = _rsi_series(closes, config.RSI_FAST)
        m = {"macd": {"dif": dif[-1], "dea": dea[-1], "hist": hist[-1],
                      "pdif": dif[-2], "pdea": dea[-2], "phist": hist[-2]},
             "rsi": r14[-1] if r14 else None,
             "prsi": r14[-2] if len(r14) > 1 else None,
             "rsi6": r6[-1] if r6 else None,
             "prsi6": r6[-2] if len(r6) > 1 else None}
        MACD_CACHE[code] = (now, m)
        return m
    except Exception as e:
        print("指标获取失败", code, e)
        return c[1] if c else None


def get_macd(code):
    """兼容接口: 仅取MACD部分"""
    ind = get_indicators(code)
    return ind["macd"] if ind else None


def macd_state_text(m):
    """MACD状态短文本(供AI prompt用): 金叉/死叉/柱体走强/走弱"""
    if not m:
        return "无数据"
    dif, dea, hist = m["dif"], m["dea"], m["hist"]
    pdif, pdea, phist = m["pdif"], m["pdea"], m["phist"]
    if pdif is not None and pdea is not None:
        if pdif <= pdea and dif > dea:
            return "金叉(DIF上穿DEA)"
        if pdif >= pdea and dif < dea:
            return "死叉(DIF下穿DEA)"
    if phist is not None:
        if hist > phist:
            return "柱体走强(多头动能增强)"
        if hist < phist:
            return "柱体走弱(动能衰减)"
    return "走平"


def macd_note(m, side):
    """MACD注解(红绿着色HTML): side=buy(低吸)/sell(高抛或止损)"""
    if not m:
        return ""
    txt = macd_state_text(m)
    if side == "buy":
        if "金叉" in txt:
            return notify.colored(notify.bold(f"〰️MACD金叉✅({txt})"), config.C_RED)
        if "走强" in txt:
            return notify.colored(f"〰️MACD{txt}✅", config.C_RED)
        if "走弱" in txt:
            return notify.colored(f"〰️MACD{txt}⚠️", config.C_GREEN)
        return "〰️MACD走平"
    else:
        if "死叉" in txt:
            return notify.colored(notify.bold(f"〰️MACD死叉✅({txt})"), config.C_GREEN)
        if "走弱" in txt:
            return notify.colored(f"〰️MACD{txt}✅", config.C_GREEN)
        if "走强" in txt:
            return notify.colored(f"〰️MACD{txt}⚠️可再持", config.C_ORANGE)
        return "〰️MACD走平"


# ---------------- RSI ----------------
def rsi_state_text(rsi, prsi=None):
    """RSI状态短文本(供AI prompt用): 超买/超卖/偏多/偏空 + 趋向"""
    if rsi is None:
        return "无数据"
    trend = ""
    if prsi is not None:
        trend = "↗" if rsi > prsi else ("↘" if rsi < prsi else "→")
    if rsi >= config.RSI_OVERBOUGHT:
        return f"超买{rsi:.1f}{trend}"
    if rsi <= config.RSI_OVERSOLD:
        return f"超卖{rsi:.1f}{trend}"
    if rsi >= 50:
        return f"偏多{rsi:.1f}{trend}"
    return f"偏空{rsi:.1f}{trend}"


def rsi_note(ind, side):
    """RSI注解(红绿着色HTML): side=buy(低吸)/sell(高抛或止损)
    低吸遇超卖=价值高✅ / 遇超买=追高风险⚠️; 高抛遇超买=价值高✅ / 遇超卖=割肉风险⚠️"""
    if not ind or ind.get("rsi") is None:
        return ""
    rsi = ind["rsi"]
    txt = rsi_state_text(rsi, ind.get("prsi"))
    r6 = ind.get("rsi6")
    r6_txt = f" (RSI6:{r6:.1f})" if r6 is not None else ""
    if side == "buy":
        if rsi <= config.RSI_OVERSOLD:
            return notify.colored(notify.bold(f"🌡️RSI{txt} 超卖企稳·低吸价值高✅{r6_txt}"),
                                  config.C_RED)
        if rsi >= config.RSI_OVERBOUGHT:
            return notify.colored(f"🌡️RSI{txt} 超买区低吸·追高风险⚠️{r6_txt}",
                                  config.C_GREEN)
        return f"🌡️RSI{txt}{r6_txt}"
    else:
        if rsi >= config.RSI_OVERBOUGHT:
            return notify.colored(notify.bold(f"🌡️RSI{txt} 超买冲高·高抛价值高✅{r6_txt}"),
                                  config.C_RED)
        if rsi <= config.RSI_OVERSOLD:
            return notify.colored(f"🌡️RSI{txt} 超卖区卖出·割肉风险⚠️{r6_txt}",
                                  config.C_GREEN)
        return f"🌡️RSI{txt}{r6_txt}"


# ================= 东财板块/题材接口(智能选股用) =================
# 实测: clist 仅 http 节点可用(https 被拒); 板块指数日K走 push2his, secid="90.BKxxxx"
# 板块字段: f12=BK代码 f14=名称 f3=涨幅% f6=成交额(元) f104=涨家数 f105=跌家数
# 个股字段: f2=价 f3=涨幅 f6=成交额 f8=换手 f9=动态PE f23=PB f26=上市日期(YYYYMMDD)
_KCACHE = None      # 磁盘缓存 {f"{klt}:{code}": {"date": "YYYY-MM-DD", "lmt": N, "bars": [...]}}
_MEM_K = {}         # f"{klt}:{code}" -> (时间戳, lmt, bars) 内存缓存


def _kpath():
    return os.path.join(config.CACHE_DIR, "kline_cache.json")


def _kcache():
    global _KCACHE
    if _KCACHE is None:
        _KCACHE = {}
        try:
            with open(_kpath(), encoding="utf-8") as f:
                _KCACHE = json.load(f)
        except Exception:
            _KCACHE = {}
    return _KCACHE


def _kcache_save():
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        tmp = _kpath() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_KCACHE, f, ensure_ascii=False)
        os.replace(tmp, _kpath())
    except Exception as e:
        print("K线缓存写入失败", e)


def fetch_kline_bars_cached(code, klt=101, lmt=120, refresh=False):
    """带缓存的日K拉取(选股批量扫描用): 统一走180天增量缓存池
    非日K周期仍沿用原"当日有效"缓存(分钟K按需刷新)"""
    if config.KLINE_CACHE_EN and str(klt) == "101":
        return _fetch_daily_cached(code, lmt, refresh=refresh)
    key = f"{klt}:{code}"
    now = time.time()
    c = _MEM_K.get(key)
    if c and not refresh and c[1] >= lmt and now - c[0] < config.PICK_CACHE_SEC:
        return c[2]
    cache = _kcache()
    rec = cache.get(key)
    today = datetime.date.today().strftime("%Y-%m-%d")
    if rec and not refresh and rec.get("date") == today and rec.get("lmt", 0) >= lmt:
        bars = rec["bars"]
    else:
        bars = fetch_kline_bars(code, klt=klt, lmt=lmt)
        cache[key] = {"date": today, "lmt": lmt, "bars": bars}
        _kcache_save()
    _MEM_K[key] = (now, lmt, bars)
    return bars


def fetch_board_bars(bk, lmt=120, refresh=False):
    """板块指数日K(secid=90.BKxxxx): 复用带缓存的日K拉取(板块指数仅日线粒度)"""
    return fetch_kline_bars_cached("90." + bk, klt=101, lmt=lmt, refresh=refresh)


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def _clist(fs, fid, n, fields, pn=1):
    """东财列表接口(clist): 返回 (total, diff列表); http节点容错"""
    params = {"pn": pn, "pz": n, "po": 1, "np": 1, "fltt": 2, "invt": 2,
              "fid": fid, "fs": fs, "fields": fields}
    last_err = None
    for host in config.CLIST_HOSTS:
        try:
            r = requests.get(host + "/api/qt/clist/get", params=params, timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            d = r.json().get("data") or {}
            diff = d.get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            return d.get("total"), diff
        except Exception as e:
            last_err = e
            time.sleep(0.3)
            continue
    raise last_err if last_err else RuntimeError("clist节点均失败")


def market_opened(now=None):
    """当前是否处于"交易日已开盘"状态(近似: 工作日且时间>=09:30)
    2026-11-23 用户规则"选股取最后交易日数据": 已开盘用今日实时数据,
    未开盘(盘前/周末/节假日)用上一交易日数据; 节假日按工作日近似(与
    limitup_market_on既有的weekday近似口径一致)"""
    now = now or datetime.datetime.now()
    return now.weekday() < 5 and now.time() >= datetime.time(9, 30)


def fetch_board_list(kind="concept", n=30):
    """板块榜(按当日涨幅降序): kind=concept(概念题材)/industry(行业板块)
    返回 list[{bk,name,pct,amount,up,down}]; 仅供实况展示/预热(热点判定走板块指数动量)"""
    fs = "m:90+t:3" if kind == "concept" else "m:90+t:2"
    _, diff = _clist(fs, "f3", n, "f12,f14,f3,f6,f104,f105")
    out = []
    for d in diff:
        if not d.get("f12"):
            continue
        out.append({"bk": str(d["f12"]), "name": d.get("f14") or "",
                    "pct": _num(d.get("f3")) or 0.0,
                    "amount": _num(d.get("f6")) or 0.0,
                    "up": d.get("f104"), "down": d.get("f105")})
    return out


def fetch_board_members(bk, n=15):
    """板块成分股(按成交额降序): 返回 list[{code,name,price,pct,amount,...}]
    2026-11-23 用户规则"选股取最后交易日数据": 东财f2实时价在盘前/停牌时为空
    (2026-11-18盘前宇宙为空的病根), 加请求f18昨收字段兜底——f2为空时
    price取昨收(=上一交易日收盘), 即选股宇宙永远拿得到最后交易日价格;
    开盘后f2为正常实时价"""
    _, diff = _clist("b:" + bk, "f6", n,
                     "f12,f14,f2,f3,f6,f8,f9,f18,f23,f26")
    out = []
    for d in diff:
        p = _num(d.get("f2"))
        if p is None:                           # 盘前/停牌: 实时价为空 -> 昨收兜底
            p = _num(d.get("f18"))
        code = str(d.get("f12") or "")
        if p is None or len(code) != 6 or not code.isdigit():
            continue
        out.append({"code": code, "name": d.get("f14") or code, "price": p,
                    "pct": _num(d.get("f3")) or 0.0, "amount": _num(d.get("f6")) or 0.0,
                    "turnover": _num(d.get("f8")), "pe": _num(d.get("f9")),
                    "pb": _num(d.get("f23")), "list_date": d.get("f26")})
    return out


# ---------------- 个股所属行业(clist f100): 风格/情绪板候选的真实板块标签, 永久缓存 ----------------
_IND_CACHE_FILE = os.path.join(config.CACHE_DIR, "industry_cache.json")
_IND_CACHE = None


def fetch_stock_industry(code):
    """个股所属行业板块名(东财clist f100, 如"出版"/"小家电"); 失败返回None
    行业归属极少变动, 结果永久磁盘缓存; clist被限流时回退 push2 stock/get f127
    (两接口路径不同, 限流互不影响); 失败不写缓存, 下次自动重试"""
    global _IND_CACHE
    if _IND_CACHE is None:
        try:
            _IND_CACHE = json.load(open(_IND_CACHE_FILE, encoding="utf-8"))
        except Exception:
            _IND_CACHE = {}
    if code in _IND_CACHE:
        return _IND_CACHE[code]
    secid = ("1." if code[0] in "56" else "0.") + code
    ind = None
    try:
        r = requests.get("https://push2.eastmoney.com/api/qt/clist/get",
                         params={"pn": 1, "pz": 1, "po": 1, "np": 1,
                                 "fltt": 2, "invt": 2, "fs": f"i:{secid}",
                                 "fields": "f12,f14,f100"},
                         timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        diff = ((r.json().get("data") or {}).get("diff") or [])
        if diff and diff[0].get("f100"):
            ind = diff[0]["f100"]
    except Exception:
        pass
    if not ind:          # clist失败/为空 -> stock/get f127兜底(直连绕开系统代理故障)
        _hosts = sorted(config.QUOTE_HOSTS,
                        key=lambda h: 0 if h.startswith("http://") else 1)
        for host in _hosts:
            try:
                r = requests.get(host + "/api/qt/stock/get",
                                 params={"secid": secid, "fltt": 2, "invt": 2,
                                         "fields": "f127"},
                                 timeout=8, headers={"User-Agent": "Mozilla/5.0"},
                                 proxies={"http": None, "https": None})
                ind = ((r.json().get("data") or {}).get("f127") or None)
                if ind in ("-", ""):
                    ind = None
                if ind:
                    break
            except Exception:
                continue
    if not ind:
        return None
    _IND_CACHE[code] = ind
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        json.dump(_IND_CACHE, open(_IND_CACHE_FILE, "w", encoding="utf-8"))
    except Exception:
        pass
    return ind


# ---------------- 行业名->BK代码全量映射(市场环境展示用): 分页拉取+磁盘缓存 ----------------
_BKMAP_FILE = os.path.join(config.CACHE_DIR, "industry_bk_map.json")


def fetch_industry_bk_map(max_age_days=7):
    """行业板块名 -> BK代码 映射(东财行业板块全量约500个, 含Ⅱ/Ⅲ细分级别)
    磁盘缓存max_age_days天(行业划分极少变动); clist失败时回退过期缓存, 均失败返回{}
    注意: 板块榜按当日涨幅排序且单页上限100, 必须翻页拉全"""
    today = datetime.date.today().isoformat()
    m = {}
    try:
        m = json.load(open(_BKMAP_FILE, encoding="utf-8"))
    except Exception:
        m = {}
    names = m.get("names") or {}
    fresh = bool(names) and m.get("_date") == today or (
        bool(names) and m.get("_date") and
        (datetime.date.today() - datetime.date.fromisoformat(m["_date"])).days < max_age_days)
    if not fresh:
        _names, pn = {}, 1
        try:
            while pn <= 6:
                total, diff = _clist("m:90+t:2", "f3", 100, "f12,f14", pn=pn)
                if not diff:
                    break
                for d in diff:
                    if d.get("f12"):
                        _names[str(d["f12"])] = d.get("f14") or ""
                if len(_names) >= (total or 0):
                    break
                pn += 1
                time.sleep(0.5)
        except Exception as e:
            print("[行业映射] 板块榜拉取失败(回退缓存):", repr(e)[:80])
        if len(_names) >= 50:      # 拉到合理规模才覆盖缓存(避免半页数据污染)
            names = _names
            try:
                os.makedirs(config.CACHE_DIR, exist_ok=True)
                json.dump({"_date": today, "names": names},
                          open(_BKMAP_FILE, "w", encoding="utf-8"), ensure_ascii=False)
            except Exception:
                pass
    return {v: k for k, v in names.items()}   # 反转为 name -> bk


# ---------------- 板块名->BK 直查(chist限流时的兜底): 东财suggest接口 ----------------
_BKEXTRA_FILE = os.path.join(config.CACHE_DIR, "industry_bk_extra.json")
_BKEXTRA = None


def fetch_board_bk_by_name(name):
    """按板块名直查BK代码(东财suggest, 如"元件"->BK0459)
    用于行业板块榜(clist)被限流、全量映射里查不到名称时兜底; 结果永久磁盘缓存"""
    global _BKEXTRA
    if not name or name in ("-", ""):
        return None
    if _BKEXTRA is None:
        try:
            _BKEXTRA = json.load(open(_BKEXTRA_FILE, encoding="utf-8"))
        except Exception:
            _BKEXTRA = {}
    if name in _BKEXTRA:
        return _BKEXTRA[name] or None
    bk = None
    try:
        r = requests.get("http://searchapi.eastmoney.com/api/suggest/get",
                         params={"input": name, "type": 14,
                                 "token": "D43BF722C8E33BDC906FB84D85E326E8",
                                 "count": 10},
                         timeout=8, headers={"User-Agent": "Mozilla/5.0"},
                         proxies={"http": None, "https": None})
        rows = ((r.json().get("QuotationCodeTable") or {}).get("Data") or [])
        for d in rows:      # 仅取板块(MktNum=90, Code以BK开头), 优先同名精确命中
            c = str(d.get("Code") or "")
            if d.get("MktNum") == "90" and c.startswith("BK"):
                if d.get("Name") == name:
                    bk = c
                    break
                bk = bk or c
    except Exception:
        return None
    if bk:
        _BKEXTRA[name] = bk
        try:
            os.makedirs(config.CACHE_DIR, exist_ok=True)
            json.dump(_BKEXTRA, open(_BKEXTRA_FILE, "w", encoding="utf-8"),
                      ensure_ascii=False)
        except Exception:
            pass
    return bk


# ---------------- 个股新闻(t_plan生成因子D: 利空利好判定) 东财个股资讯流, 内存短缓存 ----------------
_NEWS_MEM = {}   # code -> (时间戳, [新闻dict]) 10分钟TTL, 防dry/重试重复请求


def fetch_stock_news(code, lmt=5):
    """拉取个股最新新闻标题流(东财个股资讯接口)
    返回 [{"title": str, "time": "YYYY-MM-DD HH:MM"}] 按时间倒序; 失败返回[](不影响主流程)"""
    now = time.time()
    hit = _NEWS_MEM.get(code)
    if hit and now - hit[0] < 600:
        return hit[1][:lmt]
    try:
        r = requests.get(
            "https://np-listapi.eastmoney.com/comm/web/getListInfo",
            params={"m": 1, "col": 606, "mTypeAndCode": secid(code), "type": 1,
                    "kfl": 1, "n": max(lmt, 10), "otype": "json", "client": "web"},
            timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        lst = ((r.json() or {}).get("data") or {}).get("list") or []
        news = []
        for it in lst:
            t = str(it.get("Art_ShowTime") or it.get("showTime") or "")[:16]
            title = str(it.get("Art_Title") or it.get("title") or "").strip()
            if title:
                news.append({"title": title, "time": t})
        _NEWS_MEM[code] = (now, news)
        return news[:lmt]
    except Exception:
        return []


# ---------------- 涨停池(连板行情判定, 选股用): 东财push2ex, 按日磁盘缓存 ----------------
_ZT_CACHE_FILE = os.path.join(config.CACHE_DIR, "zt_pool_cache.json")
_ZT_CACHE = None


def fetch_zt_pool(day):
    """指定交易日涨停池统计: 返回 {"cnt": 涨停家数, "lb": 连板(>=2板)家数} 或 None
    day="YYYY-MM-DD"或"YYYYMMDD"; 历史数据按日磁盘缓存(当日数据每次重拉)"""
    global _ZT_CACHE
    d = day.replace("-", "")
    today = datetime.datetime.now().strftime("%Y%m%d")
    if _ZT_CACHE is None:
        try:
            _ZT_CACHE = json.load(open(_ZT_CACHE_FILE, encoding="utf-8"))
        except Exception:
            _ZT_CACHE = {}
    if d != today and d in _ZT_CACHE:        # 历史日已封板, 永久缓存
        return _ZT_CACHE[d]
    try:
        r = requests.get("http://push2ex.eastmoney.com/getTopicZTPool",
                         params={"ut": "7eea3edcaed734bea9cbfc24409ed989",
                                 "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 500,
                                 "sort": "fbt:asc", "date": d},
                         timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        pool = ((r.json().get("data") or {}).get("pool")) or []
        res = {"cnt": len(pool),
               "lb": sum(1 for x in pool if (x.get("lbc") or 1) >= 2)}
    except Exception:
        return _ZT_CACHE.get(d)              # 接口故障退回缓存(可能为None)
    _ZT_CACHE[d] = res
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        json.dump(_ZT_CACHE, open(_ZT_CACHE_FILE, "w", encoding="utf-8"))
    except Exception:
        pass
    return res


# ---------------- Baostock 分钟级K线(半年级历史, 东财/腾讯仅2-5日) ----------------
_BS_CACHE_FILE = os.path.join(config.CACHE_DIR, "minute_cache.json")
_BS_CACHE = None
_BS_FREQ = "5"   # Baostock 支持的最细粒度且历史完整(1m不支持)


def _bs_cache():
    global _BS_CACHE
    if _BS_CACHE is None:
        _BS_CACHE = {}
        try:
            with open(_BS_CACHE_FILE, encoding="utf-8") as f:
                _BS_CACHE = json.load(f)
        except Exception:
            _BS_CACHE = {}
    return _BS_CACHE


def _bs_cache_save():
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        tmp = _BS_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_BS_CACHE, f, ensure_ascii=False)
        os.replace(tmp, _BS_CACHE_FILE)
    except Exception as e:
        print("Baostock分钟K线缓存写入失败", e)


def fetch_minute_bars_bs(code, freq="5", start="2026-04-01", end="2026-10-19",
                         refresh=False):
    """Baostock分钟级K线(半年级历史, 需pip install baostock)
    code: 6位数字(600557/300455等), freq: '5'/'15'/'30'
    返回 [{dt,open,close,high,low,vol,amount}], dt格式'YYYY-MM-DD HH:MM'
    带磁盘缓存(整个窗口数据按 code+freq+start+end 键缓存, 窗口不变则复用)"""
    key = f"bs:{freq}:{code}:{start}:{end}"
    cache = _bs_cache()
    if not refresh and key in cache:
        return cache[key]
    try:
        import baostock as bs
    except ImportError:
        raise RuntimeError("请先 pip install baostock")
    # 代码映射: 6位数字 -> sh./sz. 前缀; 指数特殊映射
    if code == "999999":           # 上证指数
        bs_code = "sh.000001"
    elif code.startswith(("5", "6")):
        bs_code = "sh." + code
    elif code.startswith(("0", "1", "2", "3")):
        bs_code = "sz." + code
    else:
        raise ValueError(f"无法映射代码到Baostock格式: {code}")
    bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            bs_code, "date,time,open,high,low,close,volume,amount",
            start_date=start, end_date=end, frequency=freq, adjustflag="2")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()
    if not rows:
        return []
    out = []
    for r in rows:
        # Baostock time格式: YYYYMMDDHHMMSS000 -> YYYY-MM-DD HH:MM
        t = r[1]
        dt = f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}"
        try:
            o, h, l, c = float(r[2]), float(r[3]), float(r[4]), float(r[5])
            v = float(r[6]) if r[6] else 0.0
            a = float(r[7]) if r[7] else 0.0
        except (ValueError, TypeError):
            continue
        out.append({"dt": dt, "open": o, "close": c, "high": h, "low": l,
                    "vol": v, "amount": a})
    cache[key] = out
    _bs_cache_save()
    return out
