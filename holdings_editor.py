# -*- coding: utf-8 -*-
"""持仓/观察池本地编辑器: 浏览器维护母表 mystock_test.csv 与 量化观察池 watchlist.csv
启动: python holdings_editor.py [--port 8766]  ->  http://127.0.0.1:8766
两页签:
  ①持仓编辑: mystock_test.csv(唯一母表) 全行增删改, 保存自动备份.bak 并重算市值/盈亏(有现价成本时);
             上传券商App持仓截图 -> RapidOCR识别(代码/名称/成本/现价/持股/市值/盈亏+可用资金),
             识别行高亮追加进表格人工修正后保存;
             【同步】按钮可选 T模拟盘 / 量化账本 / 两者, 把母表原格式分发到
             data/ttrade/holdings.csv 与 data/quant/holdings.csv
             (勾选'保存后自动分发'则每次保存后分发到两者)。两子系统初始化只读各自的分发文件, 不读母表
  ②观察池: 量化账户手动观察池(代码/名称/备注), 【当前未启用, 见页内提示】原设计见 HINT_W
OCR: RapidOCR(rapidocr-onnxruntime, pip即用, 首次运行自动下载模型); 缺包时上传区提示降级为手动录入
注意: 模板只用 .replace() 占位符, 禁止 %-format (CSS里100%会触发ValueError)"""
import base64
import csv
import datetime
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import cv2            # opencv-python: 截图字节流解码为ndarray供OCR
import numpy as np

import config_common as config
from quant import account

