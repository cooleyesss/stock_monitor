# -*- coding: utf-8 -*-
"""
监控+做T模拟盘一体化主程序
========================================
一个进程同时完成: 实时监控告警 + AI守门 + 模拟盘做T + 微信推送
- 交易时段(工作日 9:30-11:30, 13:00-15:00) 每 REFRESH_SECONDS 秒拉一次行情
- 信号触发(价格区间+量比确认) -> 一次DeepSeek分析 -> 一条微信推送:
    告警(区间/量比/MACD) + AI结论 + 成交详情(持股/现金变动) + 当日T汇总
    AI建议观望且守门开启 -> 不成交; AI失败 -> 按原策略执行; 止损纪律优先不走AI
- 每天15:15推送收盘战报(今日做T盈亏+累计盈亏+AI研判统计+账户概况)
  并追加 v2.1 双模策略次日信号(core/adaptive.py, 回测固化策略的实盘落地)
用法:
    python ttrade/main.py            正常运行(循环)
    python ttrade/main.py --test     干跑一轮(不写状态/不推送/不下单, 打印AI结论)
    python ttrade/main.py --test-eod 预览收盘报告(仅打印)
    python ttrade/main.py --eod      收盘日跑(stop.ps1于15:15触发, 幂等): 收盘战报+看板+双模信号
                              与监控进程是否存活无关, 保证每天必有日报
    python ttrade/main.py --reset    删除状态文件, 从持仓CSV重新初始化
    python ttrade/main.py --manual-sell <代码|名称> <价格> <股数>   手动同步真实卖出
    python ttrade/main.py --backtest [--bt-days 5] [--bt-codes 600613,...] [--bt-zone-only]
                              历史回测(最近≤5个交易日分时逐bar回放量化引擎)
"""
import os
import sys
import time
import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 支持 python ttrade/runner.py 直接运行: 把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ttrade import config_ttrade as config
from common import notify
from common import quotes
from common import textparse
from ttrade import state as tstate
from ttrade import account
from ttrade import strategy
from ttrade import vote
from core import adaptive   # 跨层调用: 15:15 收盘战报追加量化策略次日信号(见 do_eod)

DRY_RUN = "--test" in sys.argv
TEST_EOD = "--test-eod" in sys.argv
EOD_RUN = "--eod" in sys.argv
RESET = "--reset" in sys.argv
MANUAL_SELL = "--manual-sell" in sys.argv
BACKTEST = "--backtest" in sys.argv

if DRY_RUN:
    notify.DRY = True


def in_window(now):
    """工作日 9:30-11:30, 13:00-15:00"""
    if now.weekday() >= 5:
        return False
    t = now.time()
    am = datetime.time(9, 30) <= t <= datetime.time(11, 30)
    pm = datetime.time(13, 0) <= t <= datetime.time(15, 0)
    if config.LUNCH_BREAK:
        return am or pm
    return datetime.time(9, 30) <= t <= datetime.time(15, 0)


def seconds_to_next_window(now):
    """距下一个交易窗口开始的秒数(用于精确休眠); 窗口内返回0
    午休睡到13:00, 收盘后睡到下一个工作日9:30"""
    if now.weekday() >= 5:
        return config.IDLE_SLEEP_SECONDS
    t = now.time()
    if t < datetime.time(9, 30):
        target = datetime.datetime.combine(now.date(), datetime.time(9, 30))
    elif datetime.time(11, 30) < t < datetime.time(13, 0):
        target = datetime.datetime.combine(now.date(), datetime.time(13, 0))
    elif t >= datetime.time(15, 0):
        d = now.date() + datetime.timedelta(days=1)
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        target = datetime.datetime.combine(d, datetime.time(9, 30))
    else:
        return 0
    return max(1.0, (target - now).total_seconds())


def collect_codes(state, tplan):
    """需要拉行情的代码: 持仓(可ETF映射) + 做T计划"""
    codes = set()
    for key in state["positions"]:
        codes.add(config.ETF_CODE_MAP.get(key, key))
    codes.update(tplan.keys())
    return {c for c in codes if len(c) == 6 and c.isdigit()}


def _regime_of_today(now):
    """大盘门控(每轮算一次全股共享): 上证指数行情+分时VWAP; 失败按中性不拦截"""
    try:
        iq = quotes.get_index_quote()
        ib = quotes.get_bars_1m(config.REGIME_INDEX_ALIAS)
        return vote.regime_state(iq, vote.bars_of_day(ib, textparse.today_str(now)))
    except Exception as e:
        print("[大盘门控] 获取失败, 本轮按中性处理:", e)
        return None


