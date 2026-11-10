# -*- coding: utf-8 -*-
"""实验策略对比运行器
同一回放器跑 生产基线prod vs 实验策略, 输出逐标的对比与可选HTML报告
用法:
  python run_lab.py --list                  列出全部已注册策略
  python run_lab.py -s gap_exit             prod vs gap_exit 逐标的对比(控制台)
  python run_lab.py -s gap_exit --html      追加生成 report/dashboard_lab.html
  python run_lab.py -s gap_exit -c 600613,600557   只跑指定标的
口径: 数据=bt_daily_cache.json(与run_backtest.py同源), 期初资金=BT_CAPITAL(钉住基线)"""
import argparse
import json
import os

import config_common as config
from backtest.daily import BT_CAPITAL, POOL
from lab import minute as _minute_mod
from lab.base import load_universe, replay_stock
from lab.strategies import REGISTRY
from core.indicators import build_ind

REPORT = os.path.join(config.REPORT_DIR, "dashboard_lab.html")


def run_compare(names, capital, codes, minute=False):
    """依次回放各策略 -> {name: {code: res}}; names[0]固定为prod基线
    minute=True: 非prod策略走5m分钟级回放(lab.minute, Baostock数据), prod基线仍走日线
    分钟级窗口与日线缓存末日对齐(严格同窗口对比, 排除窗口差异干扰)"""
    uni = load_universe()
    out = {}
    end_d = None
    if minute:
        b0 = (uni.get(codes[0]) or {}).get("bars") or []
        end_d = b0[-1]["dt"][:10] if b0 else None
    for nm in names:
        strat = REGISTRY[nm]()
        if minute and nm != "prod":
            out[nm] = {c: _minute_mod.replay_stock_minute(
                c, uni[c].get("name", c), capital, strat,
                end=end_d or _minute_mod.END) for c in codes}
        else:
            out[nm] = {c: replay_stock(uni, c, capital, strat) for c in codes}
    return out


def _avg_returns(res_by_code):
    rs = [r["return"] for r in res_by_code.values()]
    return sum(rs) / len(rs) if rs else 0.0


def print_compare(names, results, codes):
    base_nm, base = names[0], results[names[0]]
    hdr = f"{'标的':<14}" + "".join(f"{nm + '收益':>12}{'回撤':>8}{'笔数':>6}"
                                    for nm in names) + f"{'Δpp vs ' + base_nm:>12}"
    print(hdr)
    print("-" * len(hdr))
    for c in codes:
        b = base[c]
        line = f"{b['name'] + '(' + c + ')':<14}"
        for nm in names:
            r = results[nm][c]
            line += f"{r['return'] * 100:>+11.2f}%{r['mdd'] * 100:>7.1f}%{r['trades']:>6}"
        for nm in names[1:]:
            d = (results[nm][c]["return"] - b["return"]) * 100
            line += f"{d:>+11.2f}"
        print(line)
    print("-" * len(hdr))
    avg = f"{'等权平均':<14}"
    for nm in names:
        a = _avg_returns(results[nm])
        avg += f"{a * 100:>+11.2f}%{'':>7}{'':>6}"
    for nm in names[1:]:
        d = (_avg_returns(results[nm]) - _avg_returns(base)) * 100
        avg += f"{d:>+11.2f}"
    print(avg)


