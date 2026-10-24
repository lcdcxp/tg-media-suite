#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把归档状态渲染成一张自包含的 HTML 看板。

视觉语言参照 MoviePilot v2（Vuetify 3 / Material Design 3，Materio 系模板）：
  · 页面底 #F4F5FA、白卡片、柔和多层阴影，不用发丝描边
  · 主色 MoviePilot 紫 #9155FD
  · 统计卡片配「带色底的圆角图标徽章」
  · 圆角 10px、留白宽松、信息用 chip 承载

数据可视化仍遵循 dataviz 规范：
  · 形式先于颜色；全页只有一个 hero 数字
  · 单系列柱状图用单色（主色），不需要图例
  · 图片/视频两系列的配色跑过验证器：
      浅色 #9155FD ↔ #0E8FBF  CVD ΔE 11.2 / 正常 21.1 / 对比度均 ≥3:1
      深色 #6E66ED ↔ #2A9DC4  CVD ΔE 13.2 / 正常 17.3 / 对比度均 ≥3:1
    （MoviePilot 原生的 info 蓝、warning 橙都没通过，故在其色系内另取）
  · 状态色沿用 MoviePilot 的 success/warning/error，但永远配图标+文字
  · 柱状图用 CSS 画而非 SVG —— SVG 配 preserveAspectRatio=none 会在宽屏上
    只横向拉伸，把柱子压扁、文字抻长

字体保持 system-ui：引入 Public Sans 之类的网络字体会新增外部依赖，
并把访问行为泄露给字体 CDN。
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

CST = timezone(timedelta(hours=8))

LIGHT = {
    "page": "#F4F5FA", "surface": "#ffffff",
    "ink": "#3A3541", "ink2": "#6E6B7B", "muted": "#A5A3AE",
    "line": "#E9E7EE", "track": "#EFEBFC",
    "s1": "#9155FD", "s2": "#0E8FBF", "bar": "#9155FD",
    "shadow": "0 2px 8px 0 rgba(58,53,65,.10), 0 0 2px 0 rgba(58,53,65,.06)",
    "chipbg": "#F4F2FB",
}
DARK = {
    "page": "#111827", "surface": "#161D2C",
    "ink": "#E7E3FC", "ink2": "#B6BEE3", "muted": "#7983BB",
    "line": "#252C3E", "track": "#2A2E42",
    "s1": "#6E66ED", "s2": "#2A9DC4", "bar": "#6E66ED",
    "shadow": "0 2px 10px 0 rgba(0,0,0,.40)",
    "chipbg": "#1E2537",
}
# MoviePilot 的状态色，浅深通用；永远与图标+文字同时出现
STATUS = {"good": "#56CA00", "warning": "#FFB400", "critical": "#FF4C51"}

