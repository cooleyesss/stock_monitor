# -*- coding: utf-8 -*-
"""重出quant日报(dry重生成, 状态零改动) + 追加消息面节(2026-11-23)

场景: 用户要求"结合周末的各种消息重新出上周1120的quant日报"——
  周一盘前运行, 日K止于上周五, engine.run(dry=True)以最新代码(出场计划卡
  排版/最后交易日取数规则)重生成1120日报正文, 不推进不落盘;
  再拉取 rep日15:00→现在的东财7x24快讯, 关联持仓/候选池+重磅关键词,
  追加"周末消息面"节后重发邮件。

用法: python lab/reissue_report.py            (rep日取state权益记录最后一天)
      python lab/reissue_report.py --resend   (直接重推上次落盘的正文, 免全市场扫描)
正文落盘 data/quant/reissue_last.html, 推送限频丢段后可 --resend 补发
"""
import datetime
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import news as news_api
from common import notify
from ttrade.news_radar import _codes_of_item, _hit_hot_kw
from quant import engine

STATE_FILE = os.path.join("data", "quant", "quant_state.json")
BODY_FILE = os.path.join("data", "quant", "reissue_last.html")
CAND_CAP, MACRO_CAP = 12, 15


def _news_section(rep, state):
    """rep日15:00至今的快讯 -> 持仓相关/候选池相关/重磅宏观 三张表"""
    hold = {c: a["name"] for c, a in state["accounts"].items()
            if a.get("sh", 0) > 0}
    cand = {v["code"]: v["name"] for v in (state.get("cand_pool") or {}).values()}
    start = datetime.datetime.strptime(rep, "%Y-%m-%d").replace(hour=15, minute=0)
    try:
        items = news_api.fetch_news_since(start)
    except Exception as e:
        return notify.sec(f"周末消息面（拉取失败: {str(e)[:60]}）")
    h_rows, c_rows, m_rows, c_seen = [], [], [], set()
    for it in items:                     # fetch_news_since已按时间正序
        tm, title, summ = it["showTime"][5:16], it["title"], it["summary"]
        codes = _codes_of_item(it)
        hs = [f"{hold[c]}({c})" for c in codes if c in hold]
        if not hs:                       # 关联码没带时按名称兜底命中
            hs = [f"{n}({c})" for c, n in hold.items()
                  if n in title or n in summ[:60]]
        if hs:
            h_rows.append([tm, notify.bold(title), summ[:80], "、".join(hs)])
            continue
        cs = [f"{cand[c]}({c})" for c in codes if c in cand]
        if cs:
            if it["code"] in c_seen:
                continue
            c_seen.add(it["code"])
            c_rows.append([tm, title, summ[:60], "、".join(cs)])
            continue
        if _hit_hot_kw(it):
            m_rows.append([tm, title])
    span = f"{rep} 15:00 → {datetime.datetime.now().strftime('%m-%d %H:%M')}"
    sec = notify.sec(f"周末消息面（{span}｜快讯{len(items)}条）")
    if h_rows:
        sec += notify.table(["时间", "快讯", "摘要", "关联持仓"], h_rows)
    else:
        sec += notify.colored("周末无直接关联持仓(新奥股份)的快讯", "#cccccc") + "<br>"
    if c_rows:
        more = len(c_rows) - CAND_CAP
        sec += notify.table(["时间", "快讯", "摘要", "关联候选"],
                            c_rows[:CAND_CAP])
        if more > 0:
            sec += notify.colored(f"... 另有{more}条候选池相关快讯未列", "#cccccc") + "<br>"
    if m_rows:
        sec += notify.table(["时间", "重磅宏观/市场快讯"], m_rows[-MACRO_CAP:])
    if not (h_rows or c_rows or m_rows):
        sec += notify.colored("周末快讯无持仓/候选关联且无重磅关键词命中", "#cccccc")
    return sec


def main():
    if "--resend" in sys.argv and os.path.exists(BODY_FILE):
        body = io.open(BODY_FILE, encoding="utf-8").read()
        m = re.search(r"周末消息面（(\d{4}-\d{2}-\d{2})", body)
        rep = m.group(1) if m else "?"
        notify.send(f"📊 量化独立账户日报 {rep}（重出·补发）", body)
        print("[重出] 已按落盘正文补发")
        return True
    body = engine.run(dry=True, push=False)   # dry: 推进零成交/状态零改动/dashboard重生成
    if not body or not isinstance(body, str):
        print("[重出] 报告正文生成失败")
        return False
    state = json.load(io.open(STATE_FILE, encoding="utf-8"))   # dry未落盘, 与body同口径
    rep = state["equity"][-1]["date"]
    body += _news_section(rep, state)
    os.makedirs(os.path.dirname(BODY_FILE), exist_ok=True)
    io.open(BODY_FILE, "w", encoding="utf-8").write(body)      # 落盘: 限频丢段后可--resend
    notify.send(f"📊 量化独立账户日报 {rep}（重出·含周末消息面）", body)
    print(f"[重出] 已发送: 量化日报 {rep} + 周末消息面")
    return True


if __name__ == "__main__":
    main()
