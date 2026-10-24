#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归档看板的登录门。

替代浏览器弹窗式的 HTTP Basic 认证：
  · 真正的登录页面（可以做样式、可以自动填充）
  · 自助修改密码
  · 会话用 HMAC 签名 Cookie，服务重启不掉线（无状态，不存会话表）
  · 登录失败限流，防爆破

只监听 127.0.0.1，由 nginx 反代，外网打不到这个端口。
看板 HTML 由 archiver 生成在 WEB_SRC，本进程只负责鉴权和吐文件 ——
所以它挂了不影响归档，归档挂了这里还能显示最后一次的状态。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import tempfile
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(os.environ.get("ARCHIVE_DATA_DIR", "/opt/tg-archive"))
AUTH_FILE = BASE / "auth.json"
CONFIG_FILE = BASE / "config.json"
PROGRESS_FILE = BASE / "progress.json"
STATE_FILE = BASE / "state.json"
WEB_SRC = BASE / "web" / "index.html"
PORT = int(os.environ.get("WEB_PORT", "8792"))
FORWARDER_DIR = Path(os.environ.get("FORWARDER_DATA_DIR", "/data/forwarder"))
FORWARDER_SOURCES_FILE = FORWARDER_DIR / "manual_sources.json"
FORWARDER_REQUESTS = FORWARDER_DIR / "control" / "requests"
FORWARDER_RESPONSES = FORWARDER_DIR / "control" / "responses"
NAS_KEY_FILE = Path(os.environ.get("NAS_KEY_FILE", "/root/.ssh/id_ed25519_nas"))
NAS_PUBLIC_KEY_FILE = Path(
    os.environ.get("NAS_PUBLIC_KEY_FILE", str(NAS_KEY_FILE) + ".pub")
)
NAS_KNOWN_HOSTS = BASE / "nas_known_hosts"

SESSION_HOURS = 24 * 30          # 一个月免登录
MIN_PASSWORD = 8
MAX_FAILS = 8                    # 同一 IP 十分钟内最多失败几次
FAIL_WINDOW = 600

# n=2^15 需要 128*n*r = 32 MiB，正好卡在 OpenSSL 默认上限上，必须显式放宽 maxmem
SCRYPT = {"n": 1 << 15, "r": 8, "p": 1, "maxmem": 64 << 20, "dklen": 32}

log = logging.getLogger("tg-archive-web")
_fails: dict[str, list] = {}     # ip -> [次数, 窗口起点]


# ---------------------------------------------------------------- 凭证


def load_auth() -> dict:
    return json.loads(AUTH_FILE.read_text(encoding="utf-8"))


def save_auth(data: dict) -> None:
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(AUTH_FILE)


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT).hex()


def check_password(auth: dict, password: str) -> bool:
    want = auth["hash"]
    got = hash_password(password, bytes.fromhex(auth["salt"]))
    return hmac.compare_digest(want, got)          # 定时安全比较


def set_password(auth: dict, password: str) -> None:
    salt = secrets.token_bytes(16)
    auth["salt"] = salt.hex()
    auth["hash"] = hash_password(password, salt)


# ---------------------------------------------------------------- 会话