# Material Design Icons 的 path（MoviePilot 用的就是 mdi）
ICON = {
    "database": "M12,3C7.58,3 4,4.79 4,7C4,9.21 7.58,11 12,11C16.42,11 20,9.21 20,7C20,4.79 16.42,3 12,3M4,9V12C4,14.21 7.58,16 12,16C16.42,16 20,14.21 20,12V9C20,11.21 16.42,13 12,13C7.58,13 4,11.21 4,9M4,14V17C4,19.21 7.58,21 12,21C16.42,21 20,19.21 20,17V14C20,16.21 16.42,18 12,18C7.58,18 4,16.21 4,14Z",
    "folder": "M22,4H14L12,2H6A2,2 0 0,0 4,4V16A2,2 0 0,0 6,18H22A2,2 0 0,0 24,16V6A2,2 0 0,0 22,4M2,6H0V11H0V20A2,2 0 0,0 2,22H20V20H2V6Z",
    "file": "M15,7H20.5L15,1.5V7M8,0H16L22,6V18A2,2 0 0,1 20,20H8C6.89,20 6,19.1 6,18V2A2,2 0 0,1 8,0M4,4V22H20V24H4A2,2 0 0,1 2,22V4H4Z",
    "check": "M12,2A10,10 0 0,1 22,12A10,10 0 0,1 12,22A10,10 0 0,1 2,12A10,10 0 0,1 12,2M11,16.5L18,9.5L16.59,8.09L11,13.67L7.91,10.59L6.5,12L11,16.5Z",
    "alert": "M13,13H11V7H13M13,17H11V15H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z",
    "image": "M8.5,13.5L11,16.5L14.5,12L19,18H5M21,19V5C21,3.89 20.1,3 19,3H5A2,2 0 0,0 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19Z",
    "video": "M17,10.5V7A1,1 0 0,0 16,6H4A1,1 0 0,0 3,7V17A1,1 0 0,0 4,18H16A1,1 0 0,0 17,17V13.5L21,17.5V6.5L17,10.5Z",
    "swap": "M6.45,17.45L1,12L6.45,6.55L7.86,7.96L4.83,11H15V13H4.83L7.86,16.04L6.45,17.45M17.55,14.5L16.14,13.09L19.17,10.05H9V8.05H19.17L16.14,5L17.55,3.6L23,9.05L17.55,14.5Z",
    "copy": "M19,21H8V7H19M19,5H8A2,2 0 0,0 6,7V21A2,2 0 0,0 8,23H19A2,2 0 0,0 21,21V7A2,2 0 0,0 19,5M16,1H4A2,2 0 0,0 2,3V17H4V3H16V1Z",
    "chevron": "M7.41,8.58L12,13.17L16.59,8.58L18,10L12,16L6,10L7.41,8.58Z",
    "account": "M12,4A4,4 0 0,1 16,8A4,4 0 0,1 12,12A4,4 0 0,1 8,8A4,4 0 0,1 12,4M12,14C16.42,14 20,15.79 20,18V20H4V18C4,15.79 7.58,14 12,14Z",
    "theme": "M12,18C11.11,18 10.26,17.8 9.5,17.45C11.56,16.5 13,14.42 13,12C13,9.58 11.56,7.5 9.5,6.55C10.26,6.2 11.11,6 12,6A6,6 0 0,1 18,12A6,6 0 0,1 12,18M20,8.69V4H15.31L12,0.69L8.69,4H4V8.69L0.69,12L4,15.31V20H8.69L12,23.31L15.31,20H20V15.31L23.31,12L20,8.69Z",
    "upload": "M9,16V10H5L12,3L19,10H15V16H9M5,20V18H19V20H5Z",
    "clock": "M12,20A8,8 0 0,0 20,12A8,8 0 0,0 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20M12,2A10,10 0 0,1 22,12A10,10 0 0,1 12,22C6.47,22 2,17.5 2,12A10,10 0 0,1 12,2M12.5,7V12.25L17,14.92L16.25,16.15L11,13V7H12.5Z",
    "flash": "M7,2V13H10V22L17,10H13L17,2H7Z",
    "cog": "M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,15.5M19.43,12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11L21.54,9.37C21.73,9.22 21.78,8.95 21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05C16.04,5.66 15.5,5.32 14.87,5.07L14.5,2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95,5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,8.73C2.21,8.95 2.27,9.22 2.46,9.37L4.57,11C4.53,11.34 4.5,11.67 4.5,12C4.5,12.33 4.53,12.65 4.57,12.97L2.46,14.63C2.27,14.78 2.21,15.05 2.34,15.27L4.34,18.73C4.46,18.95 4.73,19.03 4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 10,22H14C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.67 16.04,18.34 16.56,17.94L19.05,18.95C19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63L19.43,12.97Z",
}


def icon(name: str, size: int = 20, cls: str = "") -> str:
    return (f'<svg class="ic {cls}" viewBox="0 0 24 24" width="{size}" height="{size}" '
            f'aria-hidden="true"><path d="{ICON[name]}"/></svg>')


def human_bytes(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f} KB"
    return f"{n} B"


def fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone(CST).strftime("%m-%d %H:%M")
    except ValueError:
        return "—"


def ago(iso: str | None) -> str:
    if not iso:
        return "未知"
    try:
        dt = datetime.fromisoformat(iso).astimezone(CST)
    except ValueError:
        return "未知"
    sec = (datetime.now(CST) - dt).total_seconds()
    if sec < 90:
        return f"{int(sec)} 秒前"
    if sec < 5400:
        return f"{int(sec / 60)} 分钟前"
    if sec < 172800:
        return f"{int(sec / 3600)} 小时前"
    return f"{int(sec / 86400)} 天前"


