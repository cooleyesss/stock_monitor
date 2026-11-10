================================================================
  股票做T监控系统 (监控 + AI守门 + 模拟盘 一体化)
================================================================

一、程序说明
----------------------------------------------------------------
单进程程序, 启动后自动完成:
  - 交易时段(工作日 9:30-11:30 / 13:00-15:00)每60秒拉取实时行情
  - 价格到达做T位置 -> 必推送微信(附DeepSeek当时研判):
      量比达标 -> AI建议执行则模拟盘成交 + 推送成交详情
      量比未达标 -> 推送"到达T位"提醒(不做T, 量比达标后自动触发)
                  同股票同方向若AI结论与上次一致, 自动免打扰不重复推送
      跌破止损线 -> 直接按纪律清仓(不走AI)
  - 做T频率控制(防T区横盘反复成交): 每股每方向每日最多2次成交
      + 同方向两次成交间隔至少30分钟(参数在ttrade\config_ttrade.py: T_MAX_PER_DAY/T_COOLDOWN_MIN)
  - T+1规则: 当日买入部分当日不可卖(做T高抛受限;
      止损清仓/手动同步真实卖出豁免), 次日开盘自动解禁
  - mystock_test.csv 为真实持仓母表(只读): 由你(截图工具)或 python holdings_editor.py
      网页维护; 编辑器保存时可"一键分发"到 data\ttrade\holdings.csv 与
      data\quant\holdings.csv, 两子系统各自只读自己那份, 均不读/写母表
  - 每天15:00推送收盘战报到微信(今日做T盈亏/累计盈亏/账户概况)
      并追加 v2.1 双模策略次日信号(趋势买/超跌买/破位卖/保险丝, 见 core/adaptive.py)
      同时在 report\ttrade\dashboard_ttrade.html 生成 T 专属看板(与量化看板互不覆盖)
  - 节假日/非交易时段自动休眠, 无需人工干预

二、启动程序
----------------------------------------------------------------
方式1(推荐): 双击 start.bat
方式2:       命令行执行  python ttrade\main.py
方式3:       后台运行    powershell -File start.ps1

启动后:
  - 程序在后台隐藏运行, 日志写入 monitor.log (错误看 monitor_err.log)
    ⚠ monitor.log 每次 start 都会被重定向覆盖(只保留本次运行), 需留存历史请先另存
  - 做T模拟盘数据统一放在 data\ttrade\ 目录:
      holdings.csv   T 子系统持仓文件(由母表一键分发; --reset 时作为初始化源)
      sim_state.json 状态(持仓/现金/做T台账), 删除会导致重新初始化
      trades.log     成交日志(可读格式): 每笔成交一行,
                     每天15:00收盘后自动追加当日日内T盈亏记录
      sim_trades.csv 成交流水表格(可用Excel打开做统计)

三、终止程序
----------------------------------------------------------------
方式1(推荐): 双击 stop.bat
方式2:       powershell -File stop.ps1

四、常用命令
----------------------------------------------------------------
干跑测试(不下单/不推送/不改状态):
    python ttrade\main.py --test

预览收盘报告格式(仅打印, 不推送):
    python ttrade\main.py --test-eod

手动同步真实账户的卖出操作(如真实账户清仓了某股):
    python ttrade\main.py --manual-sell <代码或名称> <价格> <股数>
    例: python ttrade\main.py --manual-sell 600000 5.00 1000

重置模拟盘(删除状态, 从 data\ttrade\holdings.csv 重新初始化):
    python ttrade\main.py --reset
    注意: 会清空做T盈亏记录, 慎用! 重置前请确认已用 holdings_editor.py 一键分发过母表

五、参数调整
----------------------------------------------------------------
参数按三层分开放, 改动位置取决于影响范围:
    config_common.py    公共: 路径/数据目录/行情源/费率/PushPlus/DeepSeek/颜色
    ttrade\config_ttrade.py    T 子系统: 下列做T专属参数
    config_quant.py     量化子系统: 智能选股 PICK_* / 冰点 ICE_* / 恐慌 PANIC_*

