# -*- coding: utf-8 -*-
"""v5估算层冒烟测试(lab垂直隔离·不触生产): estim单元 + _pool_section渲染验证

运行: python lab/v5_smoke.py
验证点:
1) snapshot价位快照(压力/支撑/MA20/当日涨跌)无未来函数且量级合理
2) 位置档位(回踩确认/中位/近新高)与量能状态分档
3) entry_plan三档位+门控开关+极端沉底的时机文案
4) engine._pool_section v5新列渲染(当日情况/支撑压力/入池涨跌/时机节)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant import estim  # noqa: E402

# ---- 1) snapshot: 60根人造bars(缓涨), 末根贴近MA20 ----
bars = []
px = 10.0
for i in range(60):
    o = px
    c = px + (0.05 if i % 3 else -0.02)
    bars.append({"dt": "d%d" % i, "open": o, "close": c,
                 "high": c * 1.02, "low": o * 0.98, "vol": 1000 + i})
    px = c
snap = estim.snapshot(bars)
snap_r = {k: (round(v, 3) if isinstance(v, float) else v)
          for k, v in snap.items()}
print("1) snapshot:", snap_r)
assert snap["res1"] > snap["sup1"], "压力位应高于MA20支撑"
assert snap["sup2"] <= snap["sup1"], "强支撑(20日低)不应高于MA20"
assert snap["pct_chg"] is not None

# ---- 2) 位置档位/量能状态 ----
e = {"bias": 0.03, "vr": 1.25, "extreme": False, **snap}
assert estim.pos_state(e) == "回踩确认"
assert estim.vol_state(e) == "放量"
print("2) pos=回踩确认 | vol=放量 ✓")

# ---- 3) 入仓时机文案(三档位+门控+极端) ----
p_open = estim.entry_plan(e, gate_open=True)
p_closed = estim.entry_plan(e, gate_open=False)
assert "回踩确认" in p_open and "MA20" in p_open and "分批低吸" in p_open
assert p_closed.startswith("[大盘门控关闭")
e_mid = {"bias": 0.07, "vr": 0.80, "extreme": False, **snap}
p_mid = estim.entry_plan(e_mid, True)
assert p_mid.startswith("中位")
e_hi = {"bias": 0.12, "vr": 0.90, "extreme": False, **snap}
p_hi = estim.entry_plan(e_hi, True)
assert p_hi.startswith("近新高")
e_ext = {"bias": 0.03, "vr": 1.25, "extreme": True, **snap}
assert "极端沉底" in estim.entry_plan(e_ext, True)
print("3) 时机文案三档+门控+极端 ✓")
print("   回踩确认(开门):", p_open)
print("   中位:", p_mid)
print("   近新高:", p_hi)

# ---- 4) _pool_section v5渲染(假条目, 门控关闭场景) ----
from quant import engine  # noqa: E402

state = {"accounts": {}}
cal = ["2026-11-10", "2026-11-17", "2026-11-18", "2026-11-19"]
base = {"board_kind_cn": "行业", "board_name": "银行",
        "z_m120": 0.5, "z_mom20": -0.8, "z_vol20": -0.2, "z_bias": -0.4,
        "z_vr": 1.9, "m120": 0.12, "mom20": 0.05, "vol20": 0.02,
        "last_date": "2026-11-19", "hits": 2,
        "reason": "20日低波动", "extreme": False,
        "res1": 5.80, "res2": None, "sup1": 5.40, "sup2": 5.10,
        "pct_chg": 0.012}
ent1 = dict(base, code="601398", name="邮储银行", score=1.70, close=5.63,
            bias=0.04, vr=1.25, first_date="2026-11-18", first_close=5.50)
ent2 = dict(base, code="600036", name="招商银行", score=3.06, close=45.20,
            bias=0.12, vr=0.90, first_date="2026-11-10", first_close=42.00,
            res1=44.50, res2=46.00, sup1=41.00, sup2=40.00, pct_chg=-0.005)
obs = ([ent1, ent2], [ent1, ent2], False, {})
menv = {"idx": {"close": 3350.0, "ma20": 3360.0}}  # 模拟门控关闭
out = engine._pool_section(obs, state, cal, "2026-11-19", menv=menv)
assert "当日情况" in out and "支撑/压力" in out and "入池涨跌" in out
assert "入仓时机(v5·信息层)" in out
assert "[大盘门控关闭" in out          # 门控前缀
assert "邮储银行" in out and "招商银行" in out
assert "回踩确认" in out               # 当日情况档位
print("4) _pool_section v5渲染 ✓(当日情况/支撑压力/入池涨跌/时机节/门控前缀)")

# ---- 5) 回退开关验证: PICK_V5_EN=False 回纯v4展示 ----
import config_quant as config  # noqa: E402
config.PICK_V5_EN = False
out_v4 = engine._pool_section(obs, state, cal, "2026-11-19", menv=menv)
assert "当日情况" not in out_v4 and "入仓时机" not in out_v4
assert "[回踩确认]" in out_v4          # v4归档口径: 位置标签挂回名称
assert "入池" in out_v4
config.PICK_V5_EN = True
print("5) V5开关回退 ✓(False=纯v4展示, 位置标签回名称)")
print("\nALL_PASS")
