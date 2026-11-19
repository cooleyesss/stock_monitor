# -*- coding: utf-8 -*-
"""公共配置(公共系统): 路径/数据目录/行情源/推送/AI/费率/颜色 + 跨系统共用常量

本模块只 import os, 不依赖任何本地模块(无循环依赖)。
T 子系统配置 = config_ttrade.py (from config_common import * + T 专属常量)
量化子系统配置 = config_quant.py (from config_common import * + 量化专属常量)
调用侧统一写 `import config_common as config` / `import config_ttrade as config` /
`import config_quant as config`, 引用写法(config.XXX)与拆分前完全一致。
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- 文件路径 ----------------
# 根目录只放公共文件(代码/启停脚本/监控日志/真实持仓快照); 各系统的数据各自入库
DATA_DIR = os.path.join(BASE_DIR, "data")
TT_DATA_DIR = os.path.join(DATA_DIR, "ttrade")   # 每日做T模拟盘数据目录
QT_DATA_DIR = os.path.join(DATA_DIR, "quant")    # 量化独立账户数据目录
REPORT_DIR = os.path.join(BASE_DIR, "report")    # 看板HTML统一目录(dashboard_*.html)
DASH_SINCE = "2026-10-08"   # 看板默认展示窗口起点(用户约定: 不做特殊说明就从10.8起, 不追溯太久)
for _d in (TT_DATA_DIR, QT_DATA_DIR, REPORT_DIR):
    os.makedirs(_d, exist_ok=True)

CSV_PATH = os.path.join(BASE_DIR, "mystock_test.csv")   # 唯一母表(用户按截图维护, 系统只读)
TT_HOLDINGS_CSV = os.path.join(TT_DATA_DIR, "holdings.csv")   # T 子系统持仓(由母表一键分发)
QT_HOLDINGS_CSV = os.path.join(QT_DATA_DIR, "holdings.csv")   # 量化子系统持仓(由母表一键分发)
T_PLAN_CSV = os.path.join(TT_DATA_DIR, "t_plan.csv")   # 做T计划(每轮重读, 9:25 tplan_gen自动生成, 手工修改当日即时生效)
T_PLAN_LEGACY_CSV = None    # 旧外部计划文件路径(仅首次种子导入票池用); None=无种子,
                            # 个性化路径在 config_local.py 配置
STATE_PATH = os.path.join(TT_DATA_DIR, "sim_state.json")   # 做T模拟盘状态
TRADES_CSV = os.path.join(TT_DATA_DIR, "sim_trades.csv")   # 做T成交流水(CSV表格)
TRADES_LOG = os.path.join(TT_DATA_DIR, "trades.log")       # 做T成交日志(含每日收盘T盈亏记录)
LOG_OUT = os.path.join(BASE_DIR, "monitor.log")            # 监控进程日志(公共)
LOG_ERR = os.path.join(BASE_DIR, "monitor_err.log")
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache", "bt_tmp")
# 运行期缓存(板块指数日K/1分钟K等), 可随时删除重建; 个性化路径在 config_local.py 配置

# ---------------- 跨系统共用常量 ----------------
LOT_MIN = 100               # A股最小交易单位
ETF_CODE_MAP = {"ZZ1000": "159633"}   # 持仓文件里代码为"--"的品种对应的真实ETF代码
                                      # (ZZ1000 = 易方达中证1000ETF)
REGIME_INDEX_ALIAS = "999999" # 项目内上证指数别名(裸000001撞平安银行, 绝不能进个股集合)

# ---------------- 个性化配置(持仓/账户相关的中性默认, 用户在 config_local.py 覆盖) ----------------
# 仓库不带任何个人持仓/资金/凭证; 参照 config_local_template.py 建 config_local.py
T_EXCLUDE_CODES = set()     # T模拟盘剔除名单(不入持仓/不入估值/不做T)
QT_EXCLUDE_CODES = set()    # 量化账本剔除名单(不入持仓/不入估值/不参与信号)
BT_CAPITAL = 1000000.0      # 回测期初资金锚点(跨日对比需稳定基准, 按个人账户口径配置)
BT_EXCLUDE_CODES = set()    # 量化池回测的抛弃标的名单
LAB_BASE_CASH = 0.0         # lab 盘前实验基准现金
LAB_BASE_POS = {}           # lab 盘前实验基准持股 {code: 股数}
LAB_BASE_COST = {}          # lab 盘前实验基准成本 {code: 成本价}

# ---------------- 交易费用 ----------------
FEE_COMMISSION = 0.00025      # 佣金 万2.5
FEE_COMMISSION_MIN = 5.0      # 佣金最低5元
FEE_TRANSFER = 0.00001        # 过户费 十万分之一(双向, 仅股票)
FEE_STAMP_SELL = 0.0005       # 印花税 万5(仅卖出, 仅股票; ETF全免只收佣金)

# ---------------- 微信推送(PushPlus) ----------------
PUSHPLUS_TOKEN = ""         # PushPlus 推送令牌(个性化, 在 config_local.py 配置; 留空=不推送)

# ---------------- DeepSeek AI守门 ----------------
DEEPSEEK_ENABLED = True
DEEPSEEK_GATEKEEP = True     # True=AI建议观望则不成交 / False=AI意见仅附在推送里
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_API_KEY = ""        # DeepSeek API Key(个性化, 在 config_local.py 配置; 留空=自动禁用AI)
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_TIMEOUT = 30        # 超时/失败按原策略执行(不因AI故障漏单); 止损不走AI(纪律优先)

# ---------------- 技术指标(15分钟K线, 做T参考) ----------------
KLINE_KLT = 15               # K线周期(分钟)
KLINE_LMT = 120              # 拉取K线根数(15分钟线约7.5交易日, 保证MACD/RSI稳定)
IND_CACHE_SEC = 300          # 指标缓存秒数
RSI_PERIOD = 14              # RSI标准周期
RSI_FAST = 6                 # RSI短线周期(做T更敏感)
RSI_OVERBOUGHT = 70          # RSI超买阈值(高抛参考)
RSI_OVERSOLD = 30            # RSI超卖阈值(低吸参考)

# ---------------- 1分钟K线数据层 ----------------
K1_KLT = 1                    # 1分钟K线(决策数据源: VWAP/布林/RSI6/动量)
K1_LMT = 1200                 # 拉取根数(约5交易日)
K1_CACHE_SEC = 120            # 缓存秒(5分钟对动量票延迟过大: 冲高回落3-5分钟内完成)

# ---------------- K线磁盘缓存(公共行情层) ----------------
PICK_CACHE_SEC = 21600      # 共享日K磁盘缓存有效期(秒, 6小时)
# 日K长期缓存池(2026-10-21): 个股/指数/板块日K统一落盘, 二次调用只拉缺失区间(增量)
# 每次请求都先查缓存, 命中直接返回; 未命中/过期才请求东财(失败回退腾讯), 显著降低接口调用量
KLINE_CACHE_EN = True       # 总开关(仅作用于日K klt=101; 分钟K仍走实时拉取, 避免做T信号延迟)
KLINE_CACHE_DAYS = 280      # 保留天数(自然日, 约190个交易日: 覆盖MA60+40日动量+预热
                            # +v3多因子m120跳段动量需131根日K, 2026-11-04由180天扩容)
KLINE_CACHE_BARS = 200      # 空缓存首次拉取根数(足够填满280天窗口)
KLINE_CACHE_TTL = 1800      # 盘中新鲜期(秒): 期内复用不请求; 收盘后拿到当日完整K线即锁定至次日
KLINE_CACHE_DIR = os.path.join(BASE_DIR, "data", "cache", "kline180")
# 缓存池目录(每标的独立文件, 便于增量读写)
# 2026-11-05修复: 原先挂在 CACHE_DIR(_bt_tmp 临时目录)下, 临时目录清理后缓存丢失,
# 导致每次 EOD 全量重拉日K(腾讯源限速下单次日跑可达30分钟+); 现落到 data\cache 持久化

# ---------------- 颜色(A股惯例: 红涨绿跌) ----------------
C_RED = "#e60000"
C_GREEN = "#00a800"
C_ORANGE = "#ff8c00"
C_GRAY = "#cccccc"   # 浅灰: 平盘/备注(微信黑底下可读, 深灰看不清)

# ---------------- 东财行情多节点 ----------------
QUOTE_HOSTS = [
    "https://push2.eastmoney.com",
    "https://33.push2.eastmoney.com",
    "http://push2.eastmoney.com",
]
KLINE_HOSTS = [
    "https://push2his.eastmoney.com",
    "https://33.push2his.eastmoney.com",
    "http://push2his.eastmoney.com",
]
KLINE_MIN_INTERVAL = 0.2    # 相邻K线请求最小间隔(秒): 防被东财限流(实测无间隔连发会拒连)
KLINE_RETRY = 4             # 同一节点重试次数(实测push2his节点会在"拒连/放行"间翻转, 单次必失败)
KLINE_RETRY_SLEEP = 1.5     # 重试间隔(秒)
# 腾讯K线回退源(东财push2his间歇拒连时启用; 实测稳定可用, 日K为前复权qfq与东财fqt=1口径一致)
# 注: web.ifzq.gtimg.cn 已被腾讯WAF拦截(501), 改用同接口的 ifzq.gtimg.cn 主机
TX_KLINE_HOST = "https://ifzq.gtimg.cn"   # 日K: /appstock/app/fqkline/get
TX_KLINE_HOSTS = [                        # 日K多节点轮询(2026-11-04: 主站被WAF 501时
    TX_KLINE_HOST,                        # 依次降级; 两个备用节点实测与主站同构)
    "https://proxy.finance.qq.com/ifzqgtimg",
    "https://web.ifzq.gtimg.cn",
]
TX_MKLINE_HOST = "https://ifzq.gtimg.cn"      # 分钟K: /appstock/app/kline/mkline
# 新浪财经第三级兜底(2026-11-19: 东财+腾讯同时故障时启用)
# K线: json_v2端点(日K不复权/量为股÷100转手, 分钟K自带额); 实时: hq.sinajs.cn批量(必须带Referer头)
SINA_KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
SINA_HQ_HOST = "https://hq.sinajs.cn"         # 实时: /list=sh600036,sz000001(GBK编码)
SINA_HQ_BATCH = 60                            # 实时批量每请求标的数上限(防URL过长)
EM_FAIL_BREAK = 3           # 东财K线连续失败N次 -> 本进程内熔断, 直接走腾讯(避免每次白等6秒)
EM_DOWN_SEC = 900           # 熔断静默秒数(到期后再探一次东财; 探测只给1轮, 失败立即续熔断)
# 板块/全市场列表(clist)节点: 实测仅 http 协议可用(https 被拒 RemoteDisconnected)
CLIST_HOSTS = [
    "http://push2.eastmoney.com",
    "http://33.push2.eastmoney.com",
]

# ---------------- 个性化配置加载(不入库, 用户自行建 config_local.py) ----------------
# 覆盖任意上方配置: token/key/机器路径/剔除名单/回测资金/lab基准等, 参照 config_local_template.py
# 注意: config_local.py 内不要再 from config_common import *, 只写要覆盖的赋值
try:
    from config_local import *          # noqa: F401,F403
    _HAS_LOCAL = True
except ImportError:
    _HAS_LOCAL = False
if not DEEPSEEK_API_KEY:
    DEEPSEEK_ENABLED = False    # 无Key自动禁用AI守门(调用侧按开关跳过, 空Key请求只会失败)