★个性化配置(首次使用必读): 个人信息统一放 config_local.py(不入库), 复制
    config_local_template.py 建立, 各键均有中性默认, 不建也能跑:
    - PUSHPLUS_TOKEN   微信推送令牌(留空=不推送)
    - DEEPSEEK_API_KEY AI守门Key(留空=自动禁用AI, 信号照常)
    - T_EXCLUDE_CODES / QT_EXCLUDE_CODES  剔除名单(不入持仓/不做T/不参与信号)
    - BT_CAPITAL       回测期初资金锚点 / BT_EXCLUDE_CODES 回测抛弃标的
    - CACHE_DIR 等机器路径 / LAB_BASE_* lab盘前实验基准(持股/成本)
  真实持仓维护: mystock_test.csv(母表, 不入库) 经 holdings_editor.py 编辑并一键分发
  到两子系统; 持仓/成本/成交流水/日报/看板等个人数据均在 data\ 与 report\ (不入库)

  - REFRESH_SECONDS     行情刷新间隔(默认60秒)
  - DEEPSEEK_GATEKEEP   AI守门开关(True=AI观望则不成交)
  - STOP_LOSS_MODE      止损模式(clear=清仓 / t_only=只卖T仓 / alert=仅提醒)
  - LOT_RATIO           每次做T股数比例(t_plan填了"T量"列则优先按T量执行)
  - KLINE_KLT           技术指标K线周期(默认15分钟, MACD+RSI)
  - 做T计划文件: D:\trae_projects\t_plan.csv (每轮自动重读, 改完即时生效)
      列(按表头名识别, 顺序可调): 代码,名称,T类型,低吸下限,低吸上限,低吸量比上限,
      高抛下限,高抛上限,高抛量比下限,止损线,单次T价差下限,T量,说明
      - "T量"列有值 -> 做T股数按该值(百股取整), 不填则按持仓*LOT_RATIO
      - "单次T价差下限" -> 提供给AI研判参考(不做硬性拦截)

  防空仓/促回补机制(config_ttrade.py):
  - PAIR_T=True 配对T: 未回补净卖出(累计卖出-买入)≥单次T量时冻结高抛,
    需先低吸回补; 触发时推送"⛔高抛·配对拦截"; 止损清仓不计入台账
  - T_DWELL_MIN=10 低吸超时豁免: 价格在低吸区停留≥10分钟视为缩量企稳,
    量比超标也执行(AI守门仍在), 推送中标注"⏱停留N分钟豁免"

六、修改代码后生效方法
----------------------------------------------------------------
先 stop.bat 停止, 再 start.bat 启动 (直接改代码不会热加载)

七、目录结构与策略迭代(回测)
----------------------------------------------------------------
系统分三层: 两条互相独立的子系统(T / 量化) + 一个公共系统.
根目录只放公共文件(启停脚本/监控日志/真实持仓母表), 各子系统的代码各自成包, 数据各自入库:

    config_common.py    公共配置: 路径/数据目录/母表与两份 holdings 路径/行情源/费率/
                        PushPlus/DeepSeek/颜色
    config_quant.py     量化子系统配置: 智能选股 PICK_*/观察池/冰点 ICE_*/恐慌 PANIC_*
    common/             公共基础层: notify(推送) quotes(东财行情+板块/题材接口+腾讯回退)
                        fees(费用模型) textparse(日期/金额解析)
    core/               公共决策核: indicators(指标库) adaptive(v2.5双模实盘信号)
                        —— T/量化/回测/lab 四方共用
    ttrade\             ★T 子系统(独立, 与量化完全隔离, 2026-11-05收拢为垂直包):
                        config_ttrade(T配置) main(入口, start.ps1/stop.ps1指向它)
                        state(状态读写) ledger(做T盈亏) vote(投票决策核)
                        strategy(做T信号调度) account(做T模拟盘账户) ai(DeepSeek守门)
                        runner(监控+做T主循环) auction(9:26竞价微调) tplan_gen(9:25重算t_plan)
                        ttrade_v2(v2反T策略) replay(≤5日分时回放) bt_ttrade(流水回测)
                        dashboard(T专属看板 dashboard_ttrade.html)
    quant\              ★量化子系统(独立): account(状态/流水) engine(日频编排+日报)
                        picker(智能选股) dashboard(量化看板 dashboard_quant.html)
    backtest/           公共回测层: daily(半年日线回测+调优+看板)
    lab/                策略实验室(垂直隔离): base(回放器) minute(分钟级基类) runner(对比运行器)
                        strategies/(实验策略注册表) tools/(一次性验证脚本)
    quant_daily.py      量化子系统入口(15:15 由 stop.ps1 触发)
    run_backtest.py     回测独立入口     run_lab.py  实验室入口
    holdings_editor.py  持仓母表+观察池网页编辑器(保存时可一键分发到两子系统)
    monitor.log/monitor_err.log   监控进程日志(公共)
    mystock_test.csv   真实持仓母表(只读, 由你+编辑器维护): 经"一键分发"生成
                       data\ttrade\holdings.csv 与 data\quant\holdings.csv,
                       两子系统只读各自那份, 任何时候都不读/写母表
    report\            看板HTML目录(2026-10-28起 T/量化分子目录):
                       ttrade\dashboard_ttrade.html(T, 每日15:15日跑生成, 归档ttrade\history\)
                       quant\dashboard_quant.html(量化, 每日15:15生成, 归档quant\history\)
                       dashboard_backtest.html(回测) / dashboard_lab.html(实验)在根目录
    data\ttrade\       T 数据: holdings.csv / sim_state.json / sim_trades.csv / trades.log
    data\quant\        量化数据: holdings.csv / quant_state.json / quant_trades.csv /
                       quant_trades.log / watchlist.csv / quant_run.log
    (路径全部由 config_common.py 的 TT_DATA_DIR/QT_DATA_DIR/REPORT_DIR 派生, 不要写死路径)

