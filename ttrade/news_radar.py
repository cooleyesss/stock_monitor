# -*- coding: utf-8 -*-
"""新闻雷达(2026-11-16 用户需求: 新闻/舆情辅助预测板块与个股方向)

两个职责:
  ① 盘中秒级告警 poll(): 主循环每轮调用, 轮询东财7x24快讯(增量),
     关联码(stockList自带个股/板块码)或重磅关键词命中 -> DeepSeek判定 ->
     重大级秒级微信推送(只观察不交易); 一般级仅记录。
  ② 8:45隔夜汇总 morning_brief(): 交易日前拉"上一交易日15:00收盘后"的全部快讯
     (覆盖夜间+周末), DeepSeek分批打分 -> ①宏观要点 ②板块情绪 ③持仓股相关新闻,
     微信推送 + 存data/ttrade/news_history.json(按日累积)。
     供 9:00 SVM日频训练做新闻特征 + 9:26 t_plan生成参考。

失败安全: 接口/LLM任何失败 -> 降级关键词版或跳过, 不影响主流程;
         无新闻记录时SVM新闻特征=0(退化为纯价格特征)。
LLM成本控制: 盘中仅"关联持仓/板块码 或 重磅关键词命中"的条目才送LLM;
         8:45汇总按批(25条/批)压缩成标题列表送LLM。
"""
import datetime
import json
import os

import requests

from ttrade import config_ttrade as config
from common import news as news_api
from common import notify


# ---------------- 通用小工具 ----------------
def names_of(state, tplan):
    """code -> 名称(t_plan优先, 持仓兜底)"""
    names = {}
    for src in ((tplan or {}), (state.get("positions") or {})):
        for c, p in src.items():
            if isinstance(p, dict) and p.get("name"):
                names.setdefault(c, p["name"])
    return names


def _llm(prompt, max_tokens=800):
    """通用DeepSeek调用(新闻雷达专用, 不依赖state); 失败返回None"""
    if not (getattr(config, "NEWS_LLM_EN", False) and config.DEEPSEEK_ENABLED):
        return None
    try:
        r = requests.post(config.DEEPSEEK_API_URL, timeout=config.DEEPSEEK_TIMEOUT,
                          headers={"Authorization": "Bearer " + config.DEEPSEEK_API_KEY,
                                   "Content-Type": "application/json"},
                          json={"model": config.DEEPSEEK_MODEL,
                                "messages": [{"role": "user", "content": prompt}],
                                "temperature": 0.2, "max_tokens": max_tokens})
        r.raise_for_status()
        return ((r.json().get("choices") or [{}])[0]
                .get("message", {}).get("content", "").strip()) or None
    except Exception as e:
        print("[新闻雷达] LLM调用失败(降级关键词版):", str(e)[:80])
        return None


def _json_of(text):
    """从LLM回复中提取第一个JSON对象; 失败返回None"""
    if not text:
        return None
    s = text.find("{")
    e = text.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        return json.loads(text[s:e + 1])
    except Exception:
        return None


def _codes_of_item(item):
    """stockList关联码 -> 6位数字代码集合: '0.002126'->002126, '90.BK0579'板块忽略"""
    return {s.split(".")[-1] for s in item["stockList"]
            if "." in s and s.split(".")[-1].isdigit() and len(s.split(".")[-1]) == 6}


# ---------------- 盘中轮询(秒级告警) ----------------
def _seen_store():
    """已读快讯realSort去重档(跨重启); 只保留最近N条防膨胀"""
    path = config.NEWS_SEEN_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _seen_save(sorts):
    os.makedirs(os.path.dirname(config.NEWS_SEEN_FILE), exist_ok=True)
    with open(config.NEWS_SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorts[-config.NEWS_SEEN_KEEP:], f)


def _hit_hot_kw(item):
    text = item["title"] + item["summary"][:60]
    return any(k in text for k in config.NEWS_KW_HOT)


def poll(now, holding_codes, dry=False):
    """主循环每轮调用: 拉最新一页快讯 -> 增量 -> 过筛 -> LLM分级 -> 重大秒级推送"""
    if not getattr(config, "NEWS_RADAR_EN", False):
        return
    try:
        items = news_api.fetch_fast_news(page_size=50)
    except Exception as e:
        print("[新闻雷达] 快讯拉取失败:", str(e)[:80])
        return
    seen = _seen_store()
    seen_set = set(seen)
    fresh = [x for x in items if x["realSort"] not in seen_set][:config.NEWS_POLL_MAX]
    if not fresh:
        return
    _seen_save(seen + [x["realSort"] for x in fresh])
    for it in fresh:
        hits = _codes_of_item(it) & set(holding_codes)
        if not (hits or _hit_hot_kw(it)):
            continue                      # 与持仓无关且无重磅词, 不打扰
        _alert_one(it, sorted(hits), dry)