# ── 图表：CSS 柱状图（不用 SVG，避免宽屏横向拉伸变形）────────────────
def daily_chart(batches: list[dict]) -> str:
    per_day: dict[str, int] = defaultdict(int)
    for b in batches:
        stamp = b.get("forwarded_at")
        if not stamp:
            continue
        try:
            day = datetime.fromisoformat(stamp).astimezone(CST).date()
        except ValueError:
            continue
        per_day[day.isoformat()] += b.get("bytes", 0)

    if not per_day:
        return '<p class="empty">暂无数据</p>'

    # 中间没有活动的日子必须补零，否则等距柱子会把时间轴画歪
    first = datetime.fromisoformat(min(per_day)).date()
    last = datetime.fromisoformat(max(per_day)).date()
    span = [(first + timedelta(days=i)).isoformat()
            for i in range((last - first).days + 1)]
    days = span[-14:]
    values = [per_day.get(d, 0) for d in days]
    peak = max(values) or 1

    cols = []
    for day, val in zip(days, values):
        pct = max(val / peak * 100, 1.5) if val else 1.5
        md = day[5:].replace("-", "/")
        # 只给峰值直接标注 —— 每根都标等于都没标
        tag = (f'<span class="pk">{human_bytes(val)}</span>'
               if val == peak and val else "")
        cols.append(
            f'<div class="col" title="{md}　{human_bytes(val)}">'
            f'{tag}<div class="bar" style="height:{pct:.1f}%"></div>'
            f'<span class="tk">{md}</span></div>'
        )
    return f'<div class="chart">{"".join(cols)}</div>'


def meter(label: str, text: str, pct: float, sev: str = "ok") -> str:
    return (f'<div class="meter"><div class="mh"><span>{html.escape(label)}</span>'
            f'<b>{html.escape(text)}</b></div>'
            f'<div class="track"><div class="fill f-{sev}" '
            f'style="width:{max(0.0, min(100.0, pct)):.1f}%"></div></div></div>')


def stat(icon_name: str, tone: str, value: str, label: str, big=False) -> str:
    return (f'<div class="card stat{" big" if big else ""}">'
            f'<div class="badge b-{tone}">{icon(icon_name, 22)}</div>'
            f'<div class="sv">{html.escape(value)}</div>'
            f'<div class="sl">{html.escape(label)}</div></div>')


def _vars(p: dict) -> str:
    return "".join(f"--{k}:{v};" for k, v in p.items())


# 模式标签用标记包起来。看板是归档器每 60 秒生成的静态文件，而模式由登录门
# 即时改写 —— 两者不同步会让页面最长骗人一分钟。所以登录门在吐页面时会
# 用实时配置替换掉这两个标记之间的内容。
MODE_A = "<!--mode-->"
MODE_B = "<!--/mode-->"
# 「正在归档」同理：批次可能跑十分钟，静态页面每 60 秒才生成一次，
# 光靠它看不到进度。登录门每次请求都读 progress.json 重画这一块。
WORK_A = "<!--work-->"
WORK_B = "<!--/work-->"


def work_card(prog: Optional[dict], pending: int) -> str:
    """正在归档 + 待归档。两者都由登录门实时改写。"""
    if not prog:
        if pending:
            return (f'<div class="card work idle"><div class="wt">'
                    f'{icon("clock", 17)}<b>待归档 {pending} 批</b></div>'
                    f'<div class="wd">定时模式下会等到点再传；实时模式下一轮就开始</div>'
                    f"</div>")
        return ""

    stage = "下载中" if prog.get("stage") == "download" else "上传到群晖"
    pct = max(0, min(100, int(prog.get("pct", 0))))
    done = human_bytes(int(prog.get("done", 0)))
    total = human_bytes(int(prog.get("bytes", 0)))
    title = str(prog.get("title") or prog.get("dir") or "")
    if len(title) > 34:
        title = title[:34] + "…"
    extra = f"　·　还有 {pending} 批待归档" if pending else ""
    return (
        f'<div class="card work"><div class="wt">{icon("upload", 17)}'
        f'<b>正在归档</b><span class="wstage">{stage}</span></div>'
        f'<div class="wname">{html.escape(title)}</div>'
        f'<div class="track"><div class="fill" style="width:{pct}%"></div></div>'
        f'<div class="wd">{done} / {total}　·　{pct}%{extra}</div></div>'
    )