数据源(K线, config_common.py):
    东财(push2his)优先 -> 连续失败3次(EM_FAIL_BREAK)则本进程内熔断900秒(EM_DOWN_SEC),
    期间直接走腾讯(web.ifzq.gtimg.cn / ifzq.gtimg.cn, 实测稳定; 日K前复权与东财口径一致)
    熔断到期只探1轮东财, 失败立即续熔断; 腾讯不提供成交额, 用 量(手)*100*均价 近似
    ⚠ 板块指数(90.BKxxxx)腾讯无对应 -> 东财故障期间智能选股的板块动量会取不到

单独启动回测迭代策略(不影响监控进程, 可同时进行):
    python run_backtest.py                  半年回测+个股操作点, 生成 report\dashboard_backtest.html
    python run_backtest.py --no-dashboard   只跑回测不生成HTML
    python run_backtest.py --optimize       加跑参数随机搜索调优(结果存 CACHE_DIR\best_params.json)
    回测数据缓存: CACHE_DIR\bt_daily_cache.json (删之则重新拉取)
    策略迭代改 core/indicators.py(指标)与 backtest/daily.py(规则), 达标后
    将规则同步进 core/adaptive.py 即可落地到每日收盘信号推送
    回测期初资金锚点 BT_CAPITAL(个性化, config_local.py 覆盖, 默认100万):
    全仓整百取整在现金边界会量化跳变(一笔成交与否可翻转收益),
    跨日迭代对比需稳定基准, 按自己账户口径在 config_local.py 配置