def _alert_one(item, hits, dry):
    """单条候选快讯: LLM判定重大级才推送"""
    title = item["title"]
    if not item["showTime"] or item["showTime"] < datetime.date.today().isoformat():
        return                            # 只告警当日快讯(启动积压的老消息不推)
    j = _json_of(_llm(
        "你是A股新闻快评员。判断以下快讯对A股市场的重要性与方向。\n"
        f"【快讯】{item['showTime']} {title}\n{item['summary'][:300]}\n\n"
        "严格只输出JSON(不要其他内容): "
        '{"level": "重大"或"一般", "dir": "利好"或"利空"或"中性", '
        '"sectors": ["受益/受损板块名"], "brief": "30字内点评"}'))
    level = (j or {}).get("level", "")
    if level != "重大":
        return                            # 一般级不推送(8:45汇总覆盖)
    d = (j or {}).get("dir", "中性")
    sectors = "、".join((j or {}).get("sectors") or []) or "—"
    icon = "🔴利好" if d == "利好" else ("🟢利空" if d == "利空" else "⚪中性")
    color = config.C_RED if d == "利空" else (
        config.C_GREEN if d == "利好" else config.C_GRAY)   # A股红涨绿跌: 利空红? 统一用橙
    print(f"[新闻雷达] 重大{d}: {title[:40]}")
    if dry:
        print(f"[DRY-新闻雷达] 推送跳过: {icon} {title[:50]}")
        return
    notify.send(
        f"🚨 新闻雷达·重大{d}",
        "<br>".join([
            notify.colored(notify.bold(f"{icon} {title}"), config.C_ORANGE),
            f"🕐 {item['showTime']} | 涉及板块: {sectors}",
            (f"⚠️ 关联持仓: {', '.join(hits)}" if hits else ""),
            f"💬 {(j or {}).get('brief', '')}",
            notify.colored("仅供观察参考, 不构成操作指令", config.C_GRAY)]))