def run_cycle(state, now, dry=False):
    tplan = tstate.load_t_plan()
    # t_plan随时更新: 每轮触发前检测文件变化, 变了就重置当日T量缓存, 确保用最新T量/区间
    try:
        plan_mtime = os.path.getmtime(config.T_PLAN_CSV)
    except OSError:
        plan_mtime = None
    if plan_mtime != state.get("plan_mtime"):
        state["plan_mtime"] = plan_mtime
        state["today_lot"] = {}
        if not dry:
            print(f"[{now.strftime('%H:%M:%S')}] 检测到做T策略更新, T量缓存已重置")
    codes = collect_codes(state, tplan)
    quotes_raw = quotes.fetch_quotes(codes)
    fresh = {c: q for c, q in quotes_raw.items()
             if q.get("ts") and datetime.datetime.fromtimestamp(q["ts"]).date() == now.date()}
    if not fresh:
        print(f"[{now.strftime('%H:%M:%S')}] 今日休市或无最新行情, 跳过本轮")
        return
    prices = {c: q["price"] for c, q in fresh.items()}
    state["last_prices"].update(prices)
    if not state.get("baseline"):
        state["baseline"] = {"total_asset": round(account.total_asset(state, prices), 2),
                             "set_at": now.strftime("%Y-%m-%d %H:%M")}
        print(f"[基准] 期初总资产设定为 {account.fmt_amt(state['baseline']['total_asset'])}")
        tstate.save_state(state, dry)
    # 大盘门控(全股共享, 失败=neutral不拦截)
    regime = _regime_of_today(now) if config.QUANT_ENABLED else None
    if regime and not dry:
        print(f"[大盘门控] {regime['reason']}")
    # 逐股信号检查(告警+AI+模拟盘一体)
    for code, plan in tplan.items():
        q = fresh.get(code)
        if q:
            strategy.check_signal(state, code, plan, q, now, dry, regime)
    # 状态行
    status = " ".join(
        f"{state['positions'].get(c, {}).get('name', c)}{account.fmt_price(q['price'])}"
        + (f"(量比{q['vr']:.2f})" if q["vr"] is not None else "")
        for c, q in fresh.items() if c in tplan)
    n, gross, fee, net = account.today_t_summary(state)
    print(f"[{now.strftime('%H:%M:%S')}] {status} | "
          f"现金{account.fmt_amt(state['cash'])} "
          f"总资产{account.fmt_amt(account.total_asset(state, prices))} | "
          f"今日T:{n}笔 净{account.fmt_pnl(net)}")
    tstate.save_state(state, dry)