策略规则版本(个股独立层, backtest/daily.py run_ps_stock ⇔ core/adaptive.py decide):
    v2.1 双模: 趋势腿(收盘>MA20&MA5>MA10&收盘>MA60&MA20上行&10日动量>2%→满仓, 破MA10卖)
        + scalp超跌腿(破布林下轨收回+RSI14<40+收阳+5日跌>-12%+开盘≥信号低点+0.3ATR→半仓,
        中轨止盈/破信号低点止损冷却3日/8日超时) + 恐慌保险丝(暴跌/放量长阴次日禁scalp)
    v2.2 冰点左侧(2026-10-14回测采纳, config ICE_LEFT_EN=True): 市场连续冰点
        (上证指数连跌3日 或 近3日累跌≤-3%) + 个股超卖未崩(RSI14<35 且 5日跌幅≥-10%
        且当日仍收跌) + 开盘≥信号低点+0.3ATR → 半仓左侧买入, 出场复用scalp机制, 受保险丝约束
        验证: 27组参数网格, RSI35/连跌≥3日为稳定正增量平台(Δ+0.8~+4.6%), 本参数点
        6股合计Δ+3.60%(+92.35%→+95.95%); 样本仅4笔且康缘单笔-1.1pp; RSI放宽至40
        全网格大幅负增量勿放宽; 回放验证decide⇔run_ps_stock完全一致
    v2.3 分批建仓(2026-10-21 lab池采纳后, 同日量化池回测反转, v2.4已关闭): 趋势腿
        首笔50%+站MA5次日加仓; lab池+0.96pp, 但量化池(8标的/119日)分批在趋势股上
        踏空(德才-3.3pp), 满仓+阶梯止盈全面占优(14.61% vs 12.35%), 经用户确认切回满仓
        (代码保留, config SCALE_IN_EN=False)
    v2.4 阶梯移动止盈(2026-10-21 量化池回测采纳+用户确认, config DYN_TRAIL_EN=True):
        持仓期最高收盘回撤达带宽→清仓(与MA10双退出, 先到先卖); 带宽随浮盈阶梯放宽:
        浮盈<15%→6% / 15~30%→10% / ≥30%→15%(小盈快跑锁利润, 大盈放宽让利润奔跑;
        正常行情MA10先触发, 急速闪崩时止盈线提前兜底)
        验证: 量化池8标的/119日, v2.2等权13.68%→14.61%(+0.93pp), 最大回撤6.2%→5.5%,
        受益欣旺达-3.74%→+2.35%/德才+12.62%→+14.00%; 5组邻域参数(5/9/14~6/11/15)
        结果完全一致=稳定平台非过拟合; replay验证decide⇔run_ps_stock零漂移(8/8)
    v2.5 阶梯止损(2026-10-21 量化池回测+用户确认, config DYN_STOP_EN=True):
        与v2.4阶梯止盈同时在场(止盈→止损→MA10依次判定, 先到先卖); 止损线=入场价×档位线,
        档位按"持仓期最高浮盈"取档且只上移不下移: 曾达50%→锁+10% / 曾达25%→锁+3%保本 /
        未达25%→-12%灾难线(只兜尾部); 正常行情由止盈/MA10先触发, 止损仅防极端闪崩
        验证: 第七轮共存网格(修正回测bug: 止损elif吞MA10退出后重测), 固定止损-8~-15%与
        5组阶梯止损全部与v2.4完全一致(+14.61%/回撤5.5%/止损零触发)=零成本纯保险;
        replay验证decide⇔run_ps_stock零漂移(8/8)
    ✗ 波动率自适应阶梯阈值(2026-10-21 用户提出个股幅度不同应逐股适配, 回测否决留档):
        止盈带宽/灾难止损按入场日ATR%缩放(k1/k2/k3×ATR, 27组网格×止损s∈{4,5,6}),
        10标的池全部组合等权+39.15% ≤ 固定6/10/15%基准+39.90%(Δ-0.74pp, 欣旺达-6.1pp/
        德才-1.3pp, 其余全平); 根因: 本池趋势退出九成由MA10完成, 止盈带宽处宽平台,
        逐股适配只增复杂度无正增量 —— 维持v2.5统一阈值, 不逐股调参
    ✗ 卖出端其他动态规则(2026-10-21 quant池五轮网格否决留档): RSI超买减仓/清仓(-1~
        -5pp, 趋势股超买后往往续涨)、MACD死叉叠加/替代MA10(-1.5pp/零触发)、固定trail
        (≥10%从不触发, <10%过早砍仓)、保本线/止盈减仓/分批减仓、MA5/MA20/布林中轨
        (=MA20)退出 —— 均零/负增量; 本池阿尔法=拿住趋势, 卖点跟随价格(最高收盘回撤)
        优于跟随衍生指标(RSI/MACD)
    ✗ 日内恐慌V反抄底(同日回测否决, PANIC_BUY_EN=False留备查): 盘中暴跌≥5%且收盘
        收复≥50%且放量→豁免疫丝抄底; 18组阈值网格所有可触发组合均负增量(-33%~-0.5%),
        V反多为下跌中继, 豁免疫丝等于拆保护
    ✗ 大盘/板块门控(2026-10-21 用户提出"纳入大盘走势与个股所属板块走势", 第八轮调研+
        14变体回测全部否决, 最终按用户确认落地为信息层):
        调研: Faber(2007)均线择时=风控工具非收益工具(削回撤不增益); Moskowitz-Grinblatt
        (1998)行业动量有效但为中期截面因子; 深交所研究所: 均线择时有效性随市场环境漂移;
        湘财证券: 择时+行业轮动组合框架 —— 共识: 择时降波动, 不稳定增益
        回测: run_ps_stock加实验参数(mkt_ma入场门/mkt_exit大盘破位退/mkt_dn下行软门/
        mkt_half弱市半仓/bk_ma板块门/bk_mom板块动量门/bk_exit板块破位退, 生产不传=不变),
        119交易日8标的全板块对齐: v2.5基准+14.99%/回撤5.9%; 14变体无一跑赢(大盘门
        -8.2~-11.1pp、板块门-0.6~-2.3pp、弱市半仓-6.1pp); 回撤改善真实(→2.4~2.8%)但
        收益代价过高; 根因: 新华文轩+79.94%整段发生在上证MA20下方的结构性行情,
        大盘门恰好挡掉最大盈利腿; 与10-20用户撤回PICK_RESON_EN、历史4轮RS否决一致
        落地(信息层, 用户确认): 大盘均线状态(收vs MA10/20/60)+持仓/挂单标的所属行业
        板块状态(板块收vs MA20+5日动量)进日报"市场环境"节与看板同名区块, 仅辅助人工
        决策, 引擎v2.5不动; 板块链路: f100行业名(永久缓存, clist限流时stock/get f127
        兜底) -> 行业名→BK映射(全量分页7天缓存, 失败回退种子缓存; 仍查不到时走东财
        suggest 接口按板块名直查BK) -> 板块指数日K(fetch_board_bars, MA20+动量),
        全链路静默降级不影响主流程; 板块榜clist被限流时行业查询改走 push2 stock/get
        f127(http直连绕开系统代理故障)

