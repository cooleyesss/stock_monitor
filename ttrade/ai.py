# -*- coding: utf-8 -*-
"""DeepSeek AI节点分析(做T信号守门)"""
import requests
from ttrade import config_ttrade as config
from common import quotes
from ttrade import account
from ttrade import vote


def _fmt_price(p):
    s = f"{p:.2f}"
    return s if float(s) == round(p, 6) else f"{p:.3f}"


def deepseek_analyze(state, code, plan, q, side, lot, ind, now, dry=False,
                     eval_res=None):
    """调用DeepSeek分析做T节点(含15分钟线MACD+RSI+量化投票明细);
    返回 {ok, verdict: execute/wait, text}
    同时累计 state["today_ai"] 统计; AI失败按原策略执行(不因AI故障漏单)
    """
    if not config.DEEPSEEK_ENABLED:
        return None
    ind = ind or {}
    name = plan.get("name") or state["positions"].get(code, {}).get("name", code)
    pos = state["positions"].get(code, {})

    def _net_sell(st, c):
        led = st.get("cum_ledger", {}).get(c) or {}
        return int(led.get("sq", 0) - led.get("bq", 0))

    trades_txt = "无" if not state["today_trades"] else "; ".join(
        f"{t['time'][:5]}{t.get('side_txt') or account.SIDE_LABEL.get(t['side'], t['side'])}"
        f"{t['qty']}股@{_fmt_price(t['price'])}"
        for t in state["today_trades"][-5:])
    plan_txt = []
    if "buy_low" in plan:
        plan_txt.append(f"低吸区{_fmt_price(plan['buy_low'])}~{_fmt_price(plan['buy_high'])}"
                        + (f"(需量比≤{plan.get('buy_vr_max')})" if plan.get("buy_vr_max") is not None else ""))
    if "sell_low" in plan:
        plan_txt.append(f"高抛区{_fmt_price(plan['sell_low'])}~{_fmt_price(plan['sell_high'])}"
                        + (f"(需量比≥{plan.get('sell_vr_min')})" if plan.get("sell_vr_min") is not None else ""))
    if plan.get("stop_loss"):
        plan_txt.append("止损线" + _fmt_price(max(plan["stop_loss"])))
    if plan.get("min_spread"):
        plan_txt.append(f"单次T价差要求≥{plan['min_spread']:.2f}元")
    pct = q.get("pct", 0) or 0
    vr = q.get("vr")
    signal = "🚀低吸买入" if side == "buy" else "🎯高抛卖出"
    macd = ind.get("macd")
    macd_txt = quotes.macd_state_text(macd)
    rsi, rsi6 = ind.get("rsi"), ind.get("rsi6")
    rsi_txt = quotes.rsi_state_text(rsi, ind.get("prsi"))
    rsi6_txt = f"{rsi6:.1f}" if rsi6 is not None else "无"
    vote_txt = vote.votes_text(eval_res, side) if eval_res else ""
    prompt = (
        f"你是一位专业的A股日内做T交易员，擅长量价分析与日内择时。"
        f"请基于以下实时数据，判断本次做T信号是否值得立即执行。\n\n"
        f"【标的】{name}({code})\n"
        f"【信号】{signal}\n"
        f"【时间】{now.strftime('%Y-%m-%d %H:%M')}\n"
        f"【实时行情】现价{_fmt_price(q['price'])} | 涨跌幅{pct:+.2f}% | "
        f"量比{vr:.2f}({'>1放量' if vr and vr > 1 else '≤1缩量'})\n"
        + (f"{vote_txt}\n" if vote_txt else "")
        + f"【技术指标(15分钟线)】MACD: {macd_txt} | RSI14: {rsi_txt} | RSI6: {rsi6_txt}\n"
        f"【做T计划】{' | '.join(plan_txt)}\n"
        f"【持仓】{pos.get('shares', 0)}股 | 成本{_fmt_price(pos.get('cost', 0))}\n"
        f"【今日已做T】{trades_txt}\n"
        f"【未回补净卖出】{_net_sell(state, code)}股"
        f"(配对T: 净卖出达标前高抛受限, 低吸回补优先)\n"
        f"【本次计划股数】{lot}股\n"
        f"【账户】可用现金{state['cash']:,.2f}元\n\n"
        f"分析要点：量价配合、MACD动能、RSI超买超卖位置、日内位置(距低吸/高抛/止损区间边界的空间)、"
        f"做T价差空间、动能延续性、各投票器分歧度与量化总分和人工计划的相互印证、持仓风险。\n"
        f"严格按以下格式输出，不要多余内容：\n"
        f"结论: 建议执行 或 建议观望\n"
        f"理由: 80字以内\n"
        f"风险: 40字以内")
    ai = _call(prompt, state, dry)
    return ai


def _call(prompt, state, dry=False):
    state["today_ai"]["asked"] += 1
    try:
        r = requests.post(config.DEEPSEEK_API_URL, timeout=config.DEEPSEEK_TIMEOUT,
                          headers={"Authorization": "Bearer " + config.DEEPSEEK_API_KEY,
                                   "Content-Type": "application/json"},
                          json={"model": config.DEEPSEEK_MODEL,
                                "messages": [{"role": "user", "content": prompt}],
                                "temperature": 0.3, "max_tokens": 300})
        r.raise_for_status()
        text = (r.json().get("choices") or [{}])[0] \
                   .get("message", {}).get("content", "").strip()
        if not text:
            state["today_ai"]["fail"] += 1
            return {"ok": False, "verdict": "unknown", "text": "AI返回为空"}
        first_line = text.split("\n")[0]
        if "观望" in first_line or "不建议" in first_line:
            verdict = "wait"
            state["today_ai"]["wait"] += 1
        elif "执行" in first_line:
            verdict = "execute"
            state["today_ai"]["execute"] += 1
        else:
            verdict = "execute"   # 解析失败按执行处理, 附原文供人工判断
            state["today_ai"]["execute"] += 1
        return {"ok": True, "verdict": verdict, "text": text}
    except Exception as e:
        state["today_ai"]["fail"] += 1
        return {"ok": False, "verdict": "unknown", "text": str(e)[:80]}