# ============================== HTML 报告 ==============================
def _stock_kline(uni, code, names, results):
    """单标的全窗口日K payload(与实盘看板同字段): K线/量额/涨跌幅/MA5-10-20 +
    各策略各自的成交ops(收盘信号、次日开盘价成交), 供同图分色三角对比"""
    bars = uni[code]["bars"]
    ind = build_ind(bars)                       # 均线用全量历史
    chg = [None]
    for i in range(1, len(bars)):
        pc = bars[i - 1]["close"]
        chg.append(round(bars[i]["close"] / pc - 1, 4) if pc else None)

    def _ma(k):
        return [None if v is None else round(v, 3) for v in ind[k]]

    ops_by = {}
    for nm in names:
        ops_by[nm] = [{"date": o["date"], "side": o["side"], "qty": o["qty"],
                       "price": o["price"], "reason": o.get("reason", ""),
                       "leg": o.get("leg", "")}
                      for o in results[nm][code]["ops"]]
    return {
        "days": [b["dt"][:10] for b in bars],
        "open": [round(b["open"], 3) for b in bars],
        "close_": [round(b["close"], 3) for b in bars],
        "high": [round(b["high"], 3) for b in bars],
        "low": [round(b["low"], 3) for b in bars],
        "vol": [round(b.get("vol") or 0, 1) for b in bars],
        "amount": [round(b.get("amount") or 0, 2) for b in bars],
        "chg": chg,
        "ma5": _ma("ma5"), "ma10": _ma("ma10"), "ma20": _ma("ma20"),
        "ops": ops_by,
        "range_chg": round(bars[-1]["close"] / bars[0]["close"] - 1, 4),
    }


def _sample(eq, n):
    """权益曲线均匀采样到n点(分钟级6383点 -> 日线128点对齐, 保留形状)"""
    if len(eq) <= n:
        return [round(v, 2) for v in eq]
    step = len(eq) / n
    return [round(eq[min(int(i * step), len(eq) - 1)], 2) for i in range(n)]


def build_html(names, results, codes, capital, path, minute=False):
    uni = load_universe()
    n = min(len(results[nm][c]["equity"]) for nm in names for c in codes)
    eq_by = {nm: {c: _sample(results[nm][c]["equity"], n) for c in codes}
             for nm in names}
    payload = {
        "capital": capital,
        "minute": bool(minute),
        "names": names,
        "desc": {nm: REGISTRY[nm].desc for nm in names},
        "codes": codes,
        "stock_names": {c: results[names[0]][c]["name"] for c in codes},
        "stocks": {c: _stock_kline(uni, c, names, results) for c in codes},
        "combo": {nm: [round(sum(eq_by[nm][c][i] for c in codes)
                             / len(codes), 2) for i in range(n)] for nm in names},
        "rows": {nm: {c: {"return": results[nm][c]["return"],
                          "mdd": results[nm][c]["mdd"],
                          "trades": results[nm][c]["trades"],
                          "final": results[nm][c]["final"]}
                      for c in codes} for nm in names},
    }
    js = json.dumps(payload, ensure_ascii=False)
    html = _HTML.replace("__DATA__", js)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>策略实验室 · 新策略对比</title>