八、量化子系统(quant, v2.5双模策略日频自动交易)
----------------------------------------------------------------
以持仓快照(data\quant\holdings.csv, 由母表一键分发)为期初的独立纸面账户, 与做T模拟盘
(sim_state.json)完全隔离, 全部标的统一使用回测固化的v2.5双模策略(见第七节规则版本):
  - 每股一个独立子账户: 初始=快照持股(按趋势腿)+现金0, 买入资金只来自本账户卖出回款
  - 严格无未来函数: 收盘判定信号, 次日盘中双窗口择机执行(9:30~10:00/14:30~14:55,
    见下方"双窗口择机执行"), 盘中runner未跑时按开盘价兜底(scalp需价≥信号低点+0.3ATR确认)
  - 每天收盘后跑一次: 有成交先推"🤖 量化独立账户·成交"操作提示(与日内T的
    🚀🎯个股提示区分) + 推送复盘日报(账户概况/持仓明细/明日信号/成交/选股/市场环境信息层)
  - 漏跑多天自动逐日补跑(成交用信号日+1的历史开盘价); 重复运行幂等不重复记账
用法:
    python quant_daily.py --init [--force]   从 data\quant\holdings.csv 初始化子账户
                                             (已完成, 慎用 --force; 重建前请先分发母表)
    python quant_daily.py --dry              干跑: 推进+打印+生成dashboard, 不写状态不推送
    python quant_daily.py [--no-push]        收盘后日跑: 推进+记日志+推送复盘日报+生成dashboard
    python quant_daily.py --no-dashboard     同上但不生成dashboard_quant.html
每天15:15自动执行: 计划任务 StockMonitor_Stop -> stop.ps1
    ①停止监控进程
    ②T侧日跑 ttrade\main.py --eod: 收盘战报推送+dashboard_ttrade.html(含history归档)
      +双模次日信号; 幂等(eod_sent=今日则跳过), 与监控进程是否存活无关,
      保证每天必有T侧日报; 留痕 data\ttrade\ttrade_run.log
    ③跑 quant_daily.py(推进+推送复盘日报+生成dashboard_quant.html)
    ④运行输出分别追加留痕到 data\ttrade\ttrade_run.log 与
      data\quant\quant_run.log(无人值守时排查看这里)
