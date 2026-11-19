# -*- coding: utf-8 -*-
"""东财7x24快讯(全球财经电报)获取层(2026-11-16 新闻雷达)

接口: np-weblist.eastmoney.com getFastNewsList, 焦点栏目fastColumn=102
特点: ①realSort微秒游标可翻页拉全天历史 ②每条自带stockList关联码
     ("0.002126"个股 / "90.BK0579"东财板块), 雷达可免LLM直配持仓与板块
返回条目: {code, realSort, showTime"YYYY-MM-DD HH:MM:SS", title, summary, stockList}
"""
import datetime
import time

import requests

_EM_FAST_URL = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_fast_news(page_size=50, sort_end=""):
    """拉一页快讯(倒序); sort_end=上页末条realSort翻页; 失败抛异常"""
    r = requests.get(_EM_FAST_URL, params={
        "client": "web", "biz": "web_724", "fastColumn": "102",
        "sortEnd": sort_end, "pageSize": page_size,
        "req_trace": str(int(time.time() * 1000)),
    }, timeout=8, headers=_HEADERS)
    r.raise_for_status()
    lst = (r.json().get("data") or {}).get("fastNewsList") or []
    out = []
    for it in lst:
        if not (it.get("title") and it.get("realSort")):
            continue
        out.append({"code": it.get("code") or "",
                    "realSort": str(it["realSort"]),
                    "showTime": str(it.get("showTime") or "")[:19],
                    "title": str(it["title"]).strip(),
                    "summary": str(it.get("summary") or it["title"]).strip(),
                    "stockList": [str(s) for s in (it.get("stockList") or [])]})
    return out


def fetch_news_since(start_dt, max_pages=40):
    """拉取showTime>=start_dt的全部快讯(翻页), 按时间正序返回;
    start_dt为datetime; 超过max_pages或翻到早于start即停(失败安全: 单页失败即截断)"""
    all_items, last, pages = [], "", 0
    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    while pages < max_pages:
        try:
            lst = fetch_fast_news(sort_end=last)
        except Exception:
            break
        if not lst:
            break
        fresh = [x for x in lst if x["showTime"] >= start_str]
        all_items.extend(fresh)
        pages += 1
        oldest = lst[-1]["showTime"]
        if oldest < start_str or len(fresh) < len(lst):
            break
        last = lst[-1]["realSort"]
        time.sleep(0.3)                      # 翻页限速
    # 去重(code) + 正序
    seen, uniq = set(), []
    for x in all_items:
        k = x["code"] or x["realSort"]
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    uniq.sort(key=lambda x: x["showTime"])
    return uniq


def prev_trading_close(now=None):
    """上一交易日的15:00(datetime): 周一回退到周五, 平日回退一天(节假日不判, 多拉无害)"""
    now = now or datetime.datetime.now()
    d = now.date() - datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return datetime.datetime.combine(d, datetime.time(15, 0))