def next_run_at(cfg: dict) -> Optional[datetime]:
    if cfg.get("mode") != "scheduled":
        return None
    try:
        hh, mm = (int(x) for x in str(cfg.get("start_time", "02:00")).split(":"))
    except ValueError:
        return None
    now = datetime.now(CST)
    today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return today if now < today else today + timedelta(days=1)


def mode_pill(cfg: dict) -> str:
    """当前模式的标签。登录门也会调它，所以只依赖 cfg，不依赖 runtime。"""
    if cfg.get("mode") == "scheduled":
        nxt = next_run_at(cfg)
        note = f'<em>· 下次 {fmt_time(nxt.isoformat())}</em>' if nxt else ""
        return (f'<a class="pill mode" href="/settings">{icon("clock", 15)}'
                f'定时 {html.escape(str(cfg.get("start_time", "")))} {note}</a>')
    return (f'<a class="pill mode" href="/settings">{icon("flash", 15)}'
            f'实时监控</a>')


def render(state: dict, runtime: dict) -> str:
    batches = list(state.get("batches", {}).values())
    batches.sort(key=lambda b: (b.get("archived_at") or "",
                                b.get("forwarded_at") or ""), reverse=True)

    n_batch = len(batches)
    n_files = sum(len(b.get("files", [])) for b in batches)
    n_bytes = sum(b.get("bytes", 0) for b in batches)
    n_photo = sum(b.get("photos", 0) for b in batches)
    n_video = sum(b.get("videos", 0) for b in batches)

    seen = runtime.get("total_in_chat", n_batch) or n_batch or 1
    complete = n_batch >= seen
    tx_used = runtime.get("tx_bytes", 0)
    tx_cap = 10 * (1 << 40)
    tx_pct = tx_used / tx_cap * 100

    alive = runtime.get("service_active", False)
    fails = runtime.get("failures", 0)
    stale = runtime.get("scan_age_sec", 0) > 600
    if not alive or fails:
        st_key, st_ic, st_word = "critical", "alert", "异常"
    elif stale:
        st_key, st_ic, st_word = "warning", "alert", "可能卡住"
    else:
        st_key, st_ic, st_word = "good", "check", "运行中"

    total_media = n_photo + n_video or 1
    p_pct = n_photo / total_media * 100

    mode = runtime.get("mode", "realtime")
    pending = runtime.get("pending", 0)
    pill = MODE_A + mode_pill({"mode": mode,
                               "start_time": runtime.get("start_time", "")}) + MODE_B

    rows = []
    for b in batches:
        full = b.get("title_full") or b.get("dir", "")
        clean = b.get("dir") or full          # 折叠版用清洗过的名字，否则 URL 开头的会露馅
        short = clean if len(clean) <= 26 else clean[:26] + "…"
        files = b.get("files", [])
        flist = "".join(
            f'<li><span class="fn">{html.escape(f["name"])}</span>'
            f'<span class="fs">{human_bytes(f["size"])}</span></li>' for f in files)
        path = "/volume1/video/tg/" + b.get("dir", "")
        when = (f'归档 {fmt_time(b["archived_at"])}'
                if b.get("archived_at") else "已归档")
        rows.append(
            f'<article class="card row" data-title="{html.escape(full.lower())}" '
            f'data-bytes="{b.get("bytes", 0)}" data-files="{len(files)}" '
            f'data-time="{html.escape(b.get("archived_at") or b.get("forwarded_at") or "")}">'
            f'<div class="rm">'
            f'<button class="rt" onclick="tg(this)">'
            f'<span class="short">{html.escape(short)}</span>'
            f'<span class="full">{html.escape(full)}</span></button>'
            f'<div class="chips">'
            f'<span class="chip">{len(files)} 文件</span>'
            f'<span class="chip">{human_bytes(b.get("bytes", 0))}</span>'
            f'<span class="chip"><i class="dot d1"></i>{b.get("photos", 0)} 图</span>'
            f'<span class="chip"><i class="dot d2"></i>{b.get("videos", 0)} 视频</span>'
            f"</div>"
            f'<div class="rtime">转发 {fmt_time(b.get("forwarded_at"))}'
            f'{icon("swap", 13, "sw")}{when}</div></div>'
            f'<div class="ra">'
            f'<button class="mini" onclick="cp(this,\'{html.escape(path)}\')">'
            f'{icon("copy", 15)}<span>路径</span></button>'
            f'<button class="mini" onclick="fl(this)">{icon("chevron", 15)}'
            f'<span>文件</span></button></div>'
            f'<ul class="files">{flist}</ul></article>'
        )

    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>归档看板</title>