每天9:25自动执行: 计划任务 StockMonitor_Start -> start.ps1 同时拉起两个进程:
    ①ttrade\main.py(T侧监控) ②quant_runner.py(量化盘中双窗口runner, 15:00自行退出)
数据文件(均在 data\quant\ 目录):
    holdings.csv       量化子系统持仓文件(由母表一键分发; --init 时作为初始化源)
    quant_state.json   子账户状态(现金/持股/腿/hold_d/冷却/挂单/已处理日期/逐日权益)
    quant_trades.csv   成交流水表格    quant_trades.log  成交+日报可读日志
    watchlist.csv      手动观察池    quant_run.log  15:15自动运行留痕
看板(在 report\ 目录):
    dashboard_quant.html  每日收盘生成的账户看板(账户概况/权益曲线/持仓标的日K与
                          买卖操作点/本次成交/智能选股/候选观察池/市场环境信息层);
                          只画有持股的标的, 清仓标的略过
代码: quant/account.py(状态/流水) quant/engine.py(日频编排+日报)
    quant/dashboard.py(HTML看板)  quant/picker.py(智能选股)
    策略状态机在 core/adaptive.py(new_leg_state/decide/apply_fill),
    已全量replay验证: 个股收益/操作序列/权益曲线与 backtest/daily.py run_ps_stock
    完全一致(含v2.2冰点左侧/v2.4阶梯移动止盈/v2.5阶梯止损, 8/8标的零漂移)
注意: 新进持仓无回测覆盖的标的, 同样按v2.5全局规则执行
    (不逐股调参, 避免过拟合); 存量持仓切换v2.4时移动止盈hmax自切换日起算(保守)
⚠ 切勿重新 --init: 量化账户期初固定在 quant_state.json, 重建会串数据.
★手动接管存量持仓: 用户实盘已持有、需并入量化账户管理的标的, 直接在该
  状态文件新增子账户(cash=0/持股=实盘数/cost=实盘成本/hmax与entry_px=接管日收盘, 保守),
  并把等额现金从指定子账户划出(保持总资产不变)。
  新增前须先备份 quant_state.json(自动 .bak_时间戳), 接管当日 last_date 设为当日
  以免重复推进
⚠ 东财日K节点(push2his)会间歇拒连: 某只持仓取不到日K时不决策, 总资产/看板按该股
  最近一次收盘价估值, 并在日报与看板顶部标注"行情缺失N只"(不会报出虚假总资产).
  权益曲线自 2026-10-14 起逐日累计.
★收盘价失准修复(2026-10-22日报事件, 用户报障): 两个根因均已修复——
  ①kline180缓存锁定bug: 盘中(如干跑)写入的当日未完成bar, 收盘后被"锁定复用"条款
    直接当收盘价(持仓股曾以盘中价冒充真实收盘); 已引入 forming_last/
    eod_saved 双标志(common/quotes.py): 仅"15:00后写入的当日bar"可锁定至次日,
    盘中bar收盘后强制增量刷新; 收盘后刷新失败且末根为盘中bar时直接抛错降级,
    绝不以盘中价冒充收盘价
  ②15:01整取数过早: 收盘竞价成交未合并进当日bar(实测偏差+0.1~0.3%);
    quant_daily.py 自动跑现等待至15:03再取数(_wait_eod_settled)
  事件当日数据已回滚重放, 真实收盘价修正; 坏数据备份 data\quant\quant_state.bak_*.json
★日报去回测内容(2026-10-23 用户要求): 日报与看板移除"较基准"列及"回测固化"等
  回测口径文案, 策略描述统一为"v2.5双模策略"
★PushPlus正文上限2万字(code 999"发送内容过大"): common/notify.py 检测到超限时
  按小节标记自动分段推送(标题缀(1/2)(2/2)), 单节仍超限则按字数硬切, 发送失败
  会打印响应体便于排查
