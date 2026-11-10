# -*- coding: utf-8 -*-
"""量化子系统入口: 以持仓快照为期初, v2.2双模策略日频自动交易+复盘日报推送
与 T 子系统(main.py / sim_state.json)完全独立, 可同时运行
用法:
  python quant_daily.py --init [--force]   从持仓CSV(data/quant/holdings.csv)初始化子账户
  python quant_daily.py --dry              干跑: 推进+打印+dashboard, 不写状态不推送
  python quant_daily.py [--no-push]        收盘后日跑: 推进+记日志+推送复盘日报+生成dashboard
  python quant_daily.py --no-dashboard      同上但不生成dashboard_quant.html
建议每个交易日15:15由 stop.ps1(计划任务 StockMonitor_Stop)自动触发"""
import argparse
import datetime
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from quant import account, engine


def _wait_eod_settled():
    """自动收盘跑已改在15:15触发(数据早已落定, 本函数仅作安全网):
    若手动在15:00~15:03窗口内运行, 仍等待到15:03再取数——
    东财/腾讯日K在15:00收盘竞价后需数秒~数分钟合并收盘成交,
    15:01整取到的当日bar收盘价可能仍是竞价前价格(2026-10-21实测偏差+0.1~0.3%)"""
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return
    target = now.replace(hour=15, minute=3, second=0, microsecond=0)
    if datetime.time(15, 0) <= now.time() < datetime.time(15, 3):
        wait = (target - now).total_seconds()
        print(f"[EOD] 等待收盘数据落定 {wait:.0f}s (至15:03)...")
        time.sleep(max(wait, 0))


def main():
    p = argparse.ArgumentParser(description="量化独立账户(v2.1双模策略)")
    p.add_argument("--init", action="store_true", help="从持仓CSV初始化子账户")
    p.add_argument("--force", action="store_true", help="允许覆盖已有量化状态")
    p.add_argument("--dry", action="store_true", help="干跑(不写文件不推送)")
    p.add_argument("--no-push", action="store_true", help="只记录不推送微信")
    p.add_argument("--no-dashboard", action="store_true",
                   help="不生成 dashboard_quant.html")
    args = p.parse_args()
    _wait_eod_settled()                 # 收盘自动跑等待数据落定(手动跑不受影响)
    if args.init:
        import os
        if os.path.exists(account.STATE_PATH) and not args.force:
            print("量化状态已存在, 如需重建请加 --force (将清空策略状态重新起步)")
            return 1
        st = account.init_state()
        account.save_state(st, dry=args.dry)
        print("初始化完成" + ("(dry未落盘)" if args.dry else
                            f", 状态文件: {account.STATE_PATH}"))
        return 0
    return 0 if engine.run(dry=args.dry, push=not args.no_push,
                           dash=not args.no_dashboard) else 1


if __name__ == "__main__":
    sys.exit(main())