<style>
:root{{color-scheme:light dark;{_vars(LIGHT)}
--good:{STATUS["good"]};--warning:{STATUS["warning"]};--critical:{STATUS["critical"]}}}
@media(prefers-color-scheme:dark){{:root:where(:not([data-theme=light])){{color-scheme:dark;{_vars(DARK)}}}}}
:root[data-theme=dark]{{color-scheme:dark;{_vars(DARK)}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--page);color:var(--ink);
font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;
-webkit-text-size-adjust:100%;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1180px;margin:0 auto;padding:20px 18px 40px}}
.ic{{fill:currentColor;display:block;flex:none}}

header{{display:flex;align-items:center;justify-content:space-between;
gap:12px;margin-bottom:20px;flex-wrap:wrap}}
h1{{font-size:19px;font-weight:600;margin:0;letter-spacing:.01em}}
.hl{{display:flex;align-items:center;gap:9px;flex-wrap:wrap;justify-content:flex-end}}
.pill{{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;
border-radius:999px;font-size:12.5px;font-weight:500;background:var(--surface);
box-shadow:var(--shadow)}}
.p-good{{color:var(--good)}}.p-warning{{color:var(--warning)}}.p-critical{{color:var(--critical)}}
.pill.mode{{color:var(--s1);text-decoration:none}}
.pill.mode:hover{{filter:brightness(1.1)}}
.pill em{{font-style:normal;color:var(--ink2);font-weight:400}}
.iconbtn{{display:inline-flex;align-items:center;gap:6px;background:var(--surface);
border:0;border-radius:999px;padding:7px 13px;color:var(--ink2);font:inherit;
font-size:12.5px;cursor:pointer;box-shadow:var(--shadow);text-decoration:none}}
.iconbtn:hover{{color:var(--s1)}}

.card{{background:var(--surface);border-radius:10px;box-shadow:var(--shadow)}}
.work{{padding:16px 18px;margin-bottom:14px;border-left:3px solid var(--s1)}}
.work.idle{{border-left-color:var(--warning)}}
.wt{{display:flex;align-items:center;gap:8px;font-size:13.5px;color:var(--s1)}}
.work.idle .wt{{color:var(--warning)}}
.wt b{{font-weight:600}}
.wstage{{margin-left:auto;font-size:12px;color:var(--muted);font-weight:400}}
.wname{{font-size:14px;color:var(--ink);margin:9px 0 10px;font-weight:500}}
.wd{{font-size:12px;color:var(--muted);margin-top:8px;font-variant-numeric:tabular-nums}}
.work .track{{height:6px}}
.grid{{display:grid;gap:14px;margin-bottom:14px}}
.g4{{grid-template-columns:repeat(4,1fr)}}
.g2{{grid-template-columns:1.55fr 1fr}}

.stat{{padding:18px 18px 16px}}
.badge{{width:40px;height:40px;border-radius:9px;display:flex;
align-items:center;justify-content:center;margin-bottom:13px}}
.b-1{{background:color-mix(in srgb,var(--s1) 14%,transparent);color:var(--s1)}}
.b-2{{background:color-mix(in srgb,var(--s2) 14%,transparent);color:var(--s2)}}
.b-g{{background:color-mix(in srgb,var(--good) 16%,transparent);color:var(--good)}}
.b-w{{background:color-mix(in srgb,var(--warning) 18%,transparent);color:var(--warning)}}
.sv{{font-size:25px;font-weight:600;line-height:1.15;letter-spacing:-.01em}}
.sl{{font-size:12.5px;color:var(--muted);margin-top:3px}}
.stat.big .sv{{font-size:48px;letter-spacing:-.025em;line-height:1.05}}
.stat.big{{padding:22px}}
.stat.big .sl{{font-size:13.5px;margin-top:6px}}

.pad{{padding:18px}}
h2{{font-size:13.5px;font-weight:600;margin:0 0 16px;color:var(--ink)}}
h2 span{{color:var(--muted);font-weight:400;font-size:12px;margin-left:7px}}