★9:26竞价自动更新t_plan(2026-10-22 上线): runner主循环在9:25~9:30窗口触发一次
  ttrade\auction.py(2026-11-05收拢进ttrade包), 读取昨收ATR+当日竞价open, 对zone做小幅
  动态调整写回t_plan.csv,
  runner下一轮run_cycle自然检测到mtime变化加载新计划; 调整规则(不做剧烈重算):
    高开≥+1.5%(open-昨收)/昨收: 低吸区上移ATR×0.3, 止损上移ATR×0.2
    低开≤-1.5%: 低吸区下移ATR×0.3, 止损受"昨收-ATR×0.5"地板保护(只上不下)
    跳空极宽≥ATR×2.0: 今日低吸区置空(避免追高/接飞刀), 高抛区保留
    平开(±1.5%内): 原计划不动
  竞价数据来源: fetch_quotes f2(9:25后即竞价开盘价), 失败/数据缺失则整票不调整;
  调整后t_plan自动追加变更说明列, 并推送一条微信摘要; 每日仅触发一次(last_auction_date)
  独立入口: python -m ttrade.auction [--test] 可单独干跑竞价更新
★量化双窗口择机执行(2026-10-26 用户需求, 量化操作时间改造): 收盘信号不再只按次日
  开盘价成交, 改为盘中两段操作窗口择机+兜底, 新增常驻进程 quant_runner.py
  (quant/runner.py, start.ps1 于9:25与T侧监控一同拉起, 15:00自行退出, stop.ps1
  同步收割; 日志 data\quant\runner_out.log / runner_err.log):
    9:25~9:26  竞价确认建当日计划: 遍历有pending信号的子账户, SELL算深跳空线
               (昨收×(1-3%)), BUY_SCALP竞价空间校验(开盘<need确认位即取消),
               ST股买入硬拦截; 推送"📋 9:26 今日操作计划"
    9:30~10:00 早盘窗口(每30秒盯价): SELL=反抽至分时VWAP卖/跌破深跳空线立即卖;
               BUY=回踩至分时VWAP且守住need确认位买入(无need仅看VWAP)
    14:30      纪律重估+推送尾盘验证: SELL现价收回信号level上方→取消;
               BUY跌破need确认位→取消; 超时信号(无level)不取消; 其余继续执行
    14:30~14:55 尾盘窗口: 同早盘规则继续择机
    14:55      兜底: SELL全部现价强制卖出(纪律优先); BUY需need为None或现价≥need,
               否则取消; 无行情保留信号交引擎开盘价兜底
  协调机制: state["day_plan"][date][code]为"当日已处理"唯一标记(filled/cancelled,
    _meta记planned/revalued/forced幂等标记), 引擎(engine._day_plan_pick)见filled/
    cancelled跳过开盘价补成交且fills展示盘中成交; runner未跑/崩溃(无记录或仍
    pending)时引擎按开盘价兜底(原行为, 降级安全); 崩溃重启从day_plan恢复续跑
  簿记归属: runner对盘中成交做全套簿记(apply_fill实际价+cost重算/清仓cost归零
    +last_sell+cum_fee+append_trade via="窗口择机/尾盘兜底"); 引擎对filled仅
    展示+兜底登记(幂等); 成交流水CSV仍是看板单一事实源
  回测口径不变: 回测仍按开盘价成交, 窗口择机价属执行层差异
  参数(config_quant.py): QUANT_RUNNER_EN/QUANT_TICK_SECONDS/QUANT_WIN1/QUANT_WIN2/
    QUANT_CONFIRM_TIME/QUANT_FORCE_TIME/QUANT_GAP_SELL/QUANT_VWAP_KLT
  手动演练: python quant_runner.py --dry --now "HH:MM" 指定时刻单轮干跑(不写盘不推送)