# ---------------- 8:45 隔夜汇总 ----------------
def load_history():
    """按日累积的隔夜汇总档: {date: {stocks:{code:{score,hits,titles}}, sectors, highlights}}"""
    try:
        with open(config.NEWS_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_history(hist):
    os.makedirs(os.path.dirname(config.NEWS_HISTORY_FILE), exist_ok=True)
    with open(config.NEWS_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)


def score_at(code, on_date, hist=None):
    """截至on_date(含)最近一次汇总中该股的隔夜新闻情绪分(-2..+2)与条数; 无记录=(0,0)
    (SVM日频训练的新闻特征; 8:45生成, 9:00训练可用, 严格无未来函数)"""
    hist = hist if hist is not None else load_history()
    best = None
    for d in hist:
        if d <= str(on_date)[:10] and (best is None or d > best):
            best = d
    if not best:
        return 0.0, 0
    s = ((hist[best].get("stocks") or {}).get(code)) or {}
    return float(s.get("score", 0) or 0), int(s.get("hits", 0) or 0)


def morning_brief(now, state, tplan, dry=False):
    """8:45入口: 汇总上一交易日收盘以来的全部快讯 -> 推送 + 存history(当日键)
    幂等: history已有今日键则跳过"""
    td = now.strftime("%Y-%m-%d")
    hist = load_history()
    if td in hist:
        print(f"[新闻雷达] {td} 汇总已生成, 跳过(幂等)")
        return
    names = names_of(state, tplan)
    holding = set(names) | {config.ETF_CODE_MAP.get(c, c) for c in state["positions"]}
    start = news_api.prev_trading_close(now)
    items = news_api.fetch_news_since(start)
    print(f"[新闻雷达] 隔夜快讯{len(items)}条"
          f"(覆盖{start.strftime('%m-%d %H:%M')}以来), 开始汇总...")
    # 持仓相关(LLM前先粗筛: 关联码或股票名命中)
    rel = [it for it in items
           if _codes_of_item(it) & holding
           or any(n[:2] in it["title"] + it["summary"][:80] for n in names.values()
                  if len(n) >= 2)]
    rec = {"until": start.strftime("%Y-%m-%d %H:%M"), "n": len(items),
           "stocks": {}, "sectors": {}, "highlights": []}
    # LLM分批打分(全部条目按批, 每批只要标题; 无LLM则降级仅持仓相关)
    batches = [items[i:i + config.NEWS_LLM_BATCH]
               for i in range(0, len(items), config.NEWS_LLM_BATCH)]
    for bi, bat in enumerate(batches):
        lines = [f"{it['showTime'][11:16]} {it['title']}" for it in bat]
        hold_txt = "、".join(f"{n}({c})" for c, n in sorted(names.items())) or "无"
        j = _json_of(_llm(
            "你是A股晨报编辑。以下是隔夜财经快讯列表，请输出结构化摘要。\n"
            f"【持仓股】{hold_txt}\n【快讯{len(bat)}条】\n" + "\n".join(lines) +
            "\n\n严格只输出JSON(不要其他内容): "
            '{"stocks": {"代码": {"score": -2到2整数(利好正/利空负, 无关则不列), '
            '"titles": ["相关标题"]}}, '
            '"sectors": {"板块名": {"dir": 1或-1, "n": 条数}}, '
            '"highlights": ["最多3条宏观/重磅要点, 每条25字内"]}'))
        if not j:
            continue
        for c, s in (j.get("stocks") or {}).items():
            c = str(c).strip()[:6]
            if c not in names:
                continue
            sc = s if isinstance(s, int) else int(s.get("score", 0) or 0)  # 容错
            tl = [] if isinstance(s, int) else (s.get("titles") or [])
            old = rec["stocks"].setdefault(c, {"score": 0, "hits": 0, "titles": []})
            old["score"] = max(-2, min(2, old["score"] + sc))
            old["hits"] += 1
            old["titles"] = (old["titles"] + tl)[:5]
        for sn, s in (j.get("sectors") or {}).items():
            sname = str(sn).strip()[:8]
            if not sname:
                continue
            d = s if isinstance(s, int) else int(s.get("dir", 0) or 0)   # 容错: 允许直接给整数
            old = rec["sectors"].setdefault(sname, {"dir": 0, "n": 0})
            old["dir"] = d
            old["n"] += (1 if isinstance(s, int) else int(s.get("n", 1) or 1))
        rec["highlights"] += [str(x)[:40] for x in (j.get("highlights") or [])]
    rec["highlights"] = rec["highlights"][:8]
    # 降级兜底: LLM没覆盖到的持仓相关条目按关键词补录
    for it in rel:
        c = next(iter(_codes_of_item(it) & holding), None)
        if c and c not in rec["stocks"]:
            txt = it["title"]
            sc = -1 if any(k in txt for k in config.NEWS_KW_BAD) else (
                1 if any(k in txt for k in config.NEWS_KW_GOOD) else 0)
            rec["stocks"][c] = {"score": sc, "hits": 1, "titles": [txt[:40]]}
    if dry:
        print(f"[DRY-新闻雷达] 汇总: {len(rec['stocks'])}只持仓相关, "
              f"{len(rec['sectors'])}个板块, 要点{len(rec['highlights'])}条")
        return
    hist[td] = rec
    _save_history(hist)
    _push_brief(rec, names, start, now)
    print(f"[新闻雷达] 隔夜汇总已存 {config.NEWS_HISTORY_FILE}")


def _push_brief(rec, names, start, now):
    secs = sorted(rec["sectors"].items(), key=lambda x: -abs(x[1]["dir"]) * x[1]["n"])
    sec_txt = "、".join(f"{n}({'利好' if v['dir'] > 0 else '利空'}×{v['n']})"
                        for n, v in secs[:6]) or "无明显方向"
    hl = "<br>".join("• " + h for h in rec["highlights"]) or "—"
    st_rows = []
    for c, s in sorted(rec["stocks"].items()):
        sc = s["score"]
        t = (f"+{sc}" if sc > 0 else str(sc)) if sc else "0"
        col = config.C_RED if sc > 0 else (config.C_GREEN if sc < 0 else config.C_GRAY)
        st_rows.append([names.get(c, c), notify.colored(t, col),
                        "; ".join(s["titles"][:2])[:60] or "—"])
    parts = [
        f"🗞 覆盖{start.strftime('%m-%d %H:%M')}以来快讯{rec['n']}条",
        notify.sec("宏观/重磅要点") + hl,
        notify.sec("板块情绪") + sec_txt]
    if st_rows:
        parts.append(notify.sec("持仓相关") + notify.table(
            ["股票", "隔夜分", "相关新闻"], st_rows))
    else:
        parts.append(notify.sec("持仓相关") + "隔夜无直接相关新闻")
    parts.append(notify.colored("仅供观察参考, 已作为9:00 SVM新闻特征与9:26 t_plan参考",
                                config.C_GRAY))
    notify.send(f"📰 新闻雷达·隔夜汇总 ({now.strftime('%m-%d')} 早)", "<br>".join(parts))
