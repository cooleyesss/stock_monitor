# -*- coding: utf-8 -*-
"""
t_plan 每日自动生成 (9:25 程序启动时触发)
==========================================
每日开盘前根据四因子重算做T计划价位, 覆盖写内部 t_plan.csv
(config_common.T_PLAN_CSV = data/ttrade/t_plan.csv):
  因子A 个股行情(技术面基准): 昨日日K BOLL/ATR ->
      低吸区 = [BOLL下轨+A1×ATR, 下轨+A2×ATR], 高抛区 = [中轨+H1×ATR, 中轨+H2×ATR],
      止损线 = 低吸下限 - S×ATR, 单次T价差 = max(0.15, 0.8×ATR)
      (与原手工规则一致: BOLL下轨附近缩量吸, MA20/BOLL中轨上方抛)
  因子B 大盘竞价: 上证指数竞价涨跌幅 ->
      ≥+1.0% 高抛区上移防追高 / ≤-1.0% 低吸区下移等更深 / |pct|≥2.5% 低吸区置空观望
  因子C 个股竞价(9:26多档细分): |gap|<0.5%不动 / 0.5~1.5%低吸微调 / 1.5~3%低吸+高抛调
           / >=3%或>=2ATR低吸置空; 竞价量比佐证(放量高开高抛再上移/放量低开低吸置空)
  因子D 个股新闻: 近3日新闻标题关键词判定 ->
      利空(减持/立案/预亏...) 当日停低吸 / 利好(预增/中标/回购...) 低吸高抛区上移
票池来自现有计划(首跑从旧外部文件 T_PLAN_LEGACY_CSV 种子导入), 只重算价位不换票;
手工修改内部 t_plan.csv 当日即时生效(runner mtime 热加载), 次日9:25被重算覆盖。
生成失败不阻塞主流程: 整体捕获异常保留现有计划并日志告警。
"""
import os
import sys
import shutil
import datetime

# 支持 python ttrade/tplan_gen.py 直接运行: 把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ttrade import config_ttrade as config
from common import quotes, notify
from ttrade import state as tstate
from core.indicators import build_ind
from ttrade import auction

# 新闻关键词词典(命中即判定; 利空优先于利好)
_NEWS_BAD = ("减持", "质押", "立案", "调查", "预亏", "亏损", "诉讼", "违规",
             "退市", "商誉减值", "下调", "警示", "处罚", "冻结", "问询")
_NEWS_GOOD = ("预增", "中标", "回购", "增持", "分红", "签约", "获批", "扭亏",
              "业绩增长", "订单", "合作")


def _seed_import():
    """首跑种子迁移: 内部计划不存在时从旧外部文件复制(票池/T量/量比门槛来源),
    价位随后被四因子全量重算覆盖"""
    if os.path.exists(config.T_PLAN_CSV):
        return
    if config.T_PLAN_LEGACY_CSV and os.path.exists(config.T_PLAN_LEGACY_CSV):
        shutil.copy2(config.T_PLAN_LEGACY_CSV, config.T_PLAN_CSV)
        print(f"[t_plan] 种子导入: {config.T_PLAN_LEGACY_CSV} -> {config.T_PLAN_CSV}")
    else:
        print("[t_plan] 内部计划与旧种子文件均不存在, 本轮跳过生成")