九、智能选股(量化账户"选股池", 2026-10-14新增)
----------------------------------------------------------------
标的"趋势走完即卖出"后, 其子账户现金不再闲置: 每个调仓日(默认5个交易日)归集进
"选股池", 由全池统一的选股规则补入新标的(收盘入选 -> 次日开盘买入), 周而复始.
规则(全局统一, 不逐股调参):
  1) 热门板块: 概念/行业各自取当日涨幅榜前12, 复筛板块指数20日动量, 各取前4个
  2) 候选池  : 热门板块成分股按成交额取前12只(去重)
  3) 硬过滤  : 剔除 ST / 日K不足60根(近似上市未满60交易日) / 现价<3元 / 20日均额<1亿
     ★死规则(2026-10-21 用户): 一切ST股(名称含ST, 含*ST/SST等)选股与买入均排除, 四层拦截:
     ①picker扫描跳过(本条) ②候选池每日自动清除 ③建仓目标(池选/手动观察池)过滤
     ④开盘买入成交前硬拦截(engine._advance, 任何来源信号的最终闸门, 触发即取消信号并记日志)
  4) 个股门槛: v2.1趋势入场(收盘>MA20 & MA5>MA10 & 收盘>MA60 & MA20上行 & 20日动量>3%)
  5) 排序    : 按20日动量降序取前2只(PICK_TOPK); 极端标的排序方向看连板行情(见下)
  6) 冷却    : 卖出后同一标的10个交易日内禁止再入选(用该标的自身日K定位, 与市场日历一致)
  7) 极端降权(2026-10-21 用户规则, PICK_EXTREME_*): 连板妖股打标(满足任一: 近10日
     涨停>=3 / 20日动量>=100% / 近5日累计>=30%; 主板10%、创业科创20%涨停自动区分),
     排序方向看连板行情: 连板行情=妖股优先, 非连板行情=沉底不优先; 趋势股规则不受影响
  8) 连板行情判定(用户规则③, PICK_ZTM_*): 近5个交易日东财涨停池, 日均涨停>=40家且
     日均连板(>=2板)>=8家 -> 连板行情; 接口失败/有效日<3 保守按非连板行情处理
  9) 池内择优建仓(用户规则②, PICK_POOL_REAL_EN=True): 非调仓日每日扫描累积候选池
     (保鲜5个交易日), 调仓日从池中按上述排序择优建仓, 替代旧的"当日扫描实选";
     背景: 曾出现连板妖股按排序被选中建仓, 被用户否决回滚(资金退回选股池,
     人工补录标的不在此列)
参数(config_quant.py, 全部 PICK_ 前缀):
    PICK_ENABLED 总开关; PICK_LIVE=False 时仅日报展示候选不出手(观察模式)
    PICK_REBAL 调仓周期; PICK_COOLDOWN 冷却; PICK_TOPK 每次最多新建仓数
    PICK_MIN_AMOUNT 流动性门槛; PICK_BOARD_SCAN/PICK_BOARD_M/PICK_BOARD_TOPN 控请求量
数据缓存: CACHE_DIR\kline_cache.json (板块/个股日K当日缓存, 删之则重拉)
日报"智能选股"节: 热门板块 / 候选与入选 / 剔除统计 / 选股池余额
每日候选观察池(2026-10-21升级为正式择优建仓, PICK_DAILY_EN/PICK_POOL_OBS扫描 +
  PICK_POOL_REAL_EN建仓): 每个交易日收盘都扫描一次候选(调仓日复用选股结果不重复
  请求), 过门槛标的累积入 state["cand_pool"](保鲜期 PICK_POOL_KEEP=5个交易日,
  超龄出池); 日报与看板每日展示候选池(含⚠连板极端标记)与"当前池内择优"Top,
  调仓日按池内择优实际建仓(排序口径=连板行情?妖股优先:极端沉底)
  板块标签: 风格/情绪分类(昨日涨停_含一字/昨日连板/ST股/次新股等)不是所属板块,
  此类来源候选自动补真实行业标签(quotes.fetch_stock_industry, 东财f100, 永久缓存),
  展示为"行业(风格来源)"如 小家电(昨日涨停_含一字)
看板归档(2026-10-21 用户规则④): 量化/日内T看板每日除固定文件外, 自动另存日期
  后缀副本到 report\quant\history\dashboard_quant_YYYY-MM-DD.html 与
  report\ttrade\history\dashboard_ttrade_YYYY-MM-DD.html(固定名为最新一日, 归档可回溯历史)
⚠ 局限(必读): 板块历史成分股名单不可得 -> "板块维度"无法做点内时间回测(存在幸存者偏差),
  只能靠实盘日报逐日积累验证; 个股层面的入选规则与v2.1回测口径一致(无未来函数).
  建议先看几日报纸面记录, 确认选股质量后再视情调整 PICK_* 参数.
