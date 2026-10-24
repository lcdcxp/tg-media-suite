#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把目标对话里的媒体按转发标题分目录归档到群晖 NAS。

工作方式：
  1. 扫描目标对话，按 grouped_id 把消息归拢成「批次」（相册）；
     无 grouped_id 的单条自成一批（大文件走服务端直转，就是这种）
  2. 每批取那条带 caption 的消息文本，清洗成目录名
  3. 下载该批全部媒体到 staging/<目录名>/
  4. rsync 推送到 NAS，成功后删除本地并记录状态
  5. 常驻模式下轮询新消息

刻意逐批串行：瞬时磁盘占用 = 单批体积，不会积压撑爆 VPS。
不触碰 manual_forwarder.py —— 归档挂了不影响转发。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import report
from telethon import TelegramClient, utils
from telethon.tl.types import (
    DocumentAttributeFilename,
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
)

BASE = Path(os.environ.get("ARCHIVE_DATA_DIR", "/opt/tg-archive"))
STAGING = BASE / "staging"
STATE_FILE = BASE / "state.json"
# 运行模式配置。网页写、归档器每轮重读 —— 改完立刻生效，不用重启
CONFIG_FILE = BASE / "config.json"
# 当前正在处理哪一批。归档器写、登录门实时读 —— 看板是 60 秒生成一次的
# 静态文件，光靠它显示不出「正在归档」的进度
PROGRESS_FILE = BASE / "progress.json"
# 群里发过媒体的人。归档器扫描时顺手统计并导出，网页读它来渲染勾选项
# —— 登录门没有 Telegram 会话，自己查不到这些。
SENDERS_FILE = BASE / "senders.json"
SESSION = str(BASE / "archive")
FORWARDER_DIR = Path(
    os.environ.get(
        "FORWARDER_DATA_DIR", "/root/.hermes/automations/tg-media-forwarder"
    )
)
TARGET_STATES = BASE / "states"

NAS_DEST = os.environ.get("NAS_DEST", "nas:/volume1/video/tg/")
NAS_ROOT = os.environ.get("NAS_ROOT", "/volume1/video/tg/")
NAS_KEY_FILE = Path(os.environ.get("NAS_KEY_FILE", "/root/.ssh/id_ed25519_nas"))
NAS_KNOWN_HOSTS = BASE / "nas_known_hosts"
# 看板 HTML 输出。放在 700 目录里、不对外暴露；
# 由 webapp.py（登录门）读取后吐给已认证的访客
WEB_OUT = BASE / "web" / "index.html"
# 打卡系统那份凭证，复用同一个机器人，不新配
TG_NOTIFY_ENV = Path(os.environ.get("TG_NOTIFY_ENV", "/etc/tg-checkin.env"))

# Linux 文件名上限是 255 *字节*，不是字符。中文 3 字节／emoji 4 字节，
# 按字符截断会溢出。留出余量给重名后缀。
MAX_NAME_BYTES = 200
# 相册各条是分开投递的，太早抓会只拿到半批
SETTLE_SECONDS = 45
# Telegram 会把一次转发拆成多个相册，且只有第一个带 caption。
# 拆分点不一定在 10 条上限：实测见过 1+10（带标题的那张单独成组）。
# 所以判据不能是「前一相册满 10 条」，得靠 发送者 + 编号连号 + 时间紧邻。
ALBUM_LIMIT = 10
# 实测：同一次转发的相册间隔约 10 秒；不同批次之间隔了 74 秒。
MERGE_WINDOW_SECONDS = 90
POLL_SECONDS = 60
RSYNC_TIMEOUT = 3600

CST = timezone(timedelta(hours=8))

log = logging.getLogger("tg-archive")

URL_RE = re.compile(r"(?:https?://|(?<![\w.])t\.me/)\S+")
TITLE_PREFIX_RE = re.compile(r"^\s*(?:标题|title)\s*[:：]\s*", re.I)
ILLEGAL_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
# 剥掉 URL 后常留下孤零零的标点尾巴，一并去掉（含全角）
TRIM_CHARS = " 　._-~:：,，.。、;；!！?？"