/* CSS 柱状图：flex + 百分比高度，任何宽度都不变形 */
.chart{{display:flex;align-items:flex-end;gap:6px;height:150px;padding-top:20px}}
.col{{flex:1;min-width:0;display:flex;flex-direction:column;
align-items:center;justify-content:flex-end;height:100%;position:relative}}
.bar{{width:100%;max-width:26px;background:var(--bar);
border-radius:5px 5px 0 0;transition:opacity .15s}}
.col:hover .bar{{opacity:.78}}
.pk{{position:absolute;top:-18px;font-size:10.5px;font-weight:600;
color:var(--ink2);white-space:nowrap}}
.tk{{font-size:10.5px;color:var(--muted);margin-top:7px;white-space:nowrap}}

.meter{{margin-bottom:15px}}
.meter:last-child{{margin-bottom:0}}
.mh{{display:flex;justify-content:space-between;align-items:baseline;
font-size:12.5px;color:var(--muted);margin-bottom:7px}}
.mh b{{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}}
.track{{height:8px;background:var(--track);border-radius:999px;overflow:hidden}}
.fill{{height:100%;border-radius:999px;background:var(--s1)}}
.f-warn{{background:var(--warning)}}.f-crit{{background:var(--critical)}}
.comp{{display:flex;height:8px;border-radius:999px;overflow:hidden;gap:2px}}
.comp span{{display:block;height:100%}}
.c1{{background:var(--s1);border-radius:999px 0 0 999px}}
.c2{{background:var(--s2);border-radius:0 999px 999px 0}}
.legend{{display:flex;gap:18px;margin-top:10px;font-size:12.5px;color:var(--ink2)}}
.legend span{{display:flex;align-items:center;gap:6px}}
.dot{{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none}}
.d1{{background:var(--s1)}}.d2{{background:var(--s2)}}

.tools{{display:flex;gap:10px;margin:20px 0 14px}}
input,select{{font:inherit;font-size:13.5px;padding:10px 14px;border-radius:8px;
border:0;background:var(--surface);color:var(--ink);box-shadow:var(--shadow)}}
input{{flex:1;min-width:0}}
input:focus,select:focus{{outline:2px solid var(--s1);outline-offset:-1px}}

.row{{padding:14px 16px;margin-bottom:10px;display:grid;
grid-template-columns:1fr auto;gap:10px;align-items:center}}
.rt{{background:none;border:0;padding:0;font:inherit;font-weight:500;
color:var(--ink);text-align:left;cursor:pointer;line-height:1.4}}
.rt .full{{display:none}}
.rt.open .short{{display:none}}.rt.open .full{{display:inline}}
.chips{{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}}
.chip{{display:inline-flex;align-items:center;gap:5px;background:var(--chipbg);
color:var(--ink2);font-size:11.5px;padding:3px 9px;border-radius:999px}}
.rtime{{display:flex;align-items:center;gap:6px;font-size:11.5px;
color:var(--muted);margin-top:8px;font-variant-numeric:tabular-nums}}
.sw{{opacity:.7}}
.ra{{display:flex;gap:6px}}
.mini{{display:inline-flex;align-items:center;gap:5px;background:var(--chipbg);
border:0;color:var(--ink2);border-radius:7px;padding:6px 10px;font-size:11.5px;
cursor:pointer;font-family:inherit;white-space:nowrap}}
.mini:hover{{color:var(--s1)}}
.files{{display:none;grid-column:1/-1;margin:12px 0 0;padding:12px 0 0;
list-style:none;border-top:1px solid var(--line)}}
.files.open{{display:block}}
.files li{{display:flex;justify-content:space-between;font-size:12px;
color:var(--ink2);padding:4px 0;font-variant-numeric:tabular-nums}}
.fn{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}}
.empty{{color:var(--muted);font-size:13px;text-align:center;padding:26px}}
footer{{color:var(--muted);font-size:11.5px;text-align:center;padding:18px 0 0}}

@media(max-width:900px){{
 .g4{{grid-template-columns:repeat(2,1fr)}}
 .g2{{grid-template-columns:1fr}}
}}
@media(max-width:560px){{
 .wrap{{padding:16px 13px 32px}}
 .stat.big .sv{{font-size:48px}}
 .row{{grid-template-columns:1fr}}
 .chart{{height:130px;gap:4px}}
 .tk{{font-size:9.5px}}
}}
</style></head><body>
<div class="wrap">

