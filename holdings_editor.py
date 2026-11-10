# -*- coding: utf-8 -*-
"""持仓/观察池本地编辑器: 浏览器维护母表 mystock_test.csv 与 量化观察池 watchlist.csv
启动: python holdings_editor.py [--port 8766]  ->  http://127.0.0.1:8766
两页签:
  ①持仓编辑: mystock_test.csv(唯一母表) 全行增删改, 保存自动备份.bak 并重算市值/盈亏(有现价成本时);
             点【一键分发】把母表原格式分发到 data/ttrade/holdings.csv 与 data/quant/holdings.csv
             (勾选'保存后自动分发'则每次保存后自动分发)。两子系统初始化只读各自的分发文件, 不读母表
  ②观察池: 量化账户手动观察池(代码/名称/备注), 【当前未启用, 见页内提示】原设计见 HINT_W
注意: 模板只用 .replace() 占位符, 禁止 %-format (CSS里100%会触发ValueError)"""
import csv
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

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
button{cursor:pointer;background:#3f4a5a;color:#fff;border:0;border-radius:6px;padding:7px 16px;font-size:14px;margin:10px 6px 0 0}
button.warn{background:#7a3a3a}
.msg{color:#7fd07f;margin-left:8px}
.hint{color:#9aa;font-size:12px;margin-top:10px}
</style></head><body>
<h1>__TITLE__</h1>
<div class=tabs>
<a href="/" class="__ON_H__">持仓编辑</a>
<a href="/watch" class="__ON_W__">量化观察池</a>
</div>
<div id=app>__TABLE__</div>
<button onclick="addRow()">+ 增行</button>
<button onclick="save()">💾 保存(自动备份.bak)</button>
__DIST__
<span class=msg id=msg></span>
<div class=hint>__HINT__</div>
<script>
var HEAD = __HEAD__;
function cell(v){return '<td><input value="'+(v==null?'':String(v).replace(/"/g,'&quot;'))+'"></td>'}
function addRow(){
  var tb=document.querySelector('#grid tbody');
  var tr=document.createElement('tr');
  tr.innerHTML=HEAD.map(function(){return cell('')}).join('')+'<td><button class=warn onclick="this.closest(\'tr\').remove()">删</button></td>';
  tb.appendChild(tr);
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
function distribute(){
  fetch('/distribute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})})
  .then(function(r){return r.json()})
  .then(function(j){document.getElementById('msg').textContent=j.msg;});
}
</script>
</body></html>"""

HINT_H = ("持仓文件 mystock_test.csv = 唯一母表(真实持仓快照)。保存时对'持股>0且成本/现价为数字'的行"
          "自动重算 市值=现价×持股 / 盈亏=(现价-成本)×持股 / 盈亏比。"
          "系统在初始化时只读各自分发文件: T 子系统读 data/ttrade/holdings.csv, "
          "量化子系统读 data/quant/holdings.csv —— 改完母表后请点【一键分发】(或勾选'保存后自动分发')。")
HINT_W = ("量化账户手动观察池(watchlist.csv): 【当前未启用(2026-10-20 撤回共振升级), "
          "填了也不会参与分配/建仓, 数据保留】原设计: 每个调仓日与智能选股合并分配资金(观察池优先); "
          "入池后等v2.2信号(趋势站上/超跌/冰点左侧)择机建仓, 退出=趋势腿破MA10; "
          "已在管理中的标的不会重复分配。代码填6位数字(如 600519)。")

DIST_BTN = ("<button onclick=\"distribute()\">📤 一键分发到两个子系统</button>"
            "<label style=\"color:#9aa;font-size:12px;margin-left:6px\">"
            "<input type=checkbox id=autoDist checked> 保存后自动分发"
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


def _distribute():
    """把母表 mystock_test.csv 按原格式分发到两个子系统持仓文件(各自先备份.bak)
    T 子系统 -> data/ttrade/holdings.csv ; 量化子系统 -> data/quant/holdings.csv"""
    head, rows = _read_csv(config.CSV_PATH)
    if not head:
        return "母表 mystock_test.csv 为空, 未分发"
    for p in (config.TT_HOLDINGS_CSV, config.QT_HOLDINGS_CSV):
        _save_csv(p, head, rows)
    return (f"已分发{len(rows)}行 -> data/ttrade/holdings.csv 与 "
            f"data/quant/holdings.csv(均已备份.bak)")


class Handler(BaseHTTPRequestHandler):
    def _html(self, title, table, head, on_h, hint):
        return (PAGE.replace("__TITLE__", title)
                    .replace("__TABLE__", table)
                    .replace("__HEAD__", json.dumps(head, ensure_ascii=False))
                    .replace("__ON_H__", "on" if on_h else "")
                    .replace("__ON_W__", "" if on_h else "on")
                    .replace("__DIST__", DIST_BTN if on_h else "")
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
            rows = [[str(c) for c in r] for r in payload.get("rows", [])]
        except Exception as e:
            return self._json({"msg": f"解析失败: {e}"})
        path = urlparse(self.path).path
        if path == "/distribute":
            try:
                return self._json({"msg": _distribute()})
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