def truncate_bytes(text: str, limit: int) -> str:
    """按 UTF-8 字节截断，不切碎多字节字符。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


# ---------------------------------------------------------------- 配置


def load_env() -> None:
    """复用转发器的 .env，不重复维护一份凭证。"""
    path = FORWARDER_DIR / ".env"
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


DEFAULT_CONFIG = {"mode": "realtime", "start_time": "02:00",
                  "filter_senders": False, "allowed_senders": [],
                  "target_chat": "", "target_title": "",
                  "nas_host": os.environ.get("NAS_HOST", ""),
                  "nas_port": int(os.environ.get("NAS_PORT", "22")),
                  "nas_user": os.environ.get("NAS_USER", ""),
                  "nas_path": os.environ.get("NAS_PATH", NAS_ROOT)}


def load_config() -> dict:
    """运行模式与过滤规则。每轮循环重读一次，所以网页上一改立刻生效。

    realtime  —— 发现批次就归档（默认，和加这个功能之前的行为一致）
    scheduled —— 每天到点才开跑，跑一次把当天积压的全部处理完

    filter_senders 为 False 时不过滤任何人 —— 默认必须是「全存」，
    否则升级后会静默漏掉内容，比多存些东西糟糕得多。
    """
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    if cfg.get("mode") not in ("realtime", "scheduled"):
        cfg["mode"] = "realtime"
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(cfg.get("start_time", ""))):
        cfg["start_time"] = DEFAULT_CONFIG["start_time"]
    cfg["filter_senders"] = bool(cfg.get("filter_senders"))
    try:
        cfg["allowed_senders"] = [int(x) for x in (cfg.get("allowed_senders") or [])]
    except (TypeError, ValueError):
        cfg["allowed_senders"] = []
    # 开了过滤却没选人 = 什么都不存，几乎肯定是误操作，退回不过滤
    if cfg["filter_senders"] and not cfg["allowed_senders"]:
        log.warning("开启了发送者过滤但白名单为空，本轮按不过滤处理")
        cfg["filter_senders"] = False
    cfg["target_chat"] = str(cfg.get("target_chat") or "").strip()
    cfg["target_title"] = str(cfg.get("target_title") or "").strip()
    cfg["nas_host"] = str(cfg.get("nas_host") or "").strip()
    cfg["nas_user"] = str(cfg.get("nas_user") or "").strip()
    cfg["nas_path"] = str(cfg.get("nas_path") or NAS_ROOT).strip()
    try:
        cfg["nas_port"] = int(cfg.get("nas_port") or 22)
    except (TypeError, ValueError):
        cfg["nas_port"] = 22
    return cfg


def nas_transport(cfg: Optional[dict] = None) -> tuple[str, str]:
    cfg = cfg or load_config()
    host = cfg.get("nas_host")
    user = cfg.get("nas_user")
    path = str(cfg.get("nas_path") or NAS_ROOT).rstrip("/") + "/"
    if not host or not user:
        return os.environ.get("NAS_SSH_COMMAND", "ssh"), NAS_DEST

    known_hosts = (
        NAS_KNOWN_HOSTS if NAS_KNOWN_HOSTS.is_file()
        else Path("/root/.ssh/known_hosts")
    )
    ssh = [
        "ssh", "-p", str(cfg.get("nas_port") or 22),
        "-i", str(NAS_KEY_FILE),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=6",
        "-o", "UpdateHostKeys=no",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
    ]
    return shlex.join(ssh), f"{user}@{host}:{path}"


def configured_target(cfg: Optional[dict] = None) -> str:
    cfg = cfg or load_config()
    return str(
        cfg.get("target_chat")
        or os.environ.get("TG_TARGET")
        or os.environ.get("TG_BOT_CHAT_ID")
        or ""
    ).strip()


def next_run_at(cfg: dict) -> Optional[datetime]:
    """定时模式下，下一次开跑的时刻。"""
    if cfg["mode"] != "scheduled":
        return None
    hh, mm = (int(x) for x in cfg["start_time"].split(":"))
    now = datetime.now(CST)
    today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return today if now < today else today + timedelta(days=1)


def should_transfer(cfg: dict, state: dict) -> tuple[bool, str]:
    """现在能不能传输。返回 (可以吗, 说明)。

    定时模式判定：已过今天的触发时刻，且今天还没跑过。
    一旦开跑就把当天积压的全部处理完 —— 不设结束时刻，
    否则大批次传到一半被掐断，反而更糟。
    """
    if cfg["mode"] == "realtime":
        return True, "实时模式"

    hh, mm = (int(x) for x in cfg["start_time"].split(":"))
    now = datetime.now(CST)
    trigger = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    last = state.get("stats", {}).get("last_scheduled_run")
    today = now.date().isoformat()

    if last == today:
        return False, f"今天已在 {cfg['start_time']} 跑过"
    if now < trigger:
        return False, f"等待 {cfg['start_time']} 开跑"
    return True, f"已过 {cfg['start_time']}，本日开跑"


def load_state(path: Path = STATE_FILE) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state.json 损坏，当作空状态重建")
            data = {}
    else:
        data = {}
    data.setdefault("processed", [])
    data.setdefault("dirs", {})
    data.setdefault("batches", {})
    return data


def save_state_to(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def save_state(state: dict) -> None:
    save_state_to(STATE_FILE, state)


def state_slot(target_id: str) -> Path:
    raw = str(target_id)
    if raw.startswith("-"):
        raw = "neg-" + raw[1:]
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_") or "unknown"
    return TARGET_STATES / f"state-{safe}.json"


def activate_target_state(state: dict, target_id: str) -> dict:
    """每个目标对话独立记进度，切换后不会因消息 ID 重叠而漏归档。"""
    previous = str(state.get("_target_chat") or "").strip()
    if not previous:
        state["_target_chat"] = target_id
        save_state(state)
        return state
    if previous == target_id:
        return state

    save_state_to(state_slot(previous), state)
    slot = state_slot(target_id)
    fresh = load_state(slot) if slot.exists() else {
        "processed": [], "dirs": {}, "batches": {}, "stats": {}
    }
    fresh["_target_chat"] = target_id
    save_state(fresh)
    log.info("目标对话从 %s 切换为 %s，归档状态已分开保存", previous, target_id)
    return fresh


# ---------------------------------------------------------------- 命名


def clean_name(text: str) -> str:
    """把自由文本洗成能当目录名的字符串。洗不出东西就返回空串。"""
    text = (text or "").strip()
    if not text:
        return ""

    # 逐行找第一个剥掉 URL 后仍有实质内容的行
    candidate = ""
    for line in text.splitlines():
        cleaned = TITLE_PREFIX_RE.sub("", URL_RE.sub("", line)).strip()
        if cleaned:
            candidate = cleaned
            break
    if not candidate:
        candidate = URL_RE.sub("", " ".join(text.split())).strip()

    candidate = ILLEGAL_RE.sub("_", candidate)
    candidate = re.sub(r"\s+", " ", candidate)
    candidate = truncate_bytes(candidate.strip(TRIM_CHARS), MAX_NAME_BYTES)
    return candidate.strip(TRIM_CHARS)


# Telegram 客户端自动生成的文件名，没有任何信息量：
# document_2026-08-12_07-25-28.mp4 / IMG_6168.M4V / video_123.mp4
AUTO_FILENAME_RE = re.compile(r"^(document|video|photo|audio|img|vid)[-_ ]?\d", re.I)


def media_filename(message: Message) -> str:
    if isinstance(message.media, MessageMediaPhoto):
        return ""
    for attr in message.media.document.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    return ""


def batch_title(group: dict) -> str:
    """批次标题。优先级：消息文字 > 有意义的原始文件名 > 未命名-id。

    有些内容（尤其手动发进群的大视频）源头就没有任何文字，
    但文件名往往是有信息的 —— 只是要能认出 document_日期 这类
    客户端自动生成的名字，那种和「未命名」一样没用。
    """
    name = clean_name(group.get("caption", ""))
    if name:
        return name

    for message in group["messages"]:
        raw = media_filename(message)
        if not raw:
            continue
        stem = os.path.splitext(raw)[0]
        if AUTO_FILENAME_RE.match(stem):
            continue
        name = clean_name(stem)
        if name:
            log.info("批次 %s 无文字，改用文件名作标题：%s", group["key"], name)
            return name

    return f"未命名-{group['first_id']}"


def sanitize_title(caption: str, fallback_id: int) -> str:
    """兼容旧调用点：洗不出东西时回落到 未命名-id。"""
    return clean_name(caption) or f"未命名-{fallback_id}"


def unique_dir_name(base_name: str, key: str, first_id: int, state: dict) -> str:
    """同名但不同批次时追加消息 id 区分；同一批次复用原名（重跑幂等）。"""
    owner = state["dirs"].get(base_name)
    if owner is None or owner == key:
        return base_name
    suffix = f"-{first_id}"
    head = truncate_bytes(base_name, MAX_NAME_BYTES - len(suffix.encode()))
    return head.strip(TRIM_CHARS) + suffix


def media_ext(message: Message) -> str:
    if isinstance(message.media, MessageMediaPhoto):
        return ".jpg"
    document = message.media.document
    for attr in document.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            ext = os.path.splitext(attr.file_name)[1]
            if ext:
                return ext
    ext = mimetypes.guess_extension(document.mime_type or "") or ""
    return {".jpe": ".jpg"}.get(ext, ext) or ".bin"


def media_kind(message: Message) -> str:
    if isinstance(message.media, MessageMediaPhoto):
        return "photo"
    document = message.media.document
    mime = (document.mime_type or "").lower()
    if mime.startswith("video"):
        return "video"
    if mime.startswith("image"):
        return "photo"
    if mime.startswith("audio"):
        return "audio"
    filename = media_filename(message).lower()
    if Path(filename).suffix in {
        ".aac",
        ".flac",
        ".m4a",
        ".mp3",
        ".oga",
        ".ogg",
        ".opus",
        ".wav",
        ".wma",
    }:
        return "audio"
    return "other"


def media_size(message: Message) -> int:
    if isinstance(message.media, MessageMediaPhoto):
        sizes = [s.size for s in message.media.photo.sizes if hasattr(s, "size")]
        return max(sizes) if sizes else 0
    return message.media.document.size


def is_media(message: Message) -> bool:
    return isinstance(message.media, (MessageMediaPhoto, MessageMediaDocument))


def group_key(message: Message) -> str:
    return f"g:{message.grouped_id}" if message.grouped_id else f"s:{message.id}"


# ---------------------------------------------------------------- 采集


def merge_continuations(groups: list[dict]) -> list[dict]:
    """把被 Telegram 拆开的续相册并回带标题的那一批。

    四条同时满足才合并，缺一不可：
      1. 本组没有 caption
      2. 前一组有 caption（它才可能是正主）
      3. 与前一组来自同一个「投递方」—— 转发器有两个出口：≤50MB 走 Bot API
         （发送者=机器人），>50MB 走 MTProto（发送者=你本人）。同一批次会
         同时用到两个，所以白名单里的账号之间要互认，否则一批的大小文件
         永远合不起来。群友的账号不在白名单里，照样挡得住。
      4. 消息编号严格连号，且时间紧邻（实测同批相册间隔约 10 秒）
    """
    kin = set()
    try:
        cfg = load_config()
        if cfg.get("filter_senders"):
            kin = set(cfg.get("allowed_senders") or [])
    except Exception:
        pass

    merged: list[dict] = []
    for group in groups:
        group["last_id"] = (
            group["messages"][-1].id
            if group["messages"]
            else int(group["first_id"])
        )
        group["last_album_size"] = len(group["messages"])
        group["merged_keys"] = [group["key"]]

        if merged and not group["caption"].strip():
            prev = merged[-1]
            gap = (group["messages"][0].date - prev["messages"][-1].date).total_seconds()
            a, b = group["sender"], prev["sender"]
            same_actor = a is not None and (a == b or (a in kin and b in kin))
            if (prev["caption"].strip()
                    and same_actor
                    and group["first_id"] == prev["last_id"] + 1
                    and 0 <= gap <= MERGE_WINDOW_SECONDS):
                log.info("批次 %s 无标题、同投递方、紧接 %s（间隔 %ds），判定为续集，合并",
                         group["key"], prev["key"], int(gap))
                prev["messages"].extend(group["messages"])
                prev["last_id"] = group["last_id"]
                prev["last_album_size"] = group["last_album_size"]
                prev["size"] += group["size"]
                prev["merged_keys"].extend(group["merged_keys"])
                continue

        merged.append(group)
    return merged


async def collect_groups(client, entity, limit: Optional[int] = None) -> list[dict]:
    """把对话里的媒体消息归拢成批次，按时间从旧到新返回。"""
    buckets: dict[str, list[Message]] = {}
    async for message in client.iter_messages(entity, limit=None):
        if not is_media(message):
            continue
        buckets.setdefault(group_key(message), []).append(message)

    now = datetime.now(timezone.utc)
    groups = []
    for key, messages in buckets.items():
        messages.sort(key=lambda m: m.id)
        newest = max(m.date for m in messages)
        if (now - newest).total_seconds() < SETTLE_SECONDS:
            log.info("批次 %s 刚到达，等下一轮再处理（避免抓到半批）", key)
            continue
        caption = next((m.raw_text for m in messages if (m.raw_text or "").strip()), "")
        groups.append(
            {
                "key": key,
                "messages": messages,
                "caption": caption or "",
                "first_id": messages[0].id,
                "size": sum(media_size(m) for m in messages),
                # 合并续集时用来区分「机器人转发的」和「群友随手发的」
                "sender": getattr(messages[0], "sender_id", None),
            }
        )

    groups.sort(key=lambda g: g["first_id"])
    groups = merge_continuations(groups)
    return groups[:limit] if limit else groups


# ---------------------------------------------------------------- 处理


def write_progress(data: Optional[dict]) -> None:
    """当前进度。None 表示空闲，直接删文件。写失败不能影响归档，整段兜住。"""
    try:
        if data is None:
            PROGRESS_FILE.unlink(missing_ok=True)
            return
        tmp = PROGRESS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(PROGRESS_FILE)
    except Exception:
        log.debug("写进度失败", exc_info=True)


def push_to_nas(local_dir: Path, on_progress=None) -> bool:
    """把一个批次目录推到 NAS。

    参数选择有讲究：
      --partial-dir  中断时残留数据进隐藏子目录，最终文件名只在传完那刻出现。
                     用裸 --partial 的话，NAS 重启会在群晖上留下一个被截断
                     但名字完整的文件 —— 看起来是好的，其实是坏的。
      不用 --whole-file  让重试能真正续传。跨太平洋 1.4 MB/s，一个 268 MB
                     的视频要三分多钟，续传有实际价值。
      --info=progress2  逐字节汇报总进度，供看板显示。
    """
    ssh_command, nas_dest = nas_transport()
    cmd = ["rsync", "-a", "--partial-dir=.rsync-partial", "--info=progress2",
           "-e", ssh_command, str(local_dir), nas_dest]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=0)
    except Exception:
        log.exception("rsync 启动失败")
        return False

    tail, last_ping = b"", 0.0
    deadline = time.time() + RSYNC_TIMEOUT
    try:
        while True:
            if time.time() > deadline:
                proc.kill()
                log.error("rsync 超时（%d 秒）：%s", RSYNC_TIMEOUT, local_dir.name)
                return False
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            tail += chunk
            # progress2 用 \r 原地刷新，所以按 \r 和 \n 一起切
            pieces = re.split(rb"[\r\n]", tail)
            tail = pieces.pop()
            for piece in pieces:
                m = re.search(rb"([\d,]+)\s+(\d+)%", piece)
                if m and on_progress and time.time() - last_ping > 2:
                    last_ping = time.time()
                    on_progress(int(m.group(1).replace(b",", b"")),
                                int(m.group(2)))
        rc = proc.wait(timeout=60)
    except Exception:
        log.exception("rsync 过程异常")
        try:
            proc.kill()
        except Exception:
            pass
        return False

    if rc != 0:
        log.error("rsync 失败 rc=%s：%s", rc, local_dir.name)
        return False
    return True


def run_ffmpeg(args: list[str]) -> bool:
    try:
        result = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                                capture_output=True, timeout=180)
        return result.returncode == 0
    except Exception:
        log.debug("ffmpeg 调用失败", exc_info=True)
        return False


def still_from_image(src: Path, dst: Path) -> bool:
    """把任意图片转成 jpg。Telegram 的图常是 webp，Emby 对 webp 支持不稳。"""
    return (run_ffmpeg(["-i", str(src), "-frames:v", "1", "-q:v", "3", str(dst)])
            and dst.exists() and dst.stat().st_size > 0)


def still_from_video(src: Path, dst: Path) -> bool:
    """从视频截一帧。-ss 放在 -i 前面是关键：直接跳转，不解码整个文件，
    所以 1 GB 的视频也是秒级完成。太短的视频回落到更早的时间点。"""
    for at in ("3", "1", "0"):
        if (run_ffmpeg(["-ss", at, "-i", str(src), "-frames:v", "1",
                        "-q:v", "3", str(dst)])
                and dst.exists() and dst.stat().st_size > 0):
            return True
    return False


async def make_poster(group: dict, local_dir: Path, files: list[dict]) -> str:
    """给文件夹放一张 poster.jpg —— Emby 不会为剧集自动生成封面，
    没有这个文件就是一片空白。三级回落，保证取得到。

    返回用了哪一级，供日志。
    """
    poster = local_dir / "poster.jpg"

    # ① 批次自带的图片，成本为零（本来就下载了）
    for item in files:
        if item["kind"] == "photo":
            if still_from_image(local_dir / item["name"], poster):
                return "批次图片"
            break

    # ② 视频自带的 Telegram 缩略图，几十 KB
    for message in group["messages"]:
        if media_kind(message) != "video":
            continue
        tmp = local_dir / "_thumb_src"
        try:
            got = await message.download_media(file=str(tmp), thumb=-1)
            if got:
                ok = still_from_image(Path(got), poster)
                Path(got).unlink(missing_ok=True)
                if ok:
                    return "TG 缩略图"
        except Exception:
            log.debug("取 TG 缩略图失败", exc_info=True)
        break

    # ③ ffmpeg 截帧，本地操作
    for item in files:
        if item["kind"] == "video":
            if still_from_video(local_dir / item["name"], poster):
                return "ffmpeg 截帧"
            break

    return ""


async def process_group(client, group: dict, state: dict) -> bool:
    key = group["key"]
    base_name = batch_title(group)
    dir_name = unique_dir_name(base_name, key, group["first_id"], state)
    local_dir = STAGING / dir_name

    if local_dir.exists():
        shutil.rmtree(local_dir)
    local_dir.mkdir(parents=True)

    log.info("批次 %s → 「%s」（%d 个文件，%.1f MB）",
             key, dir_name, len(group["messages"]), group["size"] / 1048576)

    total = group["size"] or 1
    base = {"dir": dir_name, "title": group["caption"].strip() or dir_name,
            "files": len(group["messages"]), "bytes": group["size"],
            "started_at": datetime.now(CST).isoformat()}
    write_progress({**base, "stage": "download", "done": 0, "pct": 0})

    files: list[dict] = []
    try:
        carried = 0
        episode = image_no = other_no = 0
        for index, message in enumerate(group["messages"], start=1):
            # 视频按 Emby 的剧集命名，它才能解析出集数；图片另起一套名字，
            # 否则会被当成集。每集的预览图由 Emby 在群晖上自己截，不用我们管。
            kind = media_kind(message)
            if kind == "video":
                episode += 1
                fname = f"S01E{episode:02d}{media_ext(message)}"
            elif kind == "photo":
                image_no += 1
                fname = f"img_{image_no:02d}{media_ext(message)}"
            else:
                other_no += 1
                fname = f"file_{other_no:02d}{media_ext(message)}"
            target = local_dir / fname
            last = [0.0]

            def tick(got, _tot, _c=carried, _l=last):
                if time.time() - _l[0] < 2:
                    return
                _l[0] = time.time()
                done = _c + got
                write_progress({**base, "stage": "download", "done": done,
                                "pct": min(99, int(done * 100 / total))})

            await message.download_media(file=str(target), progress_callback=tick)
            if not target.exists():
                raise RuntimeError(f"消息 {message.id} 未产生文件")
            size = target.stat().st_size
            carried += size
            files.append({"name": target.name, "size": size,
                          "kind": media_kind(message), "msg_id": message.id})
            log.info("  已下载 %s（%.1f MB）", target.name, size / 1048576)
    except Exception:
        log.exception("下载失败，保留现场等下轮重试：%s", key)
        shutil.rmtree(local_dir, ignore_errors=True)
        write_progress(None)
        return False

    write_progress({**base, "stage": "upload", "done": 0, "pct": 0})

    source = await make_poster(group, local_dir, files)
    if source:
        log.info("  封面已生成（来源：%s）", source)
    else:
        log.warning("  没能生成封面：%s", dir_name)

    def up(done, pct):
        write_progress({**base, "stage": "upload", "done": done, "pct": pct})

    if not push_to_nas(local_dir, on_progress=up):
        state.setdefault("stats", {})["last_nas_failure"] = (
            datetime.now(CST).isoformat()
        )
        save_state(state)
        shutil.rmtree(local_dir, ignore_errors=True)
        write_progress(None)
        return False

    # 推送成功才落状态，失败下轮自然重试
    state["dirs"][dir_name] = key
    for member in group["merged_keys"]:
        if member not in state["processed"]:
            state["processed"].append(member)
    state["batches"][key] = {
        "dir": dir_name,
        "title_full": group["caption"].strip() or dir_name,
        "files": files,
        "bytes": sum(f["size"] for f in files),
        "photos": sum(1 for f in files if f["kind"] == "photo"),
        "videos": sum(1 for f in files if f["kind"] == "video"),
        "forwarded_at": group["messages"][0].date.astimezone(CST).isoformat(),
        "archived_at": datetime.now(CST).isoformat(),
        "msg_range": [group["messages"][0].id, group["messages"][-1].id],
        "merged_keys": group["merged_keys"],
        "sender": group.get("sender"),
    }
    state.setdefault("stats", {})["last_nas_success"] = datetime.now(CST).isoformat()
    save_state(state)

    shutil.rmtree(local_dir, ignore_errors=True)
    write_progress(None)
    log.info("  ✓ 已归档到 NAS 并清理本地：%s", dir_name)
    return True


# ---------------------------------------------------------------- 看板与告警


def disk_free() -> str:
    try:
        st = os.statvfs("/")
        return f"{st.f_bavail * st.f_frsize / (1 << 30):.0f} G"
    except OSError:
        return "—"


def tx_bytes_this_month() -> int:
    """本月出站字节数。vnstat --oneline 的第 9 个字段（0 起）是当月 tx。"""
    try:
        out = subprocess.run(["vnstat", "-m", "--oneline"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
        parts = out.split(";")
        if len(parts) > 9:
            num, unit = parts[9].split()
            mult = {"KiB": 1 << 10, "MiB": 1 << 20,
                    "GiB": 1 << 30, "TiB": 1 << 40}
            return int(float(num) * mult.get(unit, 1))
    except Exception:
        log.debug("读取 vnstat 失败", exc_info=True)
    return 0


def service_active() -> bool:
    if os.environ.get("RUNNING_IN_CONTAINER") == "1":
        return True
    try:
        r = subprocess.run(["systemctl", "is-active", "tg-archive"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def notify(text: str) -> None:
    """出问题时推一条 TG。复用打卡系统那份凭证，不新建机器人。"""
    try:
        cfg = {}
        for raw in TG_NOTIFY_ENV.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
        token, chat = cfg.get("TELEGRAM_BOT_TOKEN"), cfg.get("NOTIFY_CHAT_ID")
        if not token or not chat:
            log.warning("告警凭证缺失，跳过推送")
            return
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, timeout=15)
    except Exception:
        log.warning("告警推送失败", exc_info=True)


def write_report(state: dict, runtime: dict) -> None:
    """看板生成失败绝不能拖累归档，所以整段兜住。"""
    try:
        WEB_OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = WEB_OUT.with_suffix(".tmp")
        tmp.write_text(report.render(state, runtime), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(WEB_OUT)
    except Exception:
        log.warning("看板生成失败", exc_info=True)


async def publish_senders(client, entity, groups: list[dict]) -> None:
    """把「群里谁发过媒体、各发了多少」导出给网页。

    名字得单独解析：扫描时只能拿到 sender_id。一次性批量取，
    取不到就退回显示 id —— 拿不到名字不该阻塞导出。
    """
    stat: dict[int, dict] = {}
    for g in groups:
        sid = g.get("sender")
        if sid is None:
            continue
        rec = stat.setdefault(sid, {"id": sid, "batches": 0, "files": 0, "bytes": 0})
        rec["batches"] += 1
        rec["files"] += len(g["messages"])
        rec["bytes"] += g["size"]

    # 登录账号必须永远出现在列表里，哪怕它一条媒体都没发过。
    # 原因：转发器对 >50MB 的文件走 MTProto 直转，那种是以「你本人」
    # 的身份发进群的。若列表里没有你，白名单就勾不上，大文件会被静默跳过。
    try:
        me = await client.get_me()
        stat.setdefault(int(me.id),
                        {"id": int(me.id), "batches": 0, "files": 0, "bytes": 0})
        stat[int(me.id)]["self"] = True
    except Exception:
        log.debug("取登录账号失败", exc_info=True)

    for sid, rec in stat.items():
        rec["name"] = str(sid)
        rec["bot"] = False

    # 解析名字要走网络，别每 60 秒都做一遍：只在出现新面孔
    # 或上次导出超过 10 分钟时才刷新
    ids = sorted(stat)
    try:
        old = json.loads(SENDERS_FILE.read_text(encoding="utf-8"))
        old_ids = sorted(int(s["id"]) for s in old.get("senders", []))
        age = (datetime.now(CST)
               - datetime.fromisoformat(old["updated_at"])).total_seconds()
        if old_ids == ids and age < 600:
            return
    except Exception:
        pass

    for sid, rec in stat.items():
        try:
            ent = await client.get_entity(sid)
            name = (getattr(ent, "title", None)
                    or " ".join(x for x in (getattr(ent, "first_name", ""),
                                            getattr(ent, "last_name", "")) if x).strip()
                    or getattr(ent, "username", None) or str(sid))
            rec["name"] = name
            rec["bot"] = bool(getattr(ent, "bot", False))
            if getattr(ent, "username", None):
                rec["username"] = "@" + ent.username
        except Exception:
            log.debug("解析发送者 %s 失败", sid, exc_info=True)

    data = {
        "updated_at": datetime.now(CST).isoformat(),
        "chat_title": getattr(entity, "title", "") or str(entity.id),
        "chat_id": configured_target(),
        "senders": sorted(stat.values(),
                          key=lambda r: (not r.get("self"), -r["files"])),
    }
    try:
        tmp = SENDERS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(SENDERS_FILE)
    except Exception:
        log.debug("导出发送者列表失败", exc_info=True)


def snapshot(state: dict, total_groups: int, pending: int, failures: int) -> dict:
    last = state.get("stats", {}).get("last_scan")
    age = 0.0
    if last:
        try:
            age = (datetime.now(CST) - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            age = 0.0
    cfg = load_config()
    nxt = next_run_at(cfg)
    try:
        prog = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        prog = None
    return {
        "total_in_chat": total_groups,
        "pending": pending,
        "progress": prog,
        "failures": failures,
        "last_scan": last,
        "scan_age_sec": age,
        "disk_free": disk_free(),
        "tx_bytes": tx_bytes_this_month(),
        "service_active": service_active(),
        "mode": cfg["mode"],
        "start_time": cfg["start_time"],
        "filter_senders": cfg["filter_senders"],
        "allowed_count": len(cfg["allowed_senders"]),
        "next_run": nxt.isoformat() if nxt else None,
        "last_scheduled_run": state.get("stats", {}).get("last_scheduled_run"),
    }


async def run_once(client, entity, state: dict, limit: Optional[int],
                   dry_run: bool, transfer: bool = True,
                   hold_reason: str = "") -> int:
    groups = await collect_groups(client, entity, limit=None)

    cfg = load_config()
    # 先导出「全部」发送者（含被排除的），否则网页上看不到还能勾谁
    await publish_senders(client, entity, groups)
    if cfg["filter_senders"]:
        allow = set(cfg["allowed_senders"])
        kept = [g for g in groups if g.get("sender") in allow]
        if len(kept) != len(groups):
            log.info("发送者白名单：%d 批中保留 %d 批", len(groups), len(kept))
        groups = kept

    done = set(state["processed"])
    # 用 merged_keys 判定：若续集在父批次归档后才到达，整批会重跑一次
    # （父批次文件名不变、原样覆盖，续集接着编号，结果仍然正确）
    pending = [g for g in groups if not set(g["merged_keys"]).issubset(done)]
    if limit:
        pending = pending[:limit]

    if not dry_run:
        stats = state.setdefault("stats", {})
        stats["last_scan"] = datetime.now(CST).isoformat()
        # 登录门要实时显示「待归档 N 批」，但它不扫 Telegram，
        # 只能读归档器留在 state 里的这个快照
        stats["pending_snapshot"] = len(pending)

    if not pending:
        log.info("没有待处理批次")
        if not dry_run:
            save_state(state)
            write_report(state, snapshot(state, len(groups), 0, 0))
        return 0

    total = sum(g["size"] for g in pending)
    log.info("待处理 %d 批，共 %.2f GB", len(pending), total / 1073741824)

    # 定时模式还没到点：检测照做（看板要显示待办数），但不下载不传输
    if not dry_run and not transfer:
        log.info("暂不传输 —— %s", hold_reason)
        save_state(state)
        write_report(state, snapshot(state, len(groups), len(pending), 0))
        return 0

    if dry_run:
        # 用一份影子 state 走真实命名逻辑，保证预览与实际落地完全一致
        preview_state = {"processed": [], "dirs": dict(state["dirs"])}
        print()
        print("=" * 78)
        print("空跑预览 —— 以下目录会建在 NAS 的 /volume1/video/tg/ 下")
        print("=" * 78)
        for group in pending:
            base_name = batch_title(group)
            name = unique_dir_name(base_name, group["key"], group["first_id"],
                                   preview_state)
            preview_state["dirs"][name] = group["key"]
            print()
            print("📁 %s" % name)
            print("   %d 个文件，%.1f MB，目录名 %d 字节" % (
                len(group["messages"]), group["size"] / 1048576,
                len(name.encode("utf-8"))))
            for index, message in enumerate(group["messages"], start=1):
                print("      %02d_%s%s   %.1f MB" % (
                    index, message.id, media_ext(message),
                    media_size(message) / 1048576))
        print()
        print("=" * 78)
        print("合计 %d 个目录，%d 个文件，%.2f GB" % (
            len(pending), sum(len(g["messages"]) for g in pending),
            total / 1073741824))
        print("（空跑，未下载任何东西）")
        return 0

    ok = 0
    for group in pending:
        if await process_group(client, group, state):
            ok += 1
    failed = len(pending) - ok
    log.info("本轮完成 %d/%d 批", ok, len(pending))

    state.setdefault("stats", {})["failures"] = failed
    save_state(state)
    write_report(state, snapshot(state, len(groups), 0, failed))

    if failed:
        notify(f"⚠️ TG 归档告警\n本轮 {len(pending)} 批中有 {failed} 批失败，"
               f"现场已保留，下轮自动重试。\nhttps://ag.625153786.xyz/")
    return ok


# ---------------------------------------------------------------- 入口


async def reindex(client, entity, state: dict) -> int:
    """给已归档但缺元数据的批次补录信息。纯读 Telegram，不下载、不传输。

    这些批次是在看板功能之前归档的，state 里只有「处理过」一条记录。
    标题、文件名、大小、转发时间都能从 Telegram 重新读出来；
    文件名按与 process_group 完全相同的规则推导，所以和 NAS 上的实际一致。
    唯独准确的归档时刻已无从考证 —— 留空，不编造。
    """
    groups = await collect_groups(client, entity, limit=None)
    done = set(state["processed"])
    key_to_dir = {v: k for k, v in state["dirs"].items()}
    n = 0
    for group in groups:
        key = group["key"]
        if not set(group["merged_keys"]).issubset(done):
            continue                      # 还没归档，交给正常流程
        if key in state["batches"]:
            continue                      # 已有元数据
        dir_name = key_to_dir.get(key) or sanitize_title(
            group["caption"], group["first_id"])
        files = [
            {"name": f"{i:02d}_{m.id}{media_ext(m)}", "size": media_size(m),
             "kind": media_kind(m), "msg_id": m.id}
            for i, m in enumerate(group["messages"], start=1)
        ]
        state["batches"][key] = {
            "dir": dir_name,
            "title_full": group["caption"].strip() or dir_name,
            "files": files,
            "bytes": sum(f["size"] for f in files),
            "photos": sum(1 for f in files if f["kind"] == "photo"),
            "videos": sum(1 for f in files if f["kind"] == "video"),
            "forwarded_at": group["messages"][0].date.astimezone(CST).isoformat(),
            "archived_at": None,          # 历史批次的准确时刻已丢失，不伪造
            "msg_range": [group["messages"][0].id, group["messages"][-1].id],
            "merged_keys": group["merged_keys"],
            "backfilled": True,
        }
        state["dirs"].setdefault(dir_name, key)
        n += 1
        log.info("补录 %s → 「%s」（%d 个文件，%.1f MB）",
                 key, dir_name, len(files),
                 state["batches"][key]["bytes"] / 1048576)

    state.setdefault("stats", {})["last_scan"] = datetime.now(CST).isoformat()
    save_state(state)
    write_report(state, snapshot(state, len(groups), 0, 0))
    log.info("补录完成 %d 批，看板已生成", n)
    return n


async def main_async(args) -> None:
    load_env()
    STAGING.mkdir(parents=True, exist_ok=True)
    TARGET_STATES.mkdir(parents=True, exist_ok=True)
    state = load_state()

    client = TelegramClient(SESSION, int(os.environ["TG_API_ID"]),
                            os.environ["TG_API_HASH"])
    await client.start()

    async def get_target(current_id: Optional[str] = None):
        target_value = configured_target()
        if not target_value:
            raise RuntimeError("未配置目标频道/群组")
        peer = int(target_value) if re.fullmatch(r"-?\d+", target_value) else target_value
        selected = await client.get_entity(peer)
        target_id = str(utils.get_peer_id(selected))
        if current_id != target_id:
            log.info(
                "已连接目标对话：%s（%s）",
                getattr(selected, "title", None) or target_value,
                target_id,
            )
        return selected, target_id

    entity, active_target_id = await get_target()
    state = activate_target_state(state, active_target_id)

    if args.reindex:
        await reindex(client, entity, state)
    elif args.once or args.dry_run or args.backfill:
        await run_once(client, entity, state, args.limit, args.dry_run)
    else:
        log.info("常驻模式，每 %d 秒扫描一次", POLL_SECONDS)
        last_mode = None
        while True:
            try:
                cfg = load_config()                    # 每轮重读，网页改完立刻生效
                entity, selected_target_id = await get_target(active_target_id)
                if selected_target_id != active_target_id:
                    state = activate_target_state(state, selected_target_id)
                    active_target_id = selected_target_id
                if cfg["mode"] != last_mode:
                    log.info("当前模式：%s%s", cfg["mode"],
                             f"（每天 {cfg['start_time']} 开跑）"
                             if cfg["mode"] == "scheduled" else "")
                    last_mode = cfg["mode"]

                allowed, reason = should_transfer(cfg, state)
                await run_once(client, entity, state, None, False,
                               transfer=allowed, hold_reason=reason)

                # 定时模式跑完一轮就记账，当天不再重复触发
                if allowed and cfg["mode"] == "scheduled":
                    today = datetime.now(CST).date().isoformat()
                    stats = state.setdefault("stats", {})
                    if stats.get("last_scheduled_run") != today:
                        stats["last_scheduled_run"] = today
                        save_state(state)
                        log.info("定时归档本日完成，下次 %s", cfg["start_time"])
            except Exception:
                log.exception("本轮异常，%d 秒后重试", POLL_SECONDS)
            await asyncio.sleep(POLL_SECONDS)

    await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="归档 Telegram 转发媒体到群晖")
    parser.add_argument("--dry-run", action="store_true", help="只预览目录名，不下载")
    parser.add_argument("--once", action="store_true", help="处理完待办即退出")
    parser.add_argument("--backfill", action="store_true", help="同 --once，语义化别名")
    parser.add_argument("--limit", type=int, help="本次最多处理几批")
    parser.add_argument("--reindex", action="store_true",
                        help="给已归档批次补录元数据并生成看板，不下载任何文件")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