def _load_rows():
    """直接读内部 t_plan.csv 全部列(含T类型/说明原值保留) -> auction row 结构列表;
    load_t_plan()不解析T类型/说明, 直接读CSV避免写回时丢失这两列"""
    import csv
    rows = []
    try:
        with open(config.T_PLAN_CSV, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                code = (r.get("代码") or "").strip()
                if len(code) != 6 or not code.isdigit():
                    continue
                _f = lambda k: None if not (r.get(k) or "").strip() else float(r[k])
                rows.append({
                    "code": code,
                    "name": (r.get("名称") or "").strip() or code,
                    "T类型": (r.get("T类型") or "").strip(),
                    "说明": (r.get("说明") or "").strip(),
                    "buy_low": _f("低吸下限"), "buy_high": _f("低吸上限"),
                    "buy_vr_max": _f("低吸量比上限"),
                    "sell_low": _f("高抛下限"), "sell_high": _f("高抛上限"),
                    "sell_vr_min": _f("高抛量比下限"),
                    "stop_loss": _f("止损线"),
                    "min_spread": _f("单次T价差下限"),
                    "t_qty": _f("T量"),
                })
    except FileNotFoundError:
        return []
    return rows


def _base_from_ind(row, ind, i):
    """因子A: BOLL/ATR 技术面基准重算价位(ovewrite row); 返回说明list"""
    lo, mid, atr = ind.get("lo", [None])[i], ind.get("mid", [None])[i], ind["atr"][i]
    if not (lo and mid and atr and atr > 0):
        return []
    row["buy_low"] = round(lo + config.TP_LO_A1 * atr, 2)
    row["buy_high"] = round(lo + config.TP_LO_A2 * atr, 2)
    row["sell_low"] = round(mid + config.TP_HI_A1 * atr, 2)
    row["sell_high"] = round(mid + config.TP_HI_A2 * atr, 2)
    row["stop_loss"] = round(row["buy_low"] - config.TP_STOP_A * atr, 2)
    row["min_spread"] = round(max(0.15, config.TP_SPRD_A * atr), 2)
    return [f"技术面基准: 低吸{row['buy_low']}~{row['buy_high']}"
            f"(BOLL下轨{lo:.2f}+ATR), 高抛{row['sell_low']}~{row['sell_high']}"
            f"(中轨{mid:.2f}上方), 止损{row['stop_loss']}, ATR={atr:.2f}"]


def _mkt_shift(row, mkt_pct, atr):
    """因子B: 大盘竞价修正(mkt_pct=%); 返回说明list"""
    notes = []
    if mkt_pct is None:
        return notes
    s = config.TP_MKT_SHIFT * atr
    if abs(mkt_pct) >= config.TP_MKT_EXTREME:
        row["buy_low"], row["buy_high"] = None, None
        notes.append(f"大盘竞价{mkt_pct:+.1f}%≥{config.TP_MKT_EXTREME}%, 今日低吸置空观望")
    elif mkt_pct >= config.TP_MKT_HI:
        if row.get("sell_low"):
            row["sell_low"] = round(row["sell_low"] + s, 2)
            row["sell_high"] = round(row["sell_high"] + s, 2)
        if row.get("buy_low"):
            row["buy_low"] = round(row["buy_low"] + s / 2, 2)
            row["buy_high"] = round(row["buy_high"] + s / 2, 2)
        notes.append(f"大盘高开{mkt_pct:+.1f}% 高抛区上移{s:.2f}防追高")
    elif mkt_pct <= config.TP_MKT_LO:
        if row.get("buy_low"):
            row["buy_low"] = round(row["buy_low"] - s, 2)
            row["buy_high"] = round(row["buy_high"] - s, 2)
        if row.get("sell_low"):
            row["sell_low"] = round(row["sell_low"] - s / 2, 2)
            row["sell_high"] = round(row["sell_high"] - s / 2, 2)
        notes.append(f"大盘低开{mkt_pct:+.1f}% 低吸区下移{s:.2f}等更深")
    return notes


def _news_shift(row, code, atr):
    """因子D: 近3日新闻情绪修正; 返回说明list(无新闻/接口失败=不调整)"""
    if not config.TP_NEWS_EN:
        return []
    try:
        news = quotes.fetch_stock_news(code, lmt=config.TP_NEWS_LMT)
    except Exception:
        return []
    today = datetime.date.today()
    cut = today - datetime.timedelta(days=config.TP_NEWS_DAYS)
    recent = []
    for n in news:
        try:
            d = datetime.datetime.strptime(n["time"][:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if d >= cut:
            recent.append(n)
    text = " ".join(n["title"] for n in recent)
    if not text:
        return []
    if any(w in text for w in _NEWS_BAD):
        row["buy_low"], row["buy_high"] = None, None
        return [f"近{config.TP_NEWS_DAYS}日利空新闻停吸: {recent[0]['title'][:30]}"]
    if any(w in text for w in _NEWS_GOOD):
        s = config.TP_NEWS_SHIFT * atr
        if row.get("buy_low"):
            row["buy_low"] = round(row["buy_low"] + s, 2)
            row["buy_high"] = round(row["buy_high"] + s, 2)
        if row.get("sell_low"):
            row["sell_low"] = round(row["sell_low"] + s, 2)
            row["sell_high"] = round(row["sell_high"] + s, 2)
        return [f"近{config.TP_NEWS_DAYS}日利好新闻区带上移{s:.2f}: {recent[0]['title'][:30]}"]
    return []


def _gap_fine_shift(row, code, atr, last_close, now):
    """因子C(9:26竞价多档细分): 竞价开盘较昨收的多档位修正 + 竞价量比佐证
    档位: |gap|<0.5%不动 / 0.5~1.5%小档微调 / 1.5~3%大档调整 / >=3%或>=2ATR低吸置空
    量比佐证: 放量(vr>=3)高开->高抛区再上移; 放量低开->低吸置空(出逃)"""
    try:
        q = quotes.fetch_quotes([code]).get(code)
    except Exception:
        return []
    if not q or not q.get("price") or last_close <= 0 or atr <= 0:
        return []
    gap_pct = (q["price"] - last_close) / last_close * 100
    vr = q.get("vr")
    s1, s2, s3 = config.TP_S1 * atr, config.TP_S2 * atr, config.TP_S3 * atr
    gap_atr = atr / last_close * 100            # 2ATR对应的百分比阈值

    def _mv(buy=None, sell=None):
        if buy and row.get("buy_low"):
            row["buy_low"] = round(row["buy_low"] + buy, 2)
            row["buy_high"] = round(row["buy_high"] + buy, 2)
        if sell and row.get("sell_low"):
            row["sell_low"] = round(row["sell_low"] + sell, 2)
            row["sell_high"] = round(row["sell_high"] + sell, 2)

    def _clear():
        row["buy_low"], row["buy_high"] = None, None

    if gap_pct >= max(config.TP_G3, 2 * gap_atr):
        _clear()
        return [f"竞价高开{gap_pct:+.1f}%(≥2ATR) 低吸置空防追高"]
    if gap_pct <= -max(config.TP_G3, 2 * gap_atr):
        _clear()
        return [f"竞价低开{gap_pct:+.1f}%(≥2ATR) 低吸置空防接飞刀"]
    if gap_pct >= config.TP_G2:
        _mv(buy=s2, sell=s3)
        return [f"竞价高开{gap_pct:+.1f}% 低吸上移{s2:.2f}+高抛上移{s3:.2f}"]
    if gap_pct >= config.TP_G1:
        _mv(buy=s1)
        return [f"竞价高开{gap_pct:+.1f}% 低吸上移{s1:.2f}"]
    if gap_pct <= -config.TP_G2:
        _mv(buy=-s2, sell=-s2)
        return [f"竞价低开{gap_pct:+.1f}% 低吸/高抛下移{s2:.2f}降预期"]
    if gap_pct <= -config.TP_G1:
        _mv(buy=-s1)
        return [f"竞价低开{gap_pct:+.1f}% 低吸下移{s1:.2f}"]
    notes = []
    if vr is not None and vr >= config.TP_VR_TH:
        _mv(sell=s1)
        notes.append(f"平开{gap_pct:+.1f}%但竞价放量(vr={vr:.1f}) 高抛上移{s1:.2f}")
    return notes or [f"竞价平开{gap_pct:+.1f}% 不调整"]


def _ml_news_note(code, now):
    """v3(2026-11-16 因子E): 注入SVM今日方向+隔夜新闻雷达评分到计划说明
    仅展示参考(不改价位); 实际闸门在交易层ttrade_v3生效"""
    parts = []
    td = now.strftime("%Y-%m-%d")
    try:
        from ttrade import ml_guide
        p = ml_guide.day_pred(code, td) \
            if getattr(config, "V3_ML_EN", False) else None
        if p == 1:
            parts.append("ML今日看涨(正T为主,已禁反T卖)")
        elif p == 0:
            parts.append("ML今日看跌(反T为主,已禁正T+回补)")
    except Exception:
        pass
    try:
        from ttrade import news_radar
        sc, hits = news_radar.score_at(code, td)
        if hits:
            parts.append(f"隔夜新闻{('+' + str(sc)) if sc > 0 else sc}分({hits}条)")
    except Exception:
        pass
    return parts


def generate_daily_plan(now=None, dry=False):
    """9:26 入口: 四因子重算并覆盖写内部 t_plan.csv; 返回说明汇总list
    时点=9:26:00(集合竞价9:25撮合完, 行情快照~9:25:30稳定, 9:26数据最完整)"""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:
        return []
    # 等待竞价结果(9:25:00撮合完成, 9:26整取数最稳定):
    # 盘前 f2=0.0 会被误判"跳空极宽"导致低吸区被误置空
    ready = datetime.time(9, 26, 0)
    if not dry and now.time() < ready and now.hour == 9:
        wait = (datetime.datetime.combine(now.date(), ready) - now).total_seconds()
        print(f"[t_plan] 等待竞价结果 {wait:.0f}s ...")
        import time as _t
        _t.sleep(max(wait, 0))
        now = datetime.datetime.now()
    _seed_import()
    rows = _load_rows()
    if not rows:
        print("[t_plan] 无计划可生成(空票池)")
        return []
    mkt_q = {}
    try:
        mkt_q = quotes.fetch_quotes([config.REGIME_INDEX_ALIAS])
    except Exception as e:
        print("[t_plan] 大盘竞价获取失败(跳过因子B):", e)
    mkt = mkt_q.get(config.REGIME_INDEX_ALIAS) or (next(iter(mkt_q.values())) if mkt_q else None)
    mkt_pct = mkt.get("pct") if mkt else None
    all_notes, generated = [], 0
    for k, row in enumerate(rows):
        code, name = row["code"], row.get("name") or row["code"]
        # 原说明剔除前次自动生成段(防每日累积膨胀), 只保留用户手工注释
        old_parts = [p for p in (row.get("说明") or "").split(" | ")
                     if p.strip() and not p.strip().startswith(
                         ("9:25自动生成(", "9:26自动生成("))]
        old_note = " | ".join(old_parts)
        notes = []
        try:
            bars = quotes.fetch_kline_bars(code, klt=101, lmt=60)
            if bars and bars[-1]["dt"][:10] == now.strftime("%Y-%m-%d"):
                bars = bars[:-1]           # 9:26时点丢弃今日forming bar(指标只用昨日收盘)
            ind = build_ind(bars)
            i = len(bars) - 1
            atr = ind["atr"][i] or 0
            last_close = bars[i]["close"]
            notes += _base_from_ind(row, ind, i)                       # 因子A
            notes += _mkt_shift(row, mkt_pct, atr)                     # 因子B
            notes += _gap_fine_shift(row, code, atr, last_close, now)  # 因子C(9:26多档细分)
            notes += _news_shift(row, code, atr)                       # 因子D
            notes += _ml_news_note(code, now)                          # 因子E(v3参考)
        except Exception as e:
            print(f"[t_plan] {name}({code}) 生成异常, 保留原价位:", e)
            notes = [f"生成异常保留原价位: {e}"]
        if notes:
            generated += 1
            row["说明"] = "9:26自动生成(" + "; ".join(notes) + ")" \
                + (" | " + old_note if old_note else "")
        all_notes.append((code, name, notes))
    if dry:
        print(f"[DRY] t_plan 生成 {generated}/{len(rows)}只有调整, 大盘竞价={mkt_pct}%")
        for c, n, notes in all_notes:
            print(f"  {c} {n}:")
            for x in notes:
                print(f"    - {x}")
        return all_notes
    auction._rows_to_plan_file(rows)
    _push_report(all_notes, rows, generated, mkt_pct)
    print(f"[t_plan {now.strftime('%H:%M:%S')}] {generated}/{len(rows)}只有调整, "
          f"已写入 {config.T_PLAN_CSV}")
    return all_notes


def _push_report(all_notes, rows, generated, mkt_pct):
    """9:26 计划推送: HTML彩色表格(深蓝灰底表头, 低吸绿/高抛红/止损橙)"""
    def _num(v):
        return "—" if v is None else f"{v:.2f}"

    def _zone(lo, hi, color):
        if lo is None:
            return notify.colored("置空", config.C_GRAY)
        return notify.colored(f"{_num(lo)}~{_num(hi)}", color)

    mkt_txt = ""
    if mkt_pct is not None:
        mc = config.C_RED if mkt_pct > 0 else (
            config.C_GREEN if mkt_pct < 0 else config.C_GRAY)
        mkt_txt = f" · 大盘竞价{notify.colored(f'{mkt_pct:+.2f}%', mc)}"
    trows = []
    for code, name, notes in all_notes:
        row = next((r for r in rows if r["code"] == code), {})
        note_html = ("<span style='color:#888888;font-size:11px;'>"
                     + "<br>".join("• " + x for x in notes) + "</span>") \
            if notes else "<span style='color:#bbbbbb;'>—</span>"
        trows.append([
            code, name,
            _zone(row.get("buy_low"), row.get("buy_high"), config.C_GREEN),
            _zone(row.get("sell_low"), row.get("sell_high"), config.C_RED),
            notify.colored(_num(row.get("stop_loss")), config.C_ORANGE),
            _num(row.get("min_spread")),
            row.get("T类型") or "—",
            note_html,
        ])
    content = (notify.sec(f"t_plan 自动生成 · {generated}/{len(rows)} 只有调整{mkt_txt}")
               + notify.table(["代码", "名称", "低吸区", "高抛区", "止损线",
                               "T价差", "T类型", "调整说明"], trows))
    try:
        notify.send("📋 9:26 t_plan 自动生成", content)
    except Exception as e:
        print("[t_plan] 推送失败:", e)


if __name__ == "__main__":
    import sys
    generate_daily_plan(dry="--dry" in sys.argv or "--test" in sys.argv)
