# -*- coding: utf-8 -*-
"""个性化配置模板: 复制为 config_local.py 后按需填写(该文件不入库, 已在 .gitignore)
config_common.py 末尾会自动加载 config_local.py 覆盖默认值;
所有键均有中性默认(见 config_common.py "个性化配置"节), 缺失不影响程序运行:
  - PUSHPLUS_TOKEN 空 -> 微信推送静默失败打印
  - DEEPSEEK_API_KEY 空 -> 自动禁用 AI 守门(信号照常, 不调AI)
  - 其余按需覆盖
注意: 本文件内不要再 from config_common import *, 只写要覆盖的赋值"""

# ---- 微信推送 / AI守门凭证(必配才有推送与AI) ----
PUSHPLUS_TOKEN = ""             # PushPlus 官网获取的推送令牌
DEEPSEEK_API_KEY = ""           # DeepSeek 平台 API Key

# ---- 本机路径(按需覆盖) ----
# CACHE_DIR = r"D:\你的缓存目录"                    # 运行期缓存, 默认项目内 data/cache/bt_tmp
# T_PLAN_LEGACY_CSV = r"D:\t_plan.csv"             # 旧外部t_plan种子文件, 默认None(无种子)

# ---- 剔除名单(按你实际持仓品种配置) ----
# T_EXCLUDE_CODES = set()        # T模拟盘剔除: 不入持仓/不入估值/不做T
# QT_EXCLUDE_CODES = set()       # 量化账本剔除: 不入持仓/不入估值/不参与信号

# ---- 回测资金锚点(按你的账户口径配置) ----
# BT_CAPITAL = 1000000.0         # 回测期初资金
# BT_EXCLUDE_CODES = set()       # 量化池回测的抛弃标的

# ---- lab 盘前实验基准(仅 lab/tools/intraday_gap_test.py 使用) ----
# LAB_BASE_CASH = 0.0
# LAB_BASE_POS = {}              # {code: 股数}
# LAB_BASE_COST = {}             # {code: 成本价}