def main():
    # 历史回测: 用近期1分钟数据回放量化投票引擎(独立于实盘状态)
    if BACKTEST:
        from ttrade import replay as backtest

        def _val(flag, default):
            i = sys.argv.index(flag)
            return sys.argv[i + 1] if i + 1 < len(sys.argv) else default
        days = int(_val("--bt-days", "5")) if "--bt-days" in sys.argv else 5
        codes = (_val("--bt-codes", "").split(",")) \
            if "--bt-codes" in sys.argv else None
        backtest.run(days=days, codes=codes,
                     zone_only="--bt-zone-only" in sys.argv)
        return

    if RESET and os.path.exists(config.STATE_PATH):
        os.remove(config.STATE_PATH)
        print("已删除旧状态文件, 将重新初始化")
    state = tstate.load_state()
    if not state:
        state = tstate.init_state()
        tstate.save_state(state)

    # 手动同步真实卖出
    if MANUAL_SELL:
        i = sys.argv.index("--manual-sell")
        args = sys.argv[i + 1:i + 4]
        if len(args) < 3:
            print("用法: python ttrade/main.py --manual-sell <代码|名称> <价格> <股数>")
            return
        strategy.manual_sell(state, args[0], float(args[1]), int(args[2]))
        return

    if TEST_EOD:
        strategy.do_eod(state, datetime.datetime.now(), dry=True)
        adaptive.eod_scan_and_notify(state, tstate.load_t_plan(), dry=True)  # 跨层调用: 量化策略
        return

    if EOD_RUN:
        # 收盘日跑(stop.ps1于15:15触发): 收盘战报+dashboard_ttrade+双模信号,
        # 与监控进程是否存活无关; 幂等: eod_sent==今日则跳过(监控兜底已发过)
        now = datetime.datetime.now()
        td = textparse.today_str(now)
        if now.weekday() < 5 and now.time() < datetime.time(15, 0):
            print("15:00前不触发收盘日跑(防盘中误发), 预览请用 --test-eod")
            return
        if state.get("eod_sent") == td:
            print(f"{td} 收盘战报已发送, 跳过(幂等)")
            strategy.eod_dashboard_only(state, now)   # 战报幂等, 但看板/归档仍需日更
            return
        if strategy.do_eod(state, now):
            try:
                adaptive.eod_scan_and_notify(state, tstate.load_t_plan())
            except Exception as e:
                print("[双模策略信号] 扫描失败:", e)
        return

    plan0 = tstate.load_t_plan()
    print(f"监控+模拟盘一体化启动: 持仓{len(state['positions'])}只 "
          f"现金{account.fmt_amt(state['cash'])} 做T计划{len(plan0)}只")
    ai_mode = ("关闭" if not config.DEEPSEEK_ENABLED else
               ("非对称守门(仅拦高抛, 低吸观望放行)" if (config.DEEPSEEK_GATEKEEP
                                            and config.GATEKEEP_SELL_ONLY) else
                ("守门模式(AI观望则不成交)" if config.DEEPSEEK_GATEKEEP else "仅参考")))
    quant_mode = ("多策略投票共振"
                  + (" [观察模式:共振通过不成交]" if config.STRATEGY_DRY_RUN else "")
                  if config.QUANT_ENABLED else "关闭(旧逻辑)")
    strat_mode = ("v2反T编排(网格回补+环境过滤+收盘兜底)" if config.STRAT_V2_EN
                  else "v1区间+量化投票+AI守门")
    print(f"刷新{config.REFRESH_SECONDS}秒 | 交易时段 工作日9:30-11:30/13:00-15:00 | "
          f"15:15收盘战报 | 止损模式:{config.STOP_LOSS_MODE} | "
          f"做T股数=当日持仓1/3 | AI(DeepSeek): {ai_mode} | "
          f"量化引擎: {quant_mode}")
    print(f"做T策略: {strat_mode}")

    if DRY_RUN:
        print("── DRY RUN: 单轮干跑, 不下单/不写状态/不推送 ──")
        run_cycle(state, datetime.datetime.now(), dry=True)
        return

    last_snapshot = None
    last_plan_mtime = None
    last_auction_date = None
    while True:
        now = datetime.datetime.now()
        try:
            tstate.rollover(state, now)
            # 9:26 集合竞价自动更新 t_plan (每日仅触发一次)
            if (now.weekday() < 5 and last_auction_date != now.date()
                    and datetime.time(9, 25) <= now.time() <= datetime.time(9, 30)):
                try:
                    if getattr(config, "TPLAN_GEN_EN", False):
                        from ttrade import tplan_gen
                        tplan_gen.generate_daily_plan(now=now, dry=DRY_RUN)
                    else:                      # 回退: 仅竞价微调模式
                        from ttrade import auction as au
                        au.auto_update_tplan(now=now, dry=DRY_RUN)
                except Exception as e:
                    print("[t_plan生成] 失败(不影响主流程):", e)
                last_auction_date = now.date()
            # 收盘报告: 工作日15:15后触发一次(正常由stop.ps1日跑承担, 此处为监控兜底)
            if now.weekday() < 5 and now.time() >= datetime.time(15, 15) \
                    and state.get("eod_sent") != textparse.today_str(now):
                strategy.do_eod(state, now)
                # 收盘战报后追加: 跨层调用量化策略(v2.2双模次日信号), 失败不影响主流程
                try:
                    adaptive.eod_scan_and_notify(state, tstate.load_t_plan())
                except Exception as e:
                    print("[双模策略信号] 扫描失败:", e)
            if in_window(now):
                # t_plan外部更新检测 -> 推送新计划摘要
                try:
                    plan_mtime = os.path.getmtime(config.T_PLAN_CSV)
                except OSError:
                    plan_mtime = None
                if last_plan_mtime is None:
                    last_plan_mtime = plan_mtime
                elif plan_mtime != last_plan_mtime:
                    last_plan_mtime = plan_mtime
                    notify.send("🔄 做T策略已更新", strategy.format_t_plan(tstate.load_t_plan()))
                run_cycle(state, now)
                # 常规快照(默认关闭)
                if config.SNAPSHOT_MINUTES > 0 and (
                        last_snapshot is None or
                        (now - last_snapshot).total_seconds() >= config.SNAPSHOT_MINUTES * 60):
                    tplan = tstate.load_t_plan()
                    notify.send(f"📊 持仓快照 {now.strftime('%H:%M')} (🔴红涨🟢绿跌)",
                                strategy.format_snapshot(quotes.fetch_quotes(
                                    collect_codes(state, tplan))))
                    last_snapshot = now
                time.sleep(config.REFRESH_SECONDS)
            else:
                # 收盘报告未发出(失败重试中) -> 短睡等待重试; 否则精确睡到下一窗口
                if now.weekday() < 5 and now.time() >= datetime.time(15, 0) \
                        and state.get("eod_sent") != textparse.today_str(now):
                    wait = 300
                else:
                    wait = min(seconds_to_next_window(now), 1800)
                print(f"[{now.strftime('%H:%M:%S')}] 非交易时段, "
                      f"休眠{int(wait)}秒...")
                time.sleep(wait)
        except Exception as e:
            print("异常(将继续重试):", e)
            time.sleep(config.REFRESH_SECONDS)


if __name__ == "__main__":
    main()
