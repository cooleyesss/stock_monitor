# -*- coding: utf-8 -*-
"""微信推送(PushPlus) + HTML着色小工具"""
import requests
import config_common as config

DRY = False   # 干跑模式由 main.py 置True: 只打印不真实推送


_PPC_MAX = 19000          # pushplus服务端上限2万字, 留余量
_SEC_MARK = "<div style='font-weight:bold"   # sec()小节标题起始标记(分段切点)


def _send_one(title, content):
    try:
        r = requests.post("http://www.pushplus.plus/send", json={
            "token": config.PUSHPLUS_TOKEN, "title": title,
            "content": content, "template": "html",
        }, timeout=10)
        ok = r.json().get("code") == 200
        print(("已推送: " if ok else "推送失败: ") + title)
        if not ok:
            print("  pushplus返回:", r.text[:300])
        return ok
    except Exception as e:
        print("推送异常:", e)
        return False


def _split_content(content, limit=_PPC_MAX):
    """超限内容按小节边界贪心切分; 单节仍超限则硬切"""
    parts, buf = [], ""
    segs = content.split(_SEC_MARK)
    segs = [segs[0]] + [_SEC_MARK + s for s in segs[1:]]
    for s in segs:
        if buf and len(buf) + len(s) > limit:
            parts.append(buf)
            buf = ""
        if len(s) > limit:                      # 单节超限: 硬切
            while len(s) > limit:
                parts.append(s[:limit])
                s = s[limit:]
        buf += s
    if buf:
        parts.append(buf)
    return parts


def send(title, content):
    """推送微信; DRY模式仅本地打印; 超过pushplus2万字上限自动按小节分段"""
    if DRY:
        print(f"[DRY-推送] {title}")
        return False
    if len(content) <= _PPC_MAX:
        return _send_one(title, content)
    parts = _split_content(content)
    print(f"[推送] 正文{len(content)}字超限, 分{len(parts)}段推送")
    ok = True
    for i, seg in enumerate(parts, 1):
        ok = _send_one(f"{title}({i}/{len(parts)})", seg) and ok
    return ok


def colored(text, color):
    return f"<font color='{color}'>{text}</font>"


def bold(text):
    return f"<b>{text}</b>"


def sec(title):
    """报告小节标题"""
    return (f"<div style='font-weight:bold;font-size:14px;"
            f"border-left:4px solid #1a73e8;padding-left:6px;"
            f"margin:10px 0 4px;'>{title}</div>")


def table(headers, rows):
    """HTML表格: headers=列名list; rows=行list(单元格可含HTML着色)
    表头用深底白字+显式color, 微信深色/浅色模式下都清晰"""
    ths = "".join(
        f"<th style='border:1px solid #2b3442;background:#3f4a5a;"
        f"color:#ffffff;font-weight:bold;padding:4px 8px;"
        f"font-size:12px;white-space:nowrap;'>{h}</th>"
        for h in headers)
    trs = []
    for r in rows:
        tds = "".join(
            f"<td style='border:1px solid #eee;padding:4px 8px;"
            f"font-size:12px;white-space:nowrap;'>{c}</td>"
            for c in r)
        trs.append(f"<tr>{tds}</tr>")
    body = "".join(trs) or ("<tr><td colspan='%d' style='border:1px solid #eee;"
                            "padding:4px 8px;font-size:12px;color:#bbbbbb;'>无</td></tr>"
                            % max(len(headers), 1))
    return (f"<table style='border-collapse:collapse;width:100%;"
            f"margin:2px 0 6px;'>{f'<tr>{ths}</tr>'}{body}</table>")


def pnl_color(v):
    """按盈亏符号取色: 红=赚 绿=亏 灰=平"""
    return config.C_RED if v > 0 else (config.C_GREEN if v < 0 else config.C_GRAY)