<header>
  <h1>归档看板</h1>
  <div class="hl">
    <span class="pill p-{st_key}">{icon(st_ic, 15)}{st_word}
      <em>· {ago(runtime.get("last_scan"))}</em></span>
    {pill}
    <button class="iconbtn" onclick="th()">{icon("theme", 15)}</button>
    <a class="iconbtn" href="/forward-sources">{icon("swap", 15)}<span>来源</span></a>
    <a class="iconbtn" href="/target">{icon("upload", 15)}<span>目标</span></a>
    <a class="iconbtn" href="/sources">{icon("account", 15)}<span>归档范围</span></a>
    <a class="iconbtn" href="/nas">{icon("database", 15)}<span>群晖</span></a>
    <a class="iconbtn" href="/settings">{icon("cog", 15)}</a>
    <a class="iconbtn" href="/account">{icon("account", 15)}<span>账号</span></a>
  </div>
</header>

{WORK_A}{work_card(runtime.get("progress"), pending)}{WORK_B}

<div class="grid g4">
  {stat("database", "1", human_bytes(n_bytes), "已同步到群晖", big=True)}
  {stat("folder", "2", str(n_batch), "已归档批次")}
  {stat("file", "1", str(n_files), "已归档文件")}
  {stat("check" if complete else "alert", "g" if complete else "w",
        f"{n_batch}/{seen}", "完整度")}
</div>

<div class="grid g2">
  <section class="card pad">
    <h2>每日转发<span>按你回复「转发」的时间统计</span></h2>
    {daily_chart(batches)}
  </section>
  <section class="card pad">
    <h2>构成与配额</h2>
    <div class="meter">
      <div class="mh"><span>内容构成</span><b>{n_photo} 图 / {n_video} 视频</b></div>
      <div class="comp"><span class="c1" style="width:{p_pct:.1f}%"></span>
        <span class="c2" style="width:{100 - p_pct:.1f}%"></span></div>
      <div class="legend"><span><i class="dot d1"></i>图片 {n_photo}</span>
        <span><i class="dot d2"></i>视频 {n_video}</span></div>
    </div>
    <div style="height:18px"></div>
    {meter("本月出站流量", f"{tx_used / (1 << 40):.2f} / 10 TB", tx_pct,
           "crit" if tx_pct > 95 else "warn" if tx_pct > 80 else "ok")}
  </section>
</div>

<div class="tools">
  <input id="q" placeholder="搜索标题…" oninput="flt()">
  <select id="sort" onchange="flt()">
    <option value="time">按时间</option>
    <option value="size">按大小</option>
    <option value="count">按文件数</option>
  </select>
</div>
<div id="list">{"".join(rows) or '<p class="empty">还没有归档任何批次</p>'}</div>

<footer>磁盘剩余 {runtime.get("disk_free", "—")} ·
{f'<b style="color:var(--warning)">待归档 {pending} 批</b> ·' if pending else '待处理 0 ·'}
失败 {fails} · 生成于 {datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}</footer>
</div>
<script>
function th(){{const r=document.documentElement;
 const d=r.getAttribute('data-theme')==='dark';
 r.setAttribute('data-theme',d?'light':'dark');
 localStorage.setItem('t',d?'light':'dark')}}
(function(){{const t=localStorage.getItem('t');
 if(t)document.documentElement.setAttribute('data-theme',t)}})();
function tg(b){{b.classList.toggle('open')}}
function fl(b){{b.closest('.row').querySelector('.files').classList.toggle('open')}}
function cp(b,p){{navigator.clipboard.writeText(p).then(()=>{{
 const s=b.querySelector('span'),o=s.textContent;
 s.textContent='已复制';setTimeout(()=>s.textContent=o,1200)}})}}
function flt(){{
 const q=document.getElementById('q').value.trim().toLowerCase();
 const s=document.getElementById('sort').value;
 const L=document.getElementById('list');
 let rs=[...L.querySelectorAll('.row')];
 rs.forEach(r=>r.style.display=(!q||r.dataset.title.includes(q))?'':'none');
 rs.sort((a,b)=>s==='size'?b.dataset.bytes-a.dataset.bytes
  :s==='count'?b.dataset.files-a.dataset.files
  :(b.dataset.time>a.dataset.time?1:-1));
 rs.forEach(r=>L.appendChild(r));
}}
setTimeout(()=>location.reload(),60000);
</script></body></html>"""