PAGE = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<title>持仓编辑器</title>
<style>
body{font:14px/1.5 -apple-system,"Microsoft YaHei",sans-serif;background:#1b1f24;color:#dde;margin:0;padding:16px}
h1{font-size:18px;margin:0 0 12px}
.tabs a{display:inline-block;padding:6px 14px;margin-right:6px;border-radius:6px;text-decoration:none;color:#dde;background:#2a3138}
.tabs a.on{background:#3f4a5a;font-weight:700}
table{border-collapse:collapse;width:100%;margin-top:12px}
th{background:#3f4a5a;color:#fff;font-weight:700;padding:6px 8px;text-align:left;white-space:nowrap}
td{padding:2px 4px;border-bottom:1px solid #333}
td input{width:100%;box-sizing:border-box;background:#22282e;color:#dde;border:1px solid #3a434d;border-radius:4px;padding:4px 6px}
td input.ro{background:transparent;border-color:transparent;color:#9aa}
tr.hl td input{background:#3a3320;border-color:#6b5b2a;color:#ffe9a8}
button{cursor:pointer;background:#3f4a5a;color:#fff;border:0;border-radius:6px;padding:7px 16px;font-size:14px;margin:10px 6px 0 0}
button.warn{background:#7a3a3a}
button.blue{background:#2f5a8a}
.msg{color:#7fd07f;margin-left:8px}
.hint{color:#9aa;font-size:12px;margin-top:10px;white-space:pre-line}
.ocrbox{background:#22282e;border:1px solid #3a434d;border-radius:8px;padding:10px 12px;margin-top:12px}
.ocrbox input[type=file]{color:#dde}
.ocrbox input[type=text]{background:#22282e;color:#dde;border:1px solid #3a434d;border-radius:4px;padding:4px 6px;width:90px}
#ocrmsg{color:#ffd27f;margin-left:8px}
</style></head><body>
<h1>__TITLE__</h1>
<div class=tabs>
<a href="/" class="__ON_H__">持仓编辑</a>
<a href="/watch" class="__ON_W__">量化观察池</a>
</div>
__OCRBOX__
<div id=app>__TABLE__</div>
<button onclick="addRow()">+ 增行</button>
<button onclick="save()">💾 保存(自动备份.bak)</button>
__DIST__
<span class=msg id=msg></span>
<div class=hint>__HINT__</div>
<script>
var HEAD = __HEAD__;
function cell(v){return '<td><input value="'+(v==null?'':String(v).replace(/"/g,'&quot;'))+'"></td>'}
function buildRow(vals,hl){
  var tr=document.createElement('tr');
  if(hl)tr.className='hl';
  tr.innerHTML=HEAD.map(function(h,i){return cell(vals&&vals[i]!=null?vals[i]:'')}).join('')
    +'<td><button class=warn onclick="this.closest(\'tr\').remove()">删</button></td>';
  return tr;
}
function addRow(){document.querySelector('#grid tbody').appendChild(buildRow(null,false))}
function ocr(){
  var f=document.getElementById('shot').files[0];
  if(!f){document.getElementById('ocrmsg').textContent='请先选择截图';return}
  document.getElementById('ocrmsg').textContent='识别中(首次需加载OCR模型, 约5~20秒)…';
  var rd=new FileReader();
  rd.onload=function(){
    fetch('/ocr',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({img:rd.result.split(',')[1],broker:(document.getElementById('broker')||{}).value||''})})
    .then(function(r){return r.json()})
    .then(function(j){
      document.getElementById('ocrmsg').textContent=j.msg||'';
      document.getElementById('ocrhint2').textContent=((j.cash!=null)?('识别到可用资金 '+j.cash+' 元, 请手动更新对应"可用"行; '):'')+(j.warn||'');
      (j.rows||[]).forEach(function(r){document.querySelector('#grid tbody').appendChild(buildRow(r,true))});
    })
    .catch(function(e){document.getElementById('ocrmsg').textContent='识别失败: '+e});
  };
  rd.readAsDataURL(f);
}
function save(){
  var rows=[];
  document.querySelectorAll('#grid tbody tr').forEach(function(tr){
    var r=[];tr.querySelectorAll('input').forEach(function(i){r.push(i.value)});
    if(r.some(function(x){return x.trim()!==''}))rows.push(r);
  });
  var ad=document.getElementById('autoDist');
  fetch(location.pathname==='/watch'?'/watch/save':'/save',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({rows:rows,auto:ad?ad.checked:false})})
  .then(function(r){return r.json()})
  .then(function(j){document.getElementById('msg').textContent=j.msg;
    if(j.table!==undefined)document.getElementById('app').innerHTML=j.table;});
}
function dist(tt,qt){
  document.getElementById('msg').textContent='分发中…';
  fetch('/distribute',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({tt:!!tt,qt:!!qt})})
  .then(function(r){return r.json()})
  .then(function(j){document.getElementById('msg').textContent=j.msg;});
}
</script>
</body></html>"""

OCRBOX = """<div class=ocrbox>
<b>📷 券商截图识别</b>
<input type=file id=shot accept="image/*">
<input type=text id=broker placeholder="券商名">
<button class=blue onclick="ocr()">🔍 识别并追加(识别行高亮, 请人工修正)</button>
<span id=ocrmsg></span>
<div class=hint id=ocrhint2></div>
</div>"""

HINT_H = ("持仓文件 mystock_test.csv = 唯一母表(真实持仓快照)。保存时对'持股>0且成本/现价为数字'的行"
          "自动重算 市值=现价×持股 / 盈亏=(现价-成本)×持股 / 盈亏比。"
          "系统在初始化时只读各自分发文件: T 子系统读 data/ttrade/holdings.csv, "
          "量化子系统读 data/quant/holdings.csv —— 改完母表后请点【同步】按钮分发到所需子系统"
          "(或勾选'保存后自动分发到两者')。\n"
          "截图识别: 上传券商App持仓页截图, 识别 代码/名称/成本/现价/持股/市值/盈亏 与 可用资金,"
          "识别行以黄色高亮追加, 请核对券商/日期/备注后再保存; 识别不准的字段直接改单元格即可。")
HINT_W = ("量化账户手动观察池(watchlist.csv): 【当前未启用(2026-10-20 撤回共振升级), "
          "填了也不会参与分配/建仓, 数据保留】原设计: 每个调仓日与智能选股合并分配资金(观察池优先); "
          "入池后等v2.2信号(趋势站上/超跌/冰点左侧)择机建仓, 退出=趋势腿破MA10; "
          "已在管理中的标的不会重复分配。代码填6位数字(如 600519)。")

SYNC_BTNS = ("<button onclick=\"dist(1,0)\">📤 同步到T模拟盘</button>"
             "<button onclick=\"dist(0,1)\">📤 同步到量化账本</button>"
             "<button onclick=\"dist(1,1)\">📤 同步两者</button>"
             "<label style=\"color:#9aa;font-size:12px;margin-left:6px\">"
             "<input type=checkbox id=autoDist> 保存后自动分发到两者"
             "(data/ttrade/holdings.csv + data/quant/holdings.csv)</label>")


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _grid(headers, rows, ro_cols=()):
    """生成可编辑表格HTML; ro_cols=只读列下标(重算结果列)"""
    h = "".join(f"<th>{_esc(x)}</th>" for x in headers) + "<th></th>"
    body = []
    for r in rows:
        tds = []
        for i, v in enumerate(r):
            ro = ' ro readonly' if i in ro_cols else ''
            tds.append(f'<td><input class="{ro.strip()}" '
                       f'{"readonly" if i in ro_cols else ""} value="{_esc(str(v))}"></td>')
        tds.append('<td><button class="warn" '
                   'onclick="this.closest(\'tr\').remove()">删</button></td>')
        body.append("<tr>" + "".join(tds) + "</tr>")
    return (f'<table id=grid><thead><tr>{h}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table>')


def _read_csv(path):
    if not os.path.exists(path):
        return [], []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    return (rows[0], rows[1:]) if rows else ([], [])


def _num(s):
    try:
        return float(str(s).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _recalc_holdings(headers, rows):
    """持仓行重算: 市值=现价*持股, 盈亏=(现价-成本)*持股, 盈亏比(成本>0时); 非数字不动"""
    idx = {h: i for i, h in enumerate(headers)}
    need = ("市值", "盈亏", "盈亏比", "成本", "现价", "持股")
    if not all(k in idx for k in need):
        return rows
    out = []
    for r in rows:
        r = list(r) + [""] * (len(headers) - len(r))
        sh, cost, px = (_num(r[idx[k]]) for k in ("持股", "成本", "现价"))
        if sh and sh > 0 and cost and px:
            mv = px * sh
            pnl = (px - cost) * sh
            r[idx["市值"]] = f"{mv:.2f}"
            r[idx["盈亏"]] = f"{pnl:.2f}"
            r[idx["盈亏比"]] = f"{(px / cost - 1) * 100:.2f}%"
        out.append(r)
    return out


def _save_csv(path, headers, rows):
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            bak = f.read()
        with open(path + ".bak", "w", encoding="utf-8") as f:
            f.write(bak)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


def _distribute(tt=True, qt=True):
    """把母表 mystock_test.csv 按原格式分发到勾选的子系统持仓文件(各自先备份.bak)
    T模拟盘 -> data/ttrade/holdings.csv ; 量化账本 -> data/quant/holdings.csv"""
    head, rows = _read_csv(config.CSV_PATH)
    if not head:
        return "母表 mystock_test.csv 为空, 未分发"
    tgt = []
    if tt:
        tgt.append(("T模拟盘", config.TT_HOLDINGS_CSV))
    if qt:
        tgt.append(("量化账本", config.QT_HOLDINGS_CSV))
    if not tgt:
        return "未选择同步目标"
    for _, p in tgt:
        _save_csv(p, head, rows)
    return ("已分发{}行 -> {}(均已备份.bak)".format(
        len(rows), " 与 ".join(f"{n}({p})" for n, p in tgt)))


# ============================== 截图OCR识别 ==============================
_OCR = None


def _get_ocr():
    """懒加载RapidOCR(模型首次加载慢); 缺包抛 RuntimeError 由调用方提示"""
    global _OCR
    if _OCR is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise RuntimeError(f"未安装 rapidocr-onnxruntime({e}); 请执行 "
                               f"pip install rapidocr-onnxruntime 后重试") from e
        _OCR = RapidOCR()
    return _OCR


# 列头/汇总关键词(命中则不算股票名称)
_CN_KW = ("合计", "可用", "总资产", "资金", "余额", "市值合计", "成本", "现价",
          "最新", "市值", "盈亏", "数量", "持仓", "证券", "参考", "当日", "比例",
          "买入", "卖出", "摊薄", "名称", "代码", "股票", "更新", "对比", "成本价",
          "市价", "浮动", "盈亏比")
_COL_FIELDS = (("摊薄", "cost"), ("成本价", "cost"), ("成本", "cost"),
               ("最新价", "px"), ("现价", "px"), ("最新", "px"), ("市价", "px"),
               ("持仓数量", "qty"), ("证券数量", "qty"), ("可用数量", "qty"),
               ("数量", "qty"), ("持仓", "qty"),
               ("参考市值", "mv"), ("市值", "mv"),
               ("参考盈亏", "pnl"), ("当日盈亏", "pnl"), ("盈亏", "pnl"),
               ("盈亏比例", "pctr"), ("比例", "pctr"))


def _numf(s):
    try:
        return float(str(s).replace(",", "").replace("%", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def _code6(s):
    """从token提取6位证券代码(容忍OCR把代码夹在文字中或数字间加空格)"""
    s = re.sub(r"[OoIl|,]", "0", str(s))
    m = re.search(r"(?<![\d.])([4-8]\d{5}|[03]\d{5})(?![\d.])", s)
    return m.group(1) if m else None


def _has_code(tokens):
    return any(_code6(t["t"]) for t in tokens)


def _tok_xspan(tok, m):
    """token内子串的中心x坐标(按字符位置比例)"""
    w = tok.get("w") or 1.0
    frac = (m.start() + m.end()) / 2.0 / max(len(tok["t"]), 1)
    return tok["x0"] + w * frac


def _classify_row(tokens, colmap, colw):
    """把一行token解析成持仓记录: 列头x对齐优先, 启发式兜底"""
    code = None
    for t in tokens:
        c = _code6(t["t"])
        if c:
            code = c
            break
    if not code:
        return None
    name = ""
    for t in tokens:
        cns = [c for c in re.findall(r"[\u4e00-\u9fa5A-Za-z]{2,}", t["t"])
               if not any(k in c for k in _CN_KW)]
        if cns:
            name = "".join(cns)
            break
    nums = []   # {v, x, tag}
    for t in tokens:
        for m in re.finditer(r"[-+]?[\d,]+\.?\d*%?", t["t"].replace(" ", "")):
            s = m.group(0)
            v = _numf(s)
            if v is None:
                continue
            # 代码本身别当数字
            if _code6(s) and re.fullmatch(r"\d{6}", s.replace(",", "")):
                continue
            tag = ("pctr" if "%" in s else
                   "dec" if "." in s else "int")
            nums.append({"v": v, "x": _tok_xspan(t, m), "tag": tag,
                         "neg": s.startswith("-")})
    rec = {"code": code, "name": name, "cost": None, "px": None,
           "qty": None, "mv": None, "pnl": None, "pctr": None}
    if colmap:
        for n in nums:
            best, bd = None, 1e18
            for f, x in colmap.items():
                d = abs(n["x"] - x)
                if d < bd:
                    best, bd = f, d
            if best and bd <= max(colw * 0.75, 60) and rec.get(best) is None:
                rec[best] = n["v"]
        return rec
    # ── 无列头启发式 ──
    decs = [n for n in nums if n["tag"] == "dec"]
    ints = [n for n in nums if n["tag"] == "int"]
    pctr = [n for n in nums if n["tag"] == "pctr"]
    if pctr:
        rec["pctr"] = pctr[0]["v"]
    # 小数 token: 值大(>=1000)当市值; 其余按 x 顺序前=成本 后=现价
    big = [n for n in decs if abs(n["v"]) >= 1000]
    small = sorted((n for n in decs if abs(n["v"]) < 1000), key=lambda n: n["x"])
    if big and rec["mv"] is None:
        rec["mv"] = big[0]["v"]
    if len(small) >= 2:
        rec["cost"], rec["px"] = small[0]["v"], small[1]["v"]
    elif len(small) == 1:
        rec["px"] = small[0]["v"]
    # 整数 token: 值在合理股数区间(>=100)第一个当持股; 更大的当市值
    for n in ints:
        if rec["qty"] is None and 100 <= abs(n["v"]) <= 2000000:
            rec["qty"] = n["v"]
        elif rec["mv"] is None and abs(n["v"]) > 2000000:
            rec["mv"] = n["v"]
    return rec


def _parse_shot(items, broker=""):
    """RapidOCR结果 -> (rows[母表列], cash, warn)
    items: RapidOCR返回 [[box, text, score], ...]"""
    toks = []
    for it in items:
        try:
            box, text = it[0], it[1]
        except (IndexError, TypeError):
            continue
        text = (text or "").strip()
        if not text:
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        toks.append({"x0": min(xs), "x": (min(xs) + max(xs)) / 2,
                     "y": (min(ys) + max(ys)) / 2, "h": max(ys) - min(ys),
                     "w": max(xs) - min(xs), "t": text})
    if not toks:
        return [], None, "截图未识别到文字"
    toks.sort(key=lambda t: (t["y"], t["x"]))
    med_h = sorted(t["h"] for t in toks)[len(toks) // 2] or 20.0
    lines = []
    for t in toks:
        if lines and abs(t["y"] - lines[-1][-1]["y"]) < med_h * 0.6:
            lines[-1].append(t)
        else:
            lines.append([t])
    for ln in lines:
        ln.sort(key=lambda t: t["x"])
    # 列头锚: 找同时含 成本/现价/数量 类关键词且无6位代码的行
    colmap, colw = {}, 100.0
    for ln in lines:
        all_t = "".join(t["t"] for t in ln)
        if not any(k in all_t for k in ("成本", "现价", "最新", "数量", "持仓",
                                        "市值", "盈亏")):
            continue
        if _has_code(ln):
            continue
        for t in ln:
            for k, f in _COL_FIELDS:
                if k in t["t"] and f not in colmap:
                    colmap[f] = t["x"]
        if len(colmap) >= 3:
            xs = sorted(colmap.values())
            gaps = [b - a for a, b in zip(xs, xs[1:]) if b > a]
            if gaps:
                colw = sum(gaps) / len(gaps)
            break
    rows, cash, warn = [], None, []
    today = datetime.date.today().isoformat()
    for ln in lines:
        all_t = "".join(t["t"] for t in ln)
        code = next((c for c in (_code6(t["t"]) for t in ln) if c), None)
        if not code:
            if re.search(r"可用|资金|余额", all_t):
                # OCR常把千分位数字拆成多token(如"104, 483.62"), 拼接去分隔符后取数
                joined = re.sub(r"[\s,，]", "", all_t)
                cand = [v for v in (_numf(m.group(0)) for m in
                        re.finditer(r"[-+]?\d+(?:\.\d+)?", joined))
                        if v is not None]
                if cand:
                    cash = max(cand)
            continue
        if re.search(r"合计|总资产", all_t):
            continue
        rec = _classify_row(ln, colmap, colw)
        if not rec:
            continue
        missing = [k for k in ("cost", "px", "qty") if rec[k] is None]
        if missing:
            warn.append(f"{rec['code']}缺{','.join(missing)}")
        rows.append([broker, rec["name"], rec["code"],
                     "" if rec["mv"] is None else f'{rec["mv"]:g}',
                     "" if rec["pnl"] is None else f'{rec["pnl"]:g}',
                     "" if rec["pctr"] is None else f'{rec["pctr"]:g}%',
                     "" if rec["cost"] is None else f'{rec["cost"]:g}',
                     "" if rec["px"] is None else f'{rec["px"]:g}',
                     "" if rec["qty"] is None else f'{rec["qty"]:g}',
                     today, "截图识别(请人工核对)"])
    if not rows:
        return [], cash, "未识别出含6位代码的持仓行; 若截图为深色主题/长图请裁剪后重试"
    return rows, cash, ("; ".join(warn[:5]) if warn else "")


class Handler(BaseHTTPRequestHandler):
    def _html(self, title, table, head, on_h, hint):
        return (PAGE.replace("__TITLE__", title)
                    .replace("__TABLE__", table)
                    .replace("__HEAD__", json.dumps(head, ensure_ascii=False))
                    .replace("__ON_H__", "on" if on_h else "")
                    .replace("__ON_W__", "" if on_h else "on")
                    .replace("__OCRBOX__", OCRBOX if on_h else "")
                    .replace("__DIST__", SYNC_BTNS if on_h else "")
                    .replace("__HINT__", hint))

    def _send(self, body, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _json(self, obj):
        self._send(json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/watch":
            rows = account.load_watchlist()
            grid = _grid(account.WATCHLIST_HEADERS,
                         [[w["code"], w["name"], w["note"]] for w in rows])
            self._send(self._html("量化观察池(手动添加, 策略择机操作)", grid,
                                  account.WATCHLIST_HEADERS, False, HINT_W))
        else:
            head, rows = _read_csv(config.CSV_PATH)
            self._send(self._html("真实持仓编辑(mystock_test.csv)",
                                  _grid(head, rows), head, True, HINT_H))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            return self._json({"msg": f"解析失败: {e}"})
        rows = [[str(c) for c in r] for r in payload.get("rows", [])]
        path = urlparse(self.path).path
        if path == "/ocr":
            return self._do_ocr(payload)
        if path == "/distribute":
            try:
                return self._json({"msg": _distribute(tt=bool(payload.get("tt")),
                                                      qt=bool(payload.get("qt")))})
            except OSError as e:
                return self._json({"msg": f"分发失败: {e}"})
        if path == "/watch/save":
            out = []
            for r in rows:
                r = list(r) + [""] * (3 - len(r))
                out.append({"code": r[0].strip(), "name": r[1].strip(),
                            "note": r[2].strip()})
            try:
                account.save_watchlist(out)
            except OSError as e:
                return self._json({"msg": f"保存失败: {e}"})
            grid = _grid(account.WATCHLIST_HEADERS,
                         [[w["code"], w["name"], w["note"]]
                          for w in account.load_watchlist()])
            return self._json({"msg": f"已保存{len(out)}行(备份watchlist.csv.bak)",
                               "table": grid})
        head, _ = _read_csv(config.CSV_PATH)
        rows = [r[:len(head)] + [""] * (len(head) - len(r)) for r in rows]
        rows = _recalc_holdings(head, rows)
        try:
            _save_csv(config.CSV_PATH, head, rows)
        except OSError as e:
            return self._json({"msg": f"保存失败: {e}"})
        msg = f"已保存{len(rows)}行(备份mystock_test.csv.bak, 市值/盈亏已重算)"
        if payload.get("auto"):
            try:
                msg += "；" + _distribute()
            except OSError as e:
                msg += f"；分发失败: {e}"
        return self._json({"msg": msg, "table": _grid(head, rows)})

    def _do_ocr(self, payload):
        img_b64 = payload.get("img") or ""
        broker = str(payload.get("broker") or "").strip()
        try:
            import numpy as np
            data = base64.b64decode(img_b64)
            arr = np.frombuffer(data, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return self._json({"msg": "图片解码失败, 请确认上传的是截图文件"})
            ocr = _get_ocr()
            result, _ = ocr(img)
            result = result or []
            orows, cash, warn = _parse_shot(result, broker)
            n = len(orows)
            msg = (f"识别到 {n} 行持仓, 已高亮追加, 请核对后保存"
                   if n else "未识别到持仓行")
            return self._json({"msg": msg, "rows": orows, "cash": cash,
                               "warn": warn})
        except RuntimeError as e:
            return self._json({"msg": str(e)})
        except Exception as e:
            return self._json({"msg": f"识别失败: {e}"})

    def log_message(self, *a):
        pass


def main(port=8766):
    url = f"http://127.0.0.1:{port}"
    print(f"持仓编辑器已启动: {url}  (Ctrl+C退出)")
    print(f"  持仓编辑 -> {url}/   量化观察池 -> {url}/watch")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    p = 8766
    if "--port" in sys.argv:
        p = int(sys.argv[sys.argv.index("--port") + 1])
    main(p)