<style>
body{background:#14171c;color:#cfd6e4;font:14px/1.6 "Microsoft YaHei",sans-serif;margin:0;padding:18px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:22px 0 8px;color:#8fa3c8}
.sub{color:#7d8797;font-size:12px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}
.card{background:#1c212b;border:1px solid #2a3140;border-radius:8px;padding:12px 16px;min-width:170px}
.card .k{color:#7d8797;font-size:12px}.card .v{font-size:20px;font-weight:700;margin-top:4px}
.pos{color:#ff5c5c}.neg{color:#4bd94b}.mut{color:#7d8797}
table{border-collapse:collapse;width:100%;font-size:13px;background:#1c212b}
th{background:#3f4a5a;color:#fff;padding:8px 10px;text-align:left}
td{padding:7px 10px;border-top:1px solid #2a3140}
canvas{width:100%;display:block;background:#1c212b;border:1px solid #2a3140;border-radius:8px}
.chart{background:#1c212b;border:1px solid #2a3140;border-radius:8px;padding:12px;margin:10px 0 18px;position:relative}
.chart .t{font-size:13px;color:#9aa7bd;margin-bottom:6px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}
.lg{font-size:12px;color:#7d8797;margin-top:6px;line-height:1.7}
.tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11px;font-weight:600;margin:2px}
.tag.b{background:rgba(230,0,0,.16);color:#ff6b6b}
.tag.s{background:rgba(0,168,0,.16);color:#4bd94b}
.tip{position:absolute;display:none;z-index:9;background:rgba(18,24,36,.97);border:1px solid #2a3140;
border-radius:8px;padding:8px 10px;font-size:12px;line-height:1.65;color:#cfd6e4;pointer-events:none;
width:max-content;max-width:300px;white-space:normal;box-shadow:0 8px 20px rgba(0,0,0,.5)}
.tip .tt{color:#fff;font-weight:700;margin-bottom:3px}.tip .mut{color:#9aa7bd}
.note{color:#7d8797;font-size:12px;margin-top:8px}
.rule{background:#1c212b;border-left:3px solid #f0a020;padding:10px 14px;margin:10px 0;color:#cfd6e4}
</style></head><body>
<h1>🧪 策略实验室 · 新策略对比（与生产隔离）</h1>
<div class="sub" id="sub"></div>
<div class="cards" id="cards"></div>
<h2>实验规则说明</h2><div id="rules"></div>
<h2>等权组合权益曲线（6只标的平均，期初同资金）</h2>
<canvas id="cv" width="1200" height="320"></canvas>
<h2>逐标的日K与买卖操作点</h2>
<div class="lg">读法：蜡烛红涨绿跌；<b style="color:#b06bd6">紫线MA5</b> / <b style="color:#f0a020">橙线MA10</b> / <b style="color:#3f7fd0">蓝线MA20</b>；
▲=买入（标在成交价下方）、▼=卖出（标在成交价上方），<b>三角颜色=策略</b>（见各图图例，同价多策略横向错开）；
<b>鼠标移到K线上</b>可看当日开收高低/振幅/量额/均线及各策略当天操作与原因。所有成交均为「收盘出信号、次日开盘价成交」。</div>
<div id="lstocks"></div>
<h2>逐标的对比</h2><div id="tbl"></div>
<h2>晋升流程（人工确认有效后才并入生产）</h2>
<div class="rule">① 规则并入 core/adaptive.py（config 开关，全局参数不逐股调参）→
② backtest/daily.py run_ps_stock 同步并入 → ③ python run_backtest.py 验证基线 →
④ replay 一致性验证 → ⑤ 落地生产并归档本实验。<b>Δ 不显著或为负：直接否决，实验留档不上线。</b></div>
<div class="note">本模块(lab/)与生产系统完全隔离：quant_daily/监控进程不引用本模块，实验结果不影响任何实盘行为 |
数据=bt_daily_cache.json（与 run_backtest.py 同源）| 收盘判定信号、次日开盘价成交（无未来函数）</div>
<script>
const D=__DATA__;
const CLR=['#8fa3c8','#4aa3ff','#f0a020','#b06bd6'];
const pct=x=>(x>=0?'+':'')+(x*100).toFixed(2)+'%';
const cls=x=>x>=0?'pos':'neg';
document.getElementById('sub').textContent=
 `期初资金(钉住基线) ${D.capital.toLocaleString()} 元 | 标的 ${D.codes.length} 只 | 基线=prod(生产v2.2)`
 +(D.minute?' | 分钟级5m回放(Baostock, 权益曲线采样对齐日线点数)':'');
let ch='';
D.names.forEach((nm,i)=>{
 const rows=D.rows[nm],cs=D.codes;
 const avg=cs.reduce((s,c)=>s+rows[c].return,0)/cs.length;
 const base=i?D.rows[D.names[0]]:null;
 const d=i?avg-cs.reduce((s,c)=>s+base[c].return,0)/cs.length:null;
 ch+=`<div class="card"><div class="k">${nm}${i?'':'（生产基线）'}</div>
 <div class="v ${cls(avg)}">${pct(avg)}</div>
 ${i?`<div class="k">Δ vs prod <span class="${cls(d)}">${(d>=0?'+':'')+(d*100).toFixed(2)}pp</span></div>`:''}</div>`;
});
document.getElementById('cards').innerHTML=ch;
document.getElementById('rules').innerHTML=D.names.map((nm,i)=>
 `<div class="rule"><b style="color:${CLR[i%4]}">${nm}</b>：${D.desc[nm]||'-'}</div>`).join('');
// 组合权益曲线
const cv=document.getElementById('cv'),ctx=cv.getContext('2d');
function draw(){
 const W=cv.width=cv.clientWidth*2,H=cv.height=640;ctx.scale(1,1);
 const all=D.names.flatMap(nm=>D.combo[nm]);
 let mn=Math.min(...all),mx=Math.max(...all),pad=(mx-mn)*0.06||1;mn-=pad;mx+=pad;
 const n=D.combo[D.names[0]].length;
 const X=i=>60+i*(W-90)/(n-1),Y=v=>H-40-(v-mn)/(mx-mn)*(H-70);
 ctx.clearRect(0,0,W,H);ctx.strokeStyle='#2a3140';ctx.fillStyle='#7d8797';ctx.font='20px sans-serif';
 for(let g=0;g<=4;g++){const v=mn+(mx-mn)*g/4,y=Y(v);
  ctx.beginPath();ctx.moveTo(60,y);ctx.lineTo(W-30,y);ctx.stroke();
  ctx.fillText((v/10000).toFixed(1)+'万',6,y+6);}
 D.names.forEach((nm,k)=>{
  ctx.strokeStyle=CLR[k%4];ctx.lineWidth=k?3:2;ctx.beginPath();
  D.combo[nm].forEach((v,i)=>i?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v)));ctx.stroke();
  ctx.fillStyle=CLR[k%4];ctx.fillText(nm,W-140,30+k*26);});
}
draw();addEventListener('resize',draw);
// 逐标表
let h='<table><tr><th>标的</th>';
D.names.forEach(nm=>{h+=`<th>${nm}收益</th><th>回撤</th><th>笔数</th>`;});
for(let i=1;i<D.names.length;i++)h+=`<th>Δpp</th>`;
h+='</tr>';
D.codes.forEach(c=>{
 h+=`<tr><td>${D.stock_names[c]}(${c})</td>`;
 D.names.forEach(nm=>{const r=D.rows[nm][c];
  h+=`<td class="${cls(r.return)}">${pct(r.return)}</td><td>${(r.mdd*100).toFixed(1)}%</td><td>${r.trades}</td>`;});
 for(let i=1;i<D.names.length;i++){
  const d=D.rows[D.names[i]][c].return-D.rows[D.names[0]][c].return;
  h+=`<td class="${cls(d)}">${(d>=0?'+':'')+(d*100).toFixed(2)}</td>`;}
 h+='</tr>';});
h+='</table>';
document.getElementById('tbl').innerHTML=h;

// ---------- 逐标的日K + 各策略买卖点(鼠标悬停查看当日详情) ----------
window.__lab_st={};
(function(){
 const S=D.stocks,K=D.names;
 const legCN=x=>({trend:'趋势腿',scalp:'超跌腿',ice:'冰点左侧'}[x]||x||'-');
 const sideCN=x=>x==='BUY'?'买入':x==='SELL'?'卖出':'止损';
 let html='';
 for(const c of D.codes){const s=S[c];
  const tr=K.map((nm,k)=>{const r=D.rows[nm][c];
   return `<span style="color:${CLR[k%4]}">● ${nm} <b class="${r.return>=0?'pos':'neg'}">${pct(r.return)}</b> 回撤${(r.mdd*100).toFixed(1)}% ${r.trades}笔</span>`;}).join('　');
  html+=`<div class="chart"><div class="t">
   <span><b>${D.stock_names[c]} (${c})</b>　${s.days[0].slice(5)}~${s.days[s.days.length-1].slice(5)} 标的区间 ${pct(s.range_chg)}</span>
   <span>${tr}</span></div>
   <canvas id="lc_${c}" height="200"></canvas>
   <div id="ltags_${c}" style="font-size:12px;color:#7d8797;margin-top:6px"></div>
   <div class="tip" id="ltip_${c}"></div></div>`;}
 document.getElementById('lstocks').innerHTML=html;
 for(const c of D.codes){let tg='';
  K.forEach((nm,k)=>{(S[c].ops[nm]||[]).slice(-6).forEach(o=>{
   tg+=`<span class="tag ${o.side==='BUY'?'b':'s'}" style="border-left:3px solid ${CLR[k%4]};cursor:pointer" title="点击定位到该日K线 · ${nm} | ${o.reason} | ${legCN(o.leg)}" onclick="window.__labShowTipDate('${c}','${String(o.date).slice(0,10)}',event.clientX,event.clientY)">[${nm}] ${o.date.slice(5)} ${sideCN(o.side)} ${o.qty}@${o.price.toFixed(3)}</span>`;});});
  document.getElementById('ltags_'+c).innerHTML=tg||'<span style="color:#667">窗口内无操作</span>';}

 window.__drawLab=function(c,hover){const cv=document.getElementById('lc_'+c);if(!cv)return;
  cv.width=cv.clientWidth*2;cv.height=200*2;const ctx=cv.getContext('2d');
  const W=cv.width,H=cv.height,s=S[c],pad=40*2;
  ctx.clearRect(0,0,W,H);
  const mn=Math.min(...s.low),mx=Math.max(...s.high);
  const X=i=>pad+(W-2*pad)*i/Math.max(1,s.days.length-1),Y=v=>H-pad-(H-2*pad)*(v-mn)/(mx-mn);
  ctx.strokeStyle='#232d3e';ctx.fillStyle='#7d8797';ctx.font='18px sans-serif';ctx.lineWidth=1;
  for(let g=0;g<=4;g++){const v=mx-(mx-mn)*g/4,y=pad+(H-2*pad)*g/4;
   ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(W-pad,y);ctx.stroke();
   ctx.fillText(v.toFixed(2),W-pad+6,y+6);}
  const step=Math.max(1,Math.ceil(s.days.length/6));
  for(let i=0;i<s.days.length;i+=step)ctx.fillText(s.days[i].slice(5),X(i)-24,H-14);
  const bw=Math.max(1.5,(W-2*pad)/s.days.length*0.62);
  for(let i=0;i<s.days.length;i++){const o=s.open[i],cln=s.close_[i],hi=s.high[i],lo=s.low[i];
   const col=cln>=o?'#e60000':'#00a800';ctx.strokeStyle=col;ctx.fillStyle=col;
   ctx.beginPath();ctx.moveTo(X(i),Y(hi));ctx.lineTo(X(i),Y(lo));ctx.stroke();
   const y1=Y(Math.max(o,cln)),y2=Y(Math.min(o,cln));ctx.fillRect(X(i)-bw/2,y1,bw,Math.max(1,y2-y1));}
  [['ma5','#b06bd6'],['ma10','#f0a020'],['ma20','#3f7fd0']].forEach(([k,col])=>{
   ctx.strokeStyle=col;ctx.lineWidth=1.6;ctx.beginPath();let started=false;
   s[k].forEach((v,i)=>{if(v==null)return;started?ctx.lineTo(X(i),Y(v)):(ctx.moveTo(X(i),Y(v)),started=true)});ctx.stroke();});
  // 买卖三角: 形状=方向(▲买/▼卖), 颜色=策略; 同日多策略横向错开, 深色描边区分
  const nk=K.length;
  K.forEach((nm,k)=>(s.ops[nm]||[]).forEach(op=>{
   const i=s.days.indexOf(String(op.date).slice(0,10));if(i<0)return;
   const x=X(i)+(k-(nk-1)/2)*9,y=Y(op.price);
   ctx.beginPath();
   if(op.side==='BUY'){ctx.moveTo(x,y+12);ctx.lineTo(x-7,y+25);ctx.lineTo(x+7,y+25);}
   else{ctx.moveTo(x,y-12);ctx.lineTo(x-7,y-25);ctx.lineTo(x+7,y-25);}
   ctx.closePath();ctx.fillStyle=CLR[k%4];ctx.fill();
   ctx.strokeStyle='#14171c';ctx.lineWidth=1;ctx.stroke();}));
  if(hover!=null&&hover>=0&&hover<s.days.length){        // 十字准线+当日收盘高亮
   const hx=X(hover),hy=Y(s.close_[hover]);
   ctx.strokeStyle='#8899aa';ctx.lineWidth=1;ctx.setLineDash([5,5]);
   ctx.beginPath();ctx.moveTo(hx,pad);ctx.lineTo(hx,H-pad);ctx.stroke();
   ctx.beginPath();ctx.moveTo(pad,hy);ctx.lineTo(W-pad,hy);ctx.stroke();
   ctx.setLineDash([]);ctx.fillStyle='#e8edf5';ctx.beginPath();ctx.arc(hx,hy,5,0,7);ctx.fill();}
  window.__lab_st[c]={pad:pad,W:W,n:s.days.length};};

 window.__labHide=function(c){const el=document.getElementById('ltip_'+c);
  if(el)el.style.display='none';window.__drawLab(c,null);};
 // 构造某日浮层HTML(十字线/标签点击共用)
 window.__labTip=function(c,i){const s=S[c];
  const d=s.days[i],o=s.open[i],cl=s.close_[i],hi=s.high[i],lo=s.low[i];
  const ch=s.chg[i]!=null?s.chg[i]*100:null;
  const pc=i>0?s.close_[i-1]:o;
  const cc=(ch!=null&&ch<0)?'neg':'pos';
  let hh=`<div class="tt">${d}</div>`
   +`<div>开 ${o.toFixed(3)} · 收 <b class="${cc}">${cl.toFixed(3)}</b> · 涨跌 <b class="${cc}">${ch==null?'-':(ch>=0?'+':'')+ch.toFixed(2)+'%'}</b></div>`
   +`<div>最高 ${hi.toFixed(3)} · 最低 ${lo.toFixed(3)} · 振幅 ${(((hi-lo)/pc)*100).toFixed(2)}%</div>`;
  const v=s.vol[i],amt=s.amount[i];
  if(v)hh+=`<div class="mut">成交 ${(v/10000).toFixed(2)}万手${amt?` · ${(amt/1e8).toFixed(2)}亿`:''}</div>`;
  if(s.ma10[i]!=null)hh+=`<div class="mut">MA5 ${s.ma5[i]!=null?s.ma5[i].toFixed(3):'-'} · MA10 ${s.ma10[i].toFixed(3)} · MA20 ${s.ma20[i]==null?'-':s.ma20[i].toFixed(3)}</div>`;
  let lines='';
  K.forEach((nm,k)=>(s.ops[nm]||[]).filter(p=>String(p.date).slice(0,10)===d).forEach(p=>{
   lines+=`<div><span style="color:${CLR[k%4]};font-weight:700">${nm}</span>
    <b class="${p.side==='BUY'?'pos':'neg'}">${sideCN(p.side)}</b> 次日开盘 ${p.qty}股@${p.price.toFixed(3)}
    <span class="mut">${p.reason} · ${legCN(p.leg)}</span></div>`;}));
  if(lines)hh+='<div style="margin-top:4px;border-top:1px solid #2a3446;padding-top:4px">'+lines+'</div>';
  return hh;};
 window.__labShowTip=function(c,i,x,y){const cv=document.getElementById('lc_'+c);
  const s=S[c];if(!cv||!s||i<0||i>=s.days.length)return;
  window.__drawLab(c,i);
  const el=document.getElementById('ltip_'+c);
  el.innerHTML=window.__labTip(c,i);el.style.display='block';
  const wrap=cv.parentElement.getBoundingClientRect();
  let lx=x-wrap.left+14,ly=y-wrap.top+14;
  if(lx+300>wrap.width)lx=Math.max(4,lx-316);
  if(ly+180>wrap.height)ly=Math.max(4,y-wrap.top-170);
  el.style.left=lx+'px';el.style.top=ly+'px';};
 window.__labShowTipDate=function(c,date,x,y){
  const s=S[c];if(!s)return;const i=s.days.indexOf(date);
  window.__labShowTip(c,i,x,y);};
 window.__labHover=function(c,ev){const cv=document.getElementById('lc_'+c);
  const st=window.__lab_st[c],s=S[c];if(!cv||!st)return;
  const rect=cv.getBoundingClientRect();
  const cx=(ev.clientX-rect.left)*(cv.width/rect.width);   // canvas 2倍缩放坐标换算
  const i=Math.max(0,Math.min(st.n-1,Math.round((cx-st.pad)/(st.W-2*st.pad)*(st.n-1))));
  window.__labShowTip(c,i,ev.clientX,ev.clientY);};

 for(const c of D.codes){window.__drawLab(c,null);
  const cv=document.getElementById('lc_'+c);if(!cv)continue;
  cv.addEventListener('mousemove',e=>window.__labHover(c,e));
  cv.addEventListener('mouseleave',()=>window.__labHide(c));}
 addEventListener('resize',()=>{for(const c of D.codes)window.__drawLab(c,null);});
})();
</script></body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="run_lab", description="策略实验室(与生产隔离)")
    ap.add_argument("--list", action="store_true", help="列出全部已注册策略")
    ap.add_argument("-s", "--strategy", default="",
                    help="实验策略名(逗号分隔多个); 自动携带prod基线对比")
    ap.add_argument("-c", "--codes", default="", help="只跑指定标的(逗号分隔)")
    ap.add_argument("--capital", type=float, default=BT_CAPITAL,
                    help="期初资金(默认钉住基线BT_CAPITAL)")
    ap.add_argument("--html", action="store_true",
                    help="生成 report/dashboard_lab.html")
    ap.add_argument("--minute", action="store_true",
                    help="分钟级模式: 实验策略走5m回放(lab.minute, Baostock数据), prod基线仍为日线")
    a = ap.parse_args(argv)
    if a.list or not a.strategy:
        print("已注册策略:")
        for nm, cls_ in REGISTRY.items():
            print(f"  {nm:<12} {cls_.desc}")
        if not a.strategy:
            print("\n用法: python run_lab.py -s <策略名> [--html]")
        return 0
    exps = [x.strip() for x in a.strategy.split(",") if x.strip()]
    for nm in exps:
        if nm not in REGISTRY:
            print(f"未知策略: {nm} (--list 查看已注册)")
            return 1
    names = ["prod"] + [x for x in exps if x != "prod"]
    codes = [x.strip() for x in a.codes.split(",") if x.strip()] or sorted(POOL)
    for c in codes:
        if c not in POOL:
            print(f"标的 {c} 不在回测池 POOL 内: {sorted(POOL)}")
            return 1
    print(f"[lab] 对比 {names} | 标的{len(codes)}只 | 期初{a.capital:,.2f}元(钉住基线)"
          + (" | 分钟级5m" if a.minute else ""))
    results = run_compare(names, a.capital, codes, minute=a.minute)
    print_compare(names, results, codes)
    if a.html:
        p = build_html(names, results, codes, a.capital, REPORT, minute=a.minute)
        print(f"[lab] HTML报告 -> {p}")
    print("[lab] 实验与生产隔离; 采纳请走晋升流程(见报告尾注)")
    return 0