def make_token(auth: dict) -> str:
    payload = json.dumps({"u": auth["user"],
                          "exp": int(time.time()) + SESSION_HOURS * 3600},
                         separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(payload).rstrip(b"=")
    sig = hmac.new(bytes.fromhex(auth["secret"]), body, hashlib.sha256).digest()
    return (body + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")).decode()


def valid_token(auth: dict, token: str) -> bool:
    try:
        body, sig = token.encode().split(b".", 1)
        pad = lambda b: b + b"=" * (-len(b) % 4)
        want = hmac.new(bytes.fromhex(auth["secret"]), body, hashlib.sha256).digest()
        if not hmac.compare_digest(want, base64.urlsafe_b64decode(pad(sig))):
            return False
        data = json.loads(base64.urlsafe_b64decode(pad(body)))
        return data.get("u") == auth["user"] and data.get("exp", 0) > time.time()
    except Exception:
        return False


def rate_limited(ip: str) -> bool:
    rec = _fails.get(ip)
    if not rec:
        return False
    count, start = rec
    if time.time() - start > FAIL_WINDOW:
        _fails.pop(ip, None)
        return False
    return count >= MAX_FAILS


def note_fail(ip: str) -> None:
    now = time.time()
    rec = _fails.get(ip)
    if not rec or now - rec[1] > FAIL_WINDOW:
        _fails[ip] = [1, now]
    else:
        rec[0] += 1


# ---------------------------------------------------------------- 页面


SHELL = """<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{title}</title>
<style>
:root{{color-scheme:light dark;
--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--border:rgba(11,11,11,0.10);--accent:#2a78d6;--bad:#d03b3b;--good:#0ca30c}}
@media(prefers-color-scheme:dark){{:root{{
--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;
--border:rgba(255,255,255,0.10);--accent:#3987e5}}}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--page);color:var(--ink);padding:24px;
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
.box{{background:var(--surface);border:1px solid var(--border);border-radius:16px;
padding:32px 28px;width:100%;max-width:360px}}
h1{{font-size:17px;font-weight:600;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:13px;margin:0 0 24px}}
label{{display:block;font-size:12.5px;color:var(--ink2);margin:14px 0 6px}}
input{{width:100%;font:inherit;font-size:15px;padding:11px 13px;border-radius:10px;
border:1px solid var(--border);background:var(--page);color:var(--ink)}}
input:focus{{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}}
button{{width:100%;margin-top:22px;font:inherit;font-size:15px;font-weight:500;
padding:12px;border:0;border-radius:10px;background:var(--accent);color:#fff;cursor:pointer}}
button:hover{{filter:brightness(1.07)}}
.msg{{display:flex;gap:7px;align-items:flex-start;font-size:13px;
padding:11px 13px;border-radius:9px;margin-bottom:18px;line-height:1.45}}
.msg.err{{background:rgba(208,59,59,.10);color:var(--bad)}}
.msg.ok{{background:rgba(12,163,12,.10);color:var(--good)}}
.msg i{{font-style:normal;flex:none}}
.foot{{margin-top:20px;text-align:center;font-size:12.5px}}
a{{color:var(--ink2);text-decoration:none}}a:hover{{color:var(--ink)}}
.hint{{color:var(--muted);font-size:11.5px;margin-top:7px}}
.box.wide{{max-width:440px}}
.widepage{{display:block}}
body:has(.widepage) .box{{max-width:520px}}
.opt{{display:block;border:1.5px solid var(--border);border-radius:10px;
padding:14px 15px;margin-bottom:11px;cursor:pointer;transition:border-color .15s}}
.opt.on{{border-color:var(--accent);background:rgba(145,85,253,.05)}}
.opt input{{width:auto;margin:0 8px 0 0;accent-color:var(--accent);vertical-align:middle}}
.ot{{font-weight:600;font-size:14.5px}}
.od{{display:block;color:var(--ink2);font-size:12.5px;margin-top:6px;line-height:1.5}}
.tw{{margin-top:4px;padding:14px 15px;border-radius:10px;background:var(--page)}}
.tw label{{margin-top:0}}
.tw input[type=time]{{font-variant-numeric:tabular-nums}}
.sd{{display:flex;align-items:center;gap:11px;border:1.5px solid var(--border);
border-radius:9px;padding:10px 13px;margin-bottom:7px;cursor:pointer}}
.sd.on{{border-color:var(--accent);background:rgba(145,85,253,.05)}}
.sd input{{width:auto;margin:0;accent-color:var(--accent);flex:none}}
.sinfo{{display:flex;flex-direction:column;gap:2px;min-width:0}}
.sname{{font-size:14px;font-weight:500;display:flex;align-items:center;gap:6px;
flex-wrap:wrap}}
.sstat{{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums}}
.tag{{font-size:10.5px;background:var(--accent);color:#fff;padding:1px 6px;
border-radius:999px;font-weight:400}}
.tag.me{{background:var(--good)}}
.uname{{font-size:11.5px;color:var(--muted);font-weight:400}}
#lst{{transition:opacity .2s}}
.tw select,.tw textarea{{width:100%;font:inherit;font-size:14px;padding:10px 12px;
border-radius:9px;border:1px solid var(--border);background:var(--page);color:var(--ink)}}
.tw textarea{{min-height:92px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:11px;line-height:1.45}}
.tw .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
.source-row{{display:flex;align-items:center;justify-content:space-between;gap:12px;
border:1px solid var(--border);border-radius:9px;padding:11px 12px;margin-bottom:8px}}
.source-main{{min-width:0}}.source-name{{font-weight:600;font-size:13.5px;overflow-wrap:anywhere}}
.source-kind{{font-size:11.5px;color:var(--muted);margin-top:2px}}
.inline{{margin:0;flex:none}}button.small{{width:auto;margin:0;padding:7px 10px;font-size:12px}}
button.danger{{background:var(--bad)}}button.secondary{{background:var(--ink2)}}
.steps{{margin:10px 0 0;padding-left:20px;color:var(--ink2);font-size:12.5px}}
.steps li{{margin:6px 0}}.status{{font-size:12.5px;margin:10px 0 0;color:var(--ink2)}}
@media(max-width:520px){{.tw .grid2{{grid-template-columns:1fr}}}}
</style></head><body><div class="box">{body}</div></body></html>"""


def msg_block(kind: str, text: str) -> str:
    if not text:
        return ""
    icon = "✕" if kind == "err" else "✓"
    return f'<div class="msg {kind}"><i>{icon}</i><span>{html.escape(text)}</span></div>'


def login_page(error: str = "", notice: str = "") -> bytes:
    body = f"""<h1>归档看板</h1><p class="sub">请登录</p>
{msg_block("err", error)}{msg_block("ok", notice)}
<form method="post" action="/login">
  <label for="u">账号</label>
  <input id="u" name="u" autocomplete="username" autocapitalize="off"
         autocorrect="off" spellcheck="false" required autofocus>
  <label for="p">密码</label>
  <input id="p" name="p" type="password" autocomplete="current-password" required>
  <button type="submit">登录</button>
</form>"""
    return SHELL.format(title="登录", body=body).encode("utf-8")


SENDERS_FILE = BASE / "senders.json"
DEFAULT_CONFIG = {"mode": "realtime", "start_time": "02:00",
                  "filter_senders": False, "allowed_senders": [],
                  "target_chat": "", "target_title": "",
                  "nas_host": os.environ.get("NAS_HOST", ""),
                  "nas_port": int(os.environ.get("NAS_PORT", "22")),
                  "nas_user": os.environ.get("NAS_USER", ""),
                  "nas_path": os.environ.get("NAS_PATH", "/volume1/video/tg/")}
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
NAS_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
NAS_USER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
NAS_PATH_RE = re.compile(r"/[A-Za-z0-9._/@+:-]+(?:/[A-Za-z0-9._@+:-]+)*/?")


def load_forward_sources() -> list[dict]:
    try:
        payload = json.loads(FORWARDER_SOURCES_FILE.read_text(encoding="utf-8"))
        rows = payload.get("sources", []) if isinstance(payload, dict) else []
    except (OSError, json.JSONDecodeError):
        return []
    sources = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("username"):
            continue
        sources.append({
            "username": str(row["username"]),
            "parser_type": (
                row.get("parser_type")
                if row.get("parser_type") in ("file", "folder", "channel")
                else "file"
            ),
        })
    return sorted(sources, key=lambda row: row["username"].lower())


def forwarder_request(action: str, **values) -> dict:
    request_id = secrets.token_hex(16)
    FORWARDER_REQUESTS.mkdir(parents=True, exist_ok=True)
    FORWARDER_RESPONSES.mkdir(parents=True, exist_ok=True)
    os.chmod(FORWARDER_REQUESTS.parent, 0o700)
    os.chmod(FORWARDER_REQUESTS, 0o700)
    os.chmod(FORWARDER_RESPONSES, 0o700)
    request_path = FORWARDER_REQUESTS / f"{request_id}.json"
    response_path = FORWARDER_RESPONSES / f"{request_id}.json"
    tmp = request_path.with_suffix(".tmp")
    payload = {
        "action": action,
        "expires_at": time.time() + 25,
        **values,
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(request_path)
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if response_path.exists():
                result = json.loads(response_path.read_text(encoding="utf-8"))
                response_path.unlink(missing_ok=True)
                return result
            time.sleep(0.2)
    finally:
        request_path.unlink(missing_ok=True)
    raise TimeoutError("转发服务未及时响应，请稍后重试")


def forward_sources_page(error: str = "", notice: str = "") -> bytes:
    rows = ""
    sources = load_forward_sources()
    for source in sources:
        username = html.escape(source["username"])
        mode = {
            "folder": "文件夹机器人模式",
            "channel": "频道/群组媒体模式",
        }.get(source["parser_type"], "普通文件机器人模式")
        rows += f"""<div class="source-row">
  <div class="source-main"><div class="source-name">{username}</div>
    <div class="source-kind">{mode}</div></div>
  <form method="post" action="/forward-sources" class="inline">
    <input type="hidden" name="action" value="remove">
    <input type="hidden" name="username" value="{username}">
    <button class="small danger" type="submit">删除</button>
  </form>
</div>"""
    if not rows:
        rows = '<p class="hint">暂时没有读取到来源配置。</p>'
    body = f"""<div class="widepage"><h1>转发来源</h1>
<p class="sub">在这些文件机器人、频道或群组中回复“转发”，系统会把完整媒体批次送到目标群</p>
{msg_block("err", error)}{msg_block("ok", notice)}
<div>{rows}</div>
<form method="post" action="/forward-sources" class="tw">
  <input type="hidden" name="action" value="add">
  <label for="source_username">来源对话</label>
  <input id="source_username" name="username" placeholder="频道/群组 ID、@用户名或 t.me 链接" required>
  <label for="parser_type">消息模式</label>
  <select id="parser_type" name="parser_type">
    <option value="file">普通文件模式（/start 获取文件）</option>
    <option value="folder">文件夹模式（带文件夹详情和媒体数量）</option>
    <option value="channel">频道/群组媒体模式（回复原消息或相册）</option>
  </select>
  <p class="hint">提交后会先用当前 Telegram 个人 Session 验证来源。普通频道/群组会自动按媒体模式处理。</p>
  <button type="submit">验证并添加</button>
</form>
<p class="foot"><a href="/">← 返回看板</a></p></div>"""
    return SHELL.format(title="转发来源", body=body).encode("utf-8")


def load_senders() -> dict:
    """归档器导出的「群里谁发过媒体」。它有 Telegram 会话，本进程没有。"""
    try:
        return json.loads(SENDERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"senders": [], "chat_title": "", "updated_at": ""}


def human_bytes(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f} KB"
    return f"{n} B"


def sources_page(cfg: dict, error: str = "", notice: str = "") -> bytes:
    data = load_senders()
    senders = data.get("senders", [])
    allow = set(cfg.get("allowed_senders") or [])
    on = "checked" if cfg.get("filter_senders") else ""

    if not senders:
        rows = ('<p class="hint" style="padding:14px 0">归档器还没统计出发送者。'
                '它每轮扫描时顺带收集，稍等一分钟刷新即可。</p>')
    else:
        rows = ""
        for s in senders:
            sid = int(s["id"])
            ck = "checked" if sid in allow else ""
            tags = ""
            if s.get("self"):
                tags += '<span class="tag me">你自己</span>'
            if s.get("bot"):
                tags += '<span class="tag">机器人</span>'
            uname = (f'<span class="uname">{html.escape(s["username"])}</span>'
                     if s.get("username") else "")
            note = ""
            if s.get("self") and not s.get("files"):
                note = ('<span class="sstat" style="color:var(--accent)">'
                        '还没发过 —— 但转发大于 50MB 的文件时会以你的身份发出，'
                        '建议一并勾上</span>')
            rows += (
                f'<label class="sd{" on" if ck else ""}">'
                f'<input type="checkbox" name="who" value="{sid}" {ck} onchange="sy()">'
                f'<span class="sinfo"><span class="sname">'
                f'{html.escape(str(s.get("name", sid)))}{tags}{uname}</span>'
                f'<span class="sstat">{s.get("batches", 0)} 批 · '
                f'{s.get("files", 0)} 文件 · {human_bytes(int(s.get("bytes", 0)))}</span>'
                f"{note}</span></label>"
            )

    chat = html.escape(str(data.get("chat_title") or "未知"))
    body = f"""<h1>监控范围</h1>
<p class="sub">当前群组：{chat}</p>
{msg_block("err", error)}{msg_block("ok", notice)}
<form method="post" action="/sources">
  <label class="opt {'on' if on else ''}" id="sw">
    <input type="checkbox" name="filter" value="1" {on} onchange="sy()">
    <span class="ot">只归档勾选的人</span>
    <span class="od">不勾这一项就是<b>全部都存</b>（现在的行为）。
      开启后，只有下面勾中的人发的内容才会归档。</span>
  </label>
  <div id="lst">
    <p class="hint" style="margin:16px 0 8px">谁发过媒体（按文件数排序）</p>
    {rows}
  </div>
  <button type="submit">保存</button>
</form>
<p class="foot"><a href="/">← 返回看板</a></p>
<script>
function sy(){{
  var on=document.querySelector('input[name=filter]').checked;
  document.getElementById('sw').classList.toggle('on',on);
  document.getElementById('lst').style.opacity=on?1:.42;
  document.querySelectorAll('.sd').forEach(function(o){{
    o.classList.toggle('on',o.querySelector('input').checked);
  }});
}}
sy();
</script>"""
    return SHELL.format(title="监控范围", body=body).encode("utf-8")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    if cfg.get("mode") not in ("realtime", "scheduled"):
        cfg["mode"] = "realtime"
    if not TIME_RE.fullmatch(str(cfg.get("start_time", ""))):
        cfg["start_time"] = DEFAULT_CONFIG["start_time"]
    cfg["filter_senders"] = bool(cfg.get("filter_senders"))
    try:
        cfg["allowed_senders"] = [int(x) for x in (cfg.get("allowed_senders") or [])]
    except (TypeError, ValueError):
        cfg["allowed_senders"] = []
    cfg["target_chat"] = str(cfg.get("target_chat") or "").strip()
    cfg["target_title"] = str(cfg.get("target_title") or "").strip()
    cfg["nas_host"] = str(cfg.get("nas_host") or "").strip()
    cfg["nas_user"] = str(cfg.get("nas_user") or "").strip()
    cfg["nas_path"] = str(cfg.get("nas_path") or DEFAULT_CONFIG["nas_path"]).strip()
    try:
        cfg["nas_port"] = int(cfg.get("nas_port") or DEFAULT_CONFIG["nas_port"])
    except (TypeError, ValueError):
        cfg["nas_port"] = DEFAULT_CONFIG["nas_port"]
    return cfg


def validate_nas_settings(values: dict) -> dict:
    host = str(values.get("nas_host") or "").strip()
    user = str(values.get("nas_user") or "").strip()
    path = str(values.get("nas_path") or "").strip()
    try:
        port = int(values.get("nas_port") or 0)
    except (TypeError, ValueError):
        raise ValueError("SSH 端口必须是数字") from None
    if not NAS_HOST_RE.fullmatch(host) or ".." in host:
        raise ValueError("NAS 主机名或 IP 不合法")
    if not 1 <= port <= 65535:
        raise ValueError("SSH 端口必须在 1 到 65535 之间")
    if not NAS_USER_RE.fullmatch(user):
        raise ValueError("NAS 用户名不合法")
    if not NAS_PATH_RE.fullmatch(path) or ".." in Path(path).parts:
        raise ValueError("目标目录必须是绝对路径，且不能包含空格或 ..")
    if not NAS_KEY_FILE.is_file():
        raise ValueError("VPS 上尚未安装 NAS SSH 私钥")
    return {
        "nas_host": host,
        "nas_port": port,
        "nas_user": user,
        "nas_path": path.rstrip("/") + "/",
    }


def nas_public_key() -> str:
    try:
        return NAS_PUBLIC_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        try:
            result = subprocess.run(
                ["ssh-keygen", "-y", "-f", str(NAS_KEY_FILE)],
                capture_output=True,
                text=True,
                timeout=8,
                check=True,
            )
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "未找到可用公钥"


def test_nas(settings: dict) -> str:
    host = settings["nas_host"]
    port = settings["nas_port"]
    with tempfile.TemporaryDirectory(prefix="nas-check-") as source_dir:
        scan = subprocess.run(
            ["ssh-keyscan", "-T", "8", "-p", str(port), host],
            capture_output=True,
            text=True,
            timeout=12,
        )
        host_keys = "\n".join(
            line for line in scan.stdout.splitlines()
            if line and not line.startswith("#")
        )
        if not host_keys:
            raise RuntimeError("无法读取群晖 SSH 主机密钥，请检查地址、端口和防火墙")

        temp_hosts = BASE / f".nas-known-hosts-{secrets.token_hex(8)}"
        try:
            temp_hosts.write_text(host_keys + "\n", encoding="utf-8")
            os.chmod(temp_hosts, 0o600)
            ssh_command = shlex.join([
                "ssh", "-p", str(port), "-i", str(NAS_KEY_FILE),
                "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=12", "-o", "UpdateHostKeys=no",
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={temp_hosts}",
            ])
            destination = (
                f"{settings['nas_user']}@{host}:{settings['nas_path']}"
            )
            check = subprocess.run(
                ["rsync", "-an", "--timeout=20", "-e", ssh_command,
                 source_dir.rstrip("/") + "/", destination],
                capture_output=True,
                text=True,
                timeout=35,
            )
            if check.returncode:
                detail = (check.stderr or check.stdout or "rsync 检测失败").strip()
                raise RuntimeError(detail[-300:])

            fingerprint = subprocess.run(
                ["ssh-keygen", "-lf", str(temp_hosts)],
                capture_output=True,
                text=True,
                timeout=8,
            ).stdout.splitlines()
            pinned = NAS_KNOWN_HOSTS.with_suffix(".tmp")
            pinned.write_text(host_keys + "\n", encoding="utf-8")
            os.chmod(pinned, 0o600)
            pinned.replace(NAS_KNOWN_HOSTS)
            return fingerprint[0].strip() if fingerprint else "主机密钥已固定"
        finally:
            temp_hosts.unlink(missing_ok=True)


def nas_page(cfg: dict, error: str = "", notice: str = "") -> bytes:
    host = html.escape(str(cfg.get("nas_host") or ""))
    port = html.escape(str(cfg.get("nas_port") or 22))
    user = html.escape(str(cfg.get("nas_user") or ""))
    path = html.escape(str(cfg.get("nas_path") or "/volume1/video/tg/"))
    pubkey = html.escape(nas_public_key())
    last = html.escape(str(cfg.get("nas_last_test_at") or "尚未在网页检测"))
    fingerprint = html.escape(str(cfg.get("nas_fingerprint") or ""))
    status = f"最近检测：{last}" + (f"<br>群晖主机密钥：{fingerprint}" if fingerprint else "")
    body = f"""<div class="widepage"><h1>群晖 NAS</h1>
<p class="sub">使用 SSH 密钥和 rsync 传输，不保存群晖密码</p>
{msg_block("err", error)}{msg_block("ok", notice)}
<form method="post" action="/nas" class="tw">
  <div class="grid2"><div><label for="nas_host">主机名或 IP</label>
    <input id="nas_host" name="nas_host" value="{host}" required></div>
  <div><label for="nas_port">SSH 端口</label>
    <input id="nas_port" name="nas_port" type="number" min="1" max="65535" value="{port}" required></div></div>
  <div class="grid2"><div><label for="nas_user">群晖用户</label>
    <input id="nas_user" name="nas_user" value="{user}" required></div>
  <div><label for="nas_path">目标目录</label>
    <input id="nas_path" name="nas_path" value="{path}" required></div></div>
  <p class="status">{status}</p>
  <button type="submit">测试 rsync 并保存</button>
</form>
<div class="tw">
  <label>VPS 公钥（只需添加公钥，不要上传私钥）</label>
  <textarea readonly onclick="this.select()">{pubkey}</textarea>
  <ol class="steps">
    <li>在 DSM 控制面板启用 SSH，并记下端口。</li>
    <li>给专用用户授予目标共享文件夹的读写权限。</li>
    <li>把上面的公钥加入该用户的 <b>~/.ssh/authorized_keys</b>。</li>
    <li>填写连接信息，点击“测试 rsync 并保存”；检测失败不会覆盖现有配置。</li>
  </ol>
</div>
<p class="foot"><a href="/">← 返回看板</a></p></div>"""
    return SHELL.format(title="群晖 NAS", body=body).encode("utf-8")


def target_page(cfg: dict, error: str = "", notice: str = "") -> bytes:
    target = html.escape(str(cfg.get("target_chat") or ""))
    title = html.escape(str(cfg.get("target_title") or "尚未识别"))
    body = f"""<h1>目标频道</h1><p class="sub">当前：{title}</p>
{msg_block("err", error)}{msg_block("ok", notice)}
<div class="tw">
  <label>频道/群组 ID</label>
  <input value="{target}" readonly>
  <p class="hint">为防止填错频道后停止归档，更换目标必须在 Telegram 私聊中完成：发送 <b>/forwarder_target_set</b>，然后提交频道 ID、@用户名或公开 t.me 链接。系统会先验证个人账号可访问且机器人能发消息，验证成功才切换。</p>
</div>
<p class="foot"><a href="/">← 返回看板</a></p>"""
    return SHELL.format(title="目标频道", body=body).encode("utf-8")


def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_FILE)


def read_progress() -> dict | None:
    """归档器当前在处理哪一批。空闲时文件不存在。"""
    try:
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_pending() -> int:
    """待归档数。取归档器上一轮写进 state 的快照值。"""
    try:
        s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return int(s.get("stats", {}).get("pending_snapshot", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def settings_page(cfg: dict, error: str = "", notice: str = "") -> bytes:
    rt = "checked" if cfg["mode"] == "realtime" else ""
    sc = "checked" if cfg["mode"] == "scheduled" else ""
    body = f"""<h1>归档模式</h1><p class="sub">改完立刻生效，无需重启</p>
{msg_block("err", error)}{msg_block("ok", notice)}
<form method="post" action="/settings">
  <label class="opt {'on' if rt else ''}">
    <input type="radio" name="mode" value="realtime" {rt} onchange="sy()">
    <span class="ot">实时监控</span>
    <span class="od">发现新批次就立刻归档，约 1~2 分钟内出现在群晖。
      传输随时可能发生，晚高峰会和你自己上网抢带宽。</span>
  </label>
  <label class="opt {'on' if sc else ''}">
    <input type="radio" name="mode" value="scheduled" {sc} onchange="sy()">
    <span class="ot">定时归档</span>
    <span class="od">平时只记录待办，每天到点一次性把积压的全部传完。
      跨太平洋链路在深夜最通畅，也不影响你白天上网。</span>
  </label>
  <div id="tw" class="tw">
    <label for="t">每天几点开跑</label>
    <input id="t" name="start_time" type="time" value="{html.escape(cfg['start_time'])}"
           step="60" required>
    <p class="hint">一旦开跑会把当天积压的全部处理完，不设结束时刻
      —— 免得大文件传一半被掐断。</p>
  </div>
  <button type="submit">保存</button>
</form>
<p class="foot"><a href="/">← 返回看板</a></p>
<script>
function sy(){{
  var s=document.querySelector('input[value=scheduled]').checked;
  document.getElementById('tw').style.display=s?'block':'none';
  document.querySelectorAll('.opt').forEach(function(o){{
    o.classList.toggle('on', o.querySelector('input').checked);
  }});
}}
sy();
</script>"""
    return SHELL.format(title="归档模式", body=body).encode("utf-8")


def account_page(user: str, error: str = "", notice: str = "") -> bytes:
    body = f"""<h1>修改密码</h1><p class="sub">当前账号 {html.escape(user)}</p>
{msg_block("err", error)}{msg_block("ok", notice)}
<form method="post" action="/account">
  <input type="text" name="u" value="{html.escape(user)}" autocomplete="username"
         style="display:none" tabindex="-1" aria-hidden="true">
  <label for="o">当前密码</label>
  <input id="o" name="old" type="password" autocomplete="current-password" required>
  <label for="n">新密码</label>
  <input id="n" name="new" type="password" autocomplete="new-password" required>
  <p class="hint">至少 {MIN_PASSWORD} 位</p>
  <label for="n2">再输一次</label>
  <input id="n2" name="new2" type="password" autocomplete="new-password" required>
  <button type="submit">保存</button>
</form>
<p class="foot"><a href="/">← 返回看板</a></p>"""
    return SHELL.format(title="修改密码", body=body).encode("utf-8")


# ---------------------------------------------------------------- 服务


TOAST = (
    '<div id="agfl" style="position:fixed;left:50%;top:16px;'
    'transform:translateX(-50%);z-index:99;display:flex;align-items:center;gap:9px;'
    'background:#3A3541;color:#fff;padding:11px 20px;border-radius:999px;'
    'font:13.5px/1.4 system-ui,-apple-system,sans-serif;'
    'box-shadow:0 6px 20px rgba(0,0,0,.22);max-width:92vw;'
    'transition:opacity .35s,transform .35s">'
    '<svg viewBox="0 0 24 24" width="16" height="16" style="fill:#56CA00;flex:none">'
    '<path d="M12,2A10,10 0 0,1 22,12A10,10 0 0,1 12,22A10,10 0 0,1 2,12A10,10 0 0,1'
    ' 12,2M11,16.5L18,9.5L16.59,8.09L11,13.67L7.91,10.59L6.5,12L11,16.5Z"/></svg>'
    '<span>{msg}</span></div>'
    '<script>setTimeout(function(){{var e=document.getElementById("agfl");'
    'if(e){{e.style.opacity=0;e.style.transform="translate(-50%,-10px)";'
    'setTimeout(function(){{e.remove()}},400)}}}},3000)</script>'
)


class Handler(BaseHTTPRequestHandler):
    server_version = "ag"
    sys_version = ""

    # ---- 基础工具 ----
    def client_ip(self) -> str:
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def cookies(self) -> dict:
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def logged_in(self, auth: dict) -> bool:
        return valid_token(auth, self.cookies().get("ag_sess", ""))

    def send(self, code: int, body: bytes, ctype="text/html; charset=utf-8",
             cookie: str | list[str] | None = None,
             location: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        for one in ([cookie] if isinstance(cookie, str) else (cookie or [])):
            self.send_header("Set-Cookie", one)
        if location:
            self.send_header("Location", location)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, to: str, cookie: str | list[str] | None = None) -> None:
        self.send(303, b"", cookie=cookie, location=to)

    def flash_cookie(self, text: str) -> str:
        """一次性提示。保存设置后直接回看板，靠它把确认信息带过去。"""
        return (f"ag_flash={urllib.parse.quote(text)}; Path=/; Max-Age=60; "
                f"HttpOnly; Secure; SameSite=Strict")

    CLEAR_FLASH = ("ag_flash=; Path=/; Max-Age=0; HttpOnly; Secure; "
                   "SameSite=Strict")

    def form(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 64 * 1024:
            return {}
        raw = self.rfile.read(n).decode("utf-8", "replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def session_cookie(self, token: str, days: int = 30) -> str:
        return (f"ag_sess={token}; Path=/; Max-Age={days * 86400}; "
                f"HttpOnly; Secure; SameSite=Strict")

    def same_origin(self) -> bool:
        """CSRF 兜底。主力防线是 SameSite=Strict Cookie，这里只是多一道。

        必须宽容：端口、大小写、以及沙箱环境下的 Origin: null 都不该误杀；
        拒绝时把实际收到的值记下来 —— 否则用户只看到 bad origin，无从排查。
        """
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return True

        def host_of(value: str) -> str:
            value = value.split("://", 1)[-1]
            return value.split("/")[0].split(":")[0].strip().lower()

        src = host_of(origin)
        host = self.headers.get("Host", "")
        fwd = self.headers.get("X-Forwarded-Host", "")
        allowed = {host_of(host)} | ({host_of(fwd)} if fwd else set())
        allowed.discard("")

        if src and src in allowed:
            return True
        log.warning("Origin 校验失败 origin=%r host=%r x-forwarded-host=%r",
                    origin, host, fwd)
        return False

    def dashboard(self) -> bytes:
        """看板由归档器每 60 秒生成一次，但模式是本进程即时改的。

        直接吐静态文件的话，改完模式后最长有一分钟显示的是旧模式 ——
        用户会以为没保存成功。所以这里用实时配置把模式标签重写一遍。
        """
        html_text = WEB_SRC.read_text(encoding="utf-8")
        try:
            import report

            def swap(text: str, mark_a: str, mark_b: str, fresh: str) -> str:
                a, b = text.find(mark_a), text.find(mark_b)
                if a == -1 or b <= a:
                    return text
                return text[:a + len(mark_a)] + fresh + text[b:]

            html_text = swap(html_text, report.MODE_A, report.MODE_B,
                             report.mode_pill(load_config()))
            html_text = swap(html_text, report.WORK_A, report.WORK_B,
                             report.work_card(read_progress(), read_pending()))
        except Exception:
            log.warning("实时区块改写失败，退回静态内容", exc_info=True)

        flash = self.cookies().get("ag_flash", "")
        if flash:
            try:
                msg = urllib.parse.unquote(flash)[:120]
                html_text = html_text.replace(
                    "<body>", "<body>" + TOAST.format(msg=html.escape(msg)), 1)
            except Exception:
                pass
        return html_text.encode("utf-8")

    # ---- 路由 ----
    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            auth = load_auth()
        except Exception:
            self.send(500, b"auth config missing")
            return

        if path == "/healthz":
            self.send(200, b"ok", "text/plain; charset=utf-8")
            return

        if not self.logged_in(auth):
            if path in ("/", "/account"):
                self.send(200, login_page())
            else:
                self.redirect("/")
            return

        if path == "/account":
            self.send(200, account_page(auth["user"]))
            return

        if path == "/settings":
            self.send(200, settings_page(load_config()))
            return

        if path == "/sources":
            self.send(200, sources_page(load_config()))
            return

        if path == "/forward-sources":
            self.send(200, forward_sources_page())
            return

        if path == "/target":
            self.send(200, target_page(load_config()))
            return

        if path == "/nas":
            self.send(200, nas_page(load_config()))
            return

        if path == "/":
            try:
                body = self.dashboard()
            except OSError:
                self.send(200, SHELL.format(
                    title="归档看板",
                    body='<h1>看板尚未生成</h1><p class="sub">'
                         '归档服务下一轮扫描后会自动生成，稍等一分钟刷新即可。</p>'
                         '<p class="foot"><a href="/">刷新</a></p>').encode("utf-8"))
                return
            # 提示只弹一次，取出即清
            clear = self.CLEAR_FLASH if self.cookies().get("ag_flash") else None
            self.send(200, body, cookie=clear)
            return

        self.send(404, b"not found", "text/plain; charset=utf-8")

    do_HEAD = do_GET

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            auth = load_auth()
        except Exception:
            self.send(500, b"auth config missing")
            return

        if not self.same_origin():
            self.send(403, login_page(
                error="来源校验失败，请从 https://ag.625153786.xyz 打开本页后重试"))
            return

        if path == "/login":
            ip = self.client_ip()
            if rate_limited(ip):
                self.send(429, login_page(
                    error="尝试过于频繁，请十分钟后再试"))
                return
            f = self.form()
            if f.get("u") == auth["user"] and check_password(auth, f.get("p", "")):
                _fails.pop(ip, None)
                log.info("登录成功 from %s", ip)
                self.redirect("/", cookie=self.session_cookie(make_token(auth)))
            else:
                note_fail(ip)
                log.warning("登录失败 from %s", ip)
                self.send(401, login_page(error="账号或密码不对"))
            return

        if path == "/logout":
            self.redirect("/", cookie="ag_sess=; Path=/; Max-Age=0; "
                                      "HttpOnly; Secure; SameSite=Strict")
            return

        if path == "/account":
            if not self.logged_in(auth):
                self.redirect("/")
                return
            f = self.form()
            old, new, new2 = f.get("old", ""), f.get("new", ""), f.get("new2", "")
            if not check_password(auth, old):
                self.send(401, account_page(auth["user"], error="当前密码不对"))
            elif len(new) < MIN_PASSWORD:
                self.send(400, account_page(
                    auth["user"], error=f"新密码至少 {MIN_PASSWORD} 位"))
            elif new != new2:
                self.send(400, account_page(auth["user"], error="两次输入不一致"))
            elif new == old:
                self.send(400, account_page(auth["user"], error="新密码不能和旧的一样"))
            else:
                set_password(auth, new)
                # 换掉签名密钥 —— 其它设备上的会话立即失效
                auth["secret"] = secrets.token_bytes(32).hex()
                save_auth(auth)
                log.info("密码已修改 from %s", self.client_ip())
                self.redirect("/", cookie=[self.session_cookie(make_token(auth)),
                                           self.flash_cookie("密码已修改，其它设备的登录已失效")])
            return

        if path == "/settings":
            if not self.logged_in(auth):
                self.redirect("/")
                return
            f = self.form()
            mode = f.get("mode", "")
            when = f.get("start_time", "")
            if mode not in ("realtime", "scheduled"):
                self.send(400, settings_page(load_config(), error="模式不合法"))
                return
            if mode == "scheduled" and not TIME_RE.fullmatch(when):
                self.send(400, settings_page(load_config(),
                                             error="时间格式不对，应为 HH:MM"))
                return
            cfg = load_config()
            cfg["mode"] = mode
            if TIME_RE.fullmatch(when):
                cfg["start_time"] = when
            cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_config(cfg)
            log.info("模式已改为 %s（%s）from %s",
                     mode, cfg["start_time"], self.client_ip())
            tip = ("已切换为实时监控，发现新批次立刻归档"
                   if mode == "realtime"
                   else f"已切换为定时归档，每天 {cfg['start_time']} 开跑")
            # 直接回看板，确认信息用一次性 cookie 带过去
            self.redirect("/", cookie=self.flash_cookie(tip))
            return

        if path == "/sources":
            if not self.logged_in(auth):
                self.redirect("/")
                return
            n = int(self.headers.get("Content-Length") or 0)
            raw = (self.rfile.read(n).decode("utf-8", "replace")
                   if 0 < n <= 256 * 1024 else "")
            form = urllib.parse.parse_qs(raw)
            want = form.get("filter", [""])[0] == "1"
            try:
                who = sorted({int(x) for x in form.get("who", [])})
            except ValueError:
                self.send(400, sources_page(load_config(), error="选择项不合法"))
                return
            if want and not who:
                self.send(400, sources_page(
                    load_config(),
                    error="开启了过滤却一个人都没勾 —— 那样等于什么都不存"))
                return
            cfg = load_config()
            cfg["filter_senders"] = want
            cfg["allowed_senders"] = who
            cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_config(cfg)
            log.info("监控范围已改：过滤=%s 白名单=%d 人 from %s",
                     want, len(who), self.client_ip())
            tip = (f"已开启过滤，只归档勾选的 {len(who)} 人"
                   if want else "已关闭过滤，群里所有人的内容都会归档")
            self.redirect("/", cookie=self.flash_cookie(tip))
            return

        if path == "/forward-sources":
            if not self.logged_in(auth):
                self.redirect("/")
                return
            f = self.form()
            action = f.get("action", "")
            try:
                if action == "add":
                    parser_type = f.get("parser_type", "")
                    if parser_type not in ("file", "folder", "channel"):
                        raise ValueError("消息模式不合法")
                    result = forwarder_request(
                        "source_add",
                        username=f.get("username", ""),
                        parser_type=parser_type,
                    )
                elif action == "remove":
                    result = forwarder_request(
                        "source_remove",
                        username=f.get("username", ""),
                    )
                else:
                    raise ValueError("操作不合法")
                if not result.get("ok"):
                    raise ValueError(str(result.get("error") or "转发来源修改失败"))
            except TimeoutError as exc:
                self.send(503, forward_sources_page(error=str(exc)))
                return
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send(400, forward_sources_page(error=str(exc)))
                return
            log.info("转发来源已修改 action=%s from %s", action, self.client_ip())
            self.send(200, forward_sources_page(
                notice=str(result.get("message") or "转发来源已更新，服务正在重载")))
            return

        if path == "/nas":
            if not self.logged_in(auth):
                self.redirect("/")
                return
            f = self.form()
            candidate = load_config()
            candidate.update({
                "nas_host": f.get("nas_host", ""),
                "nas_port": f.get("nas_port", ""),
                "nas_user": f.get("nas_user", ""),
                "nas_path": f.get("nas_path", ""),
            })
            try:
                settings = validate_nas_settings(candidate)
                fingerprint = test_nas(settings)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                failed = load_config()
                failed.update(candidate)
                self.send(400, nas_page(failed, error=f"连接检测失败：{exc}"))
                return
            cfg = load_config()
            cfg.update(settings)
            cfg["nas_last_test_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            cfg["nas_fingerprint"] = fingerprint
            cfg["updated_at"] = cfg["nas_last_test_at"]
            save_config(cfg)
            log.info("NAS 设置已验证并保存 host=%s port=%s user=%s from %s",
                     settings["nas_host"], settings["nas_port"],
                     settings["nas_user"], self.client_ip())
            self.send(200, nas_page(cfg, notice="rsync 检测通过，群晖设置已保存"))
            return

        self.send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_ip(), fmt % args)
        log.info("%s %s", self.client_ip(), fmt % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    if not AUTH_FILE.exists():
        raise SystemExit(f"缺少 {AUTH_FILE}，请先初始化账号")
    bind = os.environ.get("WEB_BIND", "127.0.0.1")
    log.info("监听 %s:%d（外部须经 nginx）", bind, PORT)
    ThreadingHTTPServer((bind, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
