#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Protected Telegram media watcher with direct 115 uploads.

This process intentionally does not call Telegram Bot API getUpdates. Commands
are observed through the authenticated user session in the official bot's
private dialog, so the encrypted 115bot remains the only update consumer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from telethon import TelegramClient, utils

HERE = Path(__file__).resolve().parent
ARCHIVE_APP_DIR = Path(os.environ.get("ARCHIVE_APP_DIR", "/app/archive"))
if not ARCHIVE_APP_DIR.is_dir():
    ARCHIVE_APP_DIR = HERE.parent / "archive"
sys.path.insert(0, str(ARCHIVE_APP_DIR))
import archiver as archive  # noqa: E402

FORWARDER_APP_DIR = Path(os.environ.get("FORWARDER_APP_DIR", "/app/forwarder"))
if not FORWARDER_APP_DIR.is_dir():
    FORWARDER_APP_DIR = HERE.parent / "forwarder"
sys.path.insert(0, str(FORWARDER_APP_DIR))
from batch_manifest import load_manifests  # noqa: E402


FORWARDER_DIR = Path(
    os.environ.get("FORWARDER_DATA_DIR", "/data/forwarder")
)
BASE = Path(os.environ.get("WATCH115_DATA_DIR", str(FORWARDER_DIR / "watch115")))
CONFIG_FILE = BASE / "config.json"
STATE_FILE = BASE / "state.json"
STAGING = BASE / "staging"
SESSION = str(FORWARDER_DIR / "user")
ENV_FILE = FORWARDER_DIR / ".env"
SUITE_CONFIG_FILE = Path(
    os.environ.get("SUITE_CONFIG_PATH", "/data/archive/config.json")
)
SENDERS_FILE = Path(
    os.environ.get("WATCH115_SENDERS_FILE", "/data/archive/senders.json")
)

BOT_PROPERTIES = Path(
    os.environ.get("WATCH115_BOT_PROPERTIES", "/run/115bot/application.properties")
)
BOT_DB = Path(
    os.environ.get("WATCH115_BOT_DB", "/run/115bot/data/115bot.db")
)

POLL_SECONDS = int(os.environ.get("WATCH115_POLL_SECONDS", "30"))
MENU_POLL_SECONDS = int(os.environ.get("WATCH115_MENU_POLL_SECONDS", "2"))
SETTLE_SECONDS = int(os.environ.get("WATCH115_SETTLE_SECONDS", "45"))
FETCH_LIMIT = int(os.environ.get("WATCH115_FETCH_LIMIT", "500"))
COMMAND_REFRESH_SECONDS = int(
    os.environ.get("WATCH115_COMMAND_REFRESH_SECONDS", "600")
)

COMMANDS = {
    "/watch_menu",
    "/watch_target",
    "/watch_target_set",
    "/watch_users",
    "/watch_user_add",
    "/watch_user_remove",
    "/watch_add",
    "/watch_remove",
    "/watch_list",
    "/watch_dialogs",
    "/watch_state",
    "/watch_pause",
    "/watch_resume",
    "/watch_cid",
    "/watch_retry",
}

MENU_BUTTONS = (
    "来源管理",
    "已监听来源",
    "添加来源",
    "删除来源",
    "目标群",
    "设置目标群",
    "指定用户",
    "添加用户",
    "删除用户",
    "运行状态",
    "暂停监控",
    "继续监控",
    "设置115目录",
    "立即重试",
)

UNIFIED_KEYBOARD_ROWS = (
    ("来源管理", "已监听来源"),
    ("添加来源", "删除来源"),
    ("目标群", "设置目标群"),
    ("指定用户", "添加用户", "删除用户"),
    ("运行状态", "设置115目录"),
    ("暂停监控", "继续监控", "立即重试"),
)

FORWARDER_BUTTONS = {
    "来源管理",
    "已监听来源",
    "添加来源",
    "删除来源",
}
FORWARDER_COMMANDS = {
    "/forwarder_menu",
    "/forwarder_list",
    "/forwarder_target",
    "/forwarder_target_set",
}

BUTTON_COMMANDS = {
    "目标群": "/watch_target",
    "设置目标群": "/watch_target_set",
    "指定用户": "/watch_users",
    "添加用户": "/watch_user_add",
    "删除用户": "/watch_user_remove",
    "运行状态": "/watch_state",
    "暂停监控": "/watch_pause",
    "继续监控": "/watch_resume",
    "设置115目录": "/watch_cid",
    "立即重试": "/watch_retry",
}

WATCH_COMMANDS = [
    {"command": "watch_menu", "description": "打开统一管理菜单"},
    {"command": "forwarder_menu", "description": "管理监控来源"},
    {"command": "forwarder_list", "description": "查看已监听来源"},
    {"command": "watch_target", "description": "查看转发目标群"},
    {"command": "watch_target_set", "description": "设置转发目标群"},
    {"command": "watch_users", "description": "查看目标群指定用户"},
    {"command": "watch_user_add", "description": "添加上传用户"},
    {"command": "watch_user_remove", "description": "删除上传用户"},
    {"command": "watch_state", "description": "查看增强模块状态"},
    {"command": "watch_pause", "description": "暂停频道监控"},
    {"command": "watch_resume", "description": "继续频道监控"},
    {"command": "watch_cid", "description": "设置115保存目录ID"},
    {"command": "watch_retry", "description": "立即重试失败批次"},
]

log = logging.getLogger("watch115")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_env(path: Path = ENV_FILE) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load_json(path: Path, default: dict) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else dict(default)
    except (OSError, json.JSONDecodeError):
        return dict(default)


def load_config(path: Path = CONFIG_FILE) -> dict:
    cfg = load_json(path, {"paused": False, "sources": [], "transfer_cid": ""})
    cfg["paused"] = bool(cfg.get("paused"))
    cfg["transfer_cid"] = str(cfg.get("transfer_cid") or "").strip()
    sources = []
    for row in cfg.get("sources") or []:
        if not isinstance(row, dict):
            continue
        try:
            source_id = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        sources.append(
            {
                "id": source_id,
                "title": str(row.get("title") or source_id).strip(),
                "input": str(row.get("input") or source_id).strip(),
            }
        )
    cfg["sources"] = sources
    return cfg


def save_config(cfg: dict, path: Path = CONFIG_FILE) -> None:
    atomic_json_write(path, cfg)


def load_state(path: Path = STATE_FILE) -> dict:
    state = load_json(
        path,
        {"version": 1, "sources": {}, "dirs": {}, "service": {}},
    )
    state.setdefault("version", 1)
    state.setdefault("sources", {})
    state.setdefault("dirs", {})
    state.setdefault("service", {})
    return state


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    atomic_json_write(path, state)


def parse_properties(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        match = re.match(r"([^:=\s]+)\s*[:=]\s*(.*)$", line)
        if match:
            values[match.group(1).strip()] = match.group(2).strip()
    return values


def read_bot_token(path: Path = BOT_PROPERTIES) -> str:
    values = parse_properties(path.read_text(encoding="utf-8"))
    token = values.get("bot.token", "").strip()
    if not token:
        raise RuntimeError("官方 115bot 未配置 bot.token")
    return token


def read_config_values(path: Path = BOT_DB) -> dict[str, str]:
    if not path.is_file():
        return {}
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=3)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute('SELECT "key", "value" FROM config').fetchall()
        return {
            str(key).strip().lower(): str(value or "").strip()
            for key, value in rows
            if key is not None
        }
    finally:
        connection.close()


def first_value(values: dict[str, str], candidates: Iterable[str]) -> str:
    for key in candidates:
        value = values.get(key.lower(), "").strip()
        if value:
            return value
    return ""


def read_115_settings(
    cfg: Optional[dict] = None,
    db_path: Path = BOT_DB,
) -> dict[str, str]:
    cfg = cfg or load_config()
    values = read_config_values(db_path)
    cookie = first_value(values, ("account.cookie", "cookie"))
    user_agent = first_value(
        values,
        ("account.useragent", "account.ua", "useragent", "ua"),
    )
    official_cid = first_value(
        values,
        ("account.transfercid", "transfercid", "account.cid"),
    )
    return {
        "cookie": cookie,
        "user_agent": user_agent,
        "transfer_cid": str(cfg.get("transfer_cid") or official_cid).strip(),
        "cid_source": "增强模块" if cfg.get("transfer_cid") else "官方115bot",
    }


def normalize_target(value: str) -> str:
    value = value.strip()
    if "t.me/" in value:
        parsed = urllib.parse.urlparse(
            value if "://" in value else f"https://{value}"
        )
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) >= 2 and parts[0] == "c" and parts[1].isdigit():
            return f"-100{parts[1]}"
        if parts and re.fullmatch(r"[A-Za-z0-9_]{5,32}", parts[0]):
            return f"@{parts[0]}"
        raise ValueError("私密邀请链接请先加入频道，再从“可用对话”复制 ID")
    if re.fullmatch(r"-?\d+", value):
        return value
    if re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", value):
        return f"@{value.lstrip('@')}"
    raise ValueError("请输入频道/群组 ID、@用户名或 t.me 链接")


def parse_command(text: str) -> tuple[str, str]:
    text = (text or "").strip()
    if text in BUTTON_COMMANDS:
        return BUTTON_COMMANDS[text], ""
    if not text.startswith("/"):
        return "", ""
    head, _, argument = text.partition(" ")
    command = head.split("@", 1)[0].lower()
    if command not in COMMANDS:
        return "", ""
    return command, argument.strip()


def message_group_key(message, source_id: int) -> str:
    grouped_id = getattr(message, "grouped_id", None)
    suffix = f"g:{grouped_id}" if grouped_id else f"s:{message.id}"
    return f"{source_id}:{suffix}"


def is_visual_media(message) -> bool:
    """Return media types that should be staged and uploaded to 115.

    The historical name is kept for persisted callers and tests; audio is now
    included alongside photos and videos.
    """
    return archive.is_media(message) and archive.media_kind(message) in {
        "photo",
        "video",
        "audio",
    }


def media_size_safe(message) -> int:
    """Use test/dummy sizes when a message lacks Telethon's full media shape."""
    value = getattr(message, "size", None)
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    try:
        return int(archive.media_size(message))
    except (AttributeError, TypeError, ValueError):
        return 0


def build_groups(
    messages: Iterable,
    source_id: int,
    media_predicate: Optional[Callable] = None,
    size_func: Callable = archive.media_size,
) -> list[dict]:
    messages = list(messages)
    media_predicate = media_predicate or is_visual_media
    buckets: dict[str, list] = {}
    for message in messages:
        if not media_predicate(message):
            continue
        key = message_group_key(message, source_id)
        buckets.setdefault(key, []).append(message)

    groups = []
    for key, members in buckets.items():
        members.sort(key=lambda item: item.id)
        caption = next(
            (
                getattr(item, "raw_text", "") or ""
                for item in members
                if (getattr(item, "raw_text", "") or "").strip()
            ),
            "",
        )
        groups.append(
            {
                "key": key,
                "messages": members,
                "caption": caption,
                "first_id": members[0].id,
                "size": sum(size_func(item) for item in members),
                "sender": getattr(members[0], "sender_id", None),
                "senders": {
                    getattr(item, "sender_id", None)
                    for item in members
                    if getattr(item, "sender_id", None) is not None
                },
            }
        )
    groups.sort(key=lambda item: item["first_id"])
    return archive.merge_continuations(groups)


async def load_manifest_groups(client, entity, source_id: int) -> tuple[list[dict], set[int]]:
    """Resolve completed forwarder manifests to exact Telegram messages."""
    groups: list[dict] = []
    consumed: set[int] = set()
    for manifest in load_manifests():
        try:
            if int(manifest.get("target_chat_id")) != int(source_id):
                continue
        except (TypeError, ValueError):
            continue
        # Pending/failed manifests are handled by scan_source as a barrier;
        # they must never be treated as ordinary single-message uploads.
        if str(manifest.get("status") or "complete").lower() != "complete":
            continue
        ids = [int(value) for value in manifest.get("message_ids") or []]
        if not ids:
            continue
        fetched = await client.get_messages(entity, ids=ids)
        if fetched is None:
            fetched = []
        elif not isinstance(fetched, (list, tuple)):
            fetched = [fetched]
        by_id = {int(item.id): item for item in fetched if item is not None}
        members = [by_id[item_id] for item_id in ids if item_id in by_id]
        consumed.update(item.id for item in members)
        dates = [getattr(item, "date", None) for item in members if getattr(item, "date", None)]
        newest = max(dates) if dates else datetime.now(timezone.utc)
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        groups.append(
            {
                "key": f"{source_id}:manifest:{manifest['id']}",
                "messages": members,
                "caption": str(manifest.get("title") or ""),
                "first_id": min(ids),
                "size": sum(media_size_safe(item) for item in members),
                "sender": getattr(members[0], "sender_id", None) if members else None,
                "senders": {
                    getattr(item, "sender_id", None)
                    for item in members
                    if getattr(item, "sender_id", None) is not None
                },
                "expected_count": int(manifest.get("expected_count") or len(ids)),
                "message_ids": ids,
                "batch_manifest": True,
                "manifest_path": manifest.get("_path"),
                "newest_date": newest,
            }
        )
    groups.sort(key=lambda item: item["first_id"])
    return groups, consumed


def source_manifest_barrier(source_id: int) -> tuple[bool, bool]:
    """Return (has_pending_or_failed, has_incomplete_complete_manifest)."""
    pending = False
    incomplete = False
    for manifest in load_manifests():
        try:
            if int(manifest.get("target_chat_id")) != int(source_id):
                continue
        except (TypeError, ValueError):
            continue
        status = str(manifest.get("status") or "complete").lower()
        if status in {"pending", "failed"}:
            pending = True
        elif status == "complete":
            expected = int(manifest.get("expected_count") or 0)
            ids = manifest.get("message_ids") or []
            if expected and len(ids) < expected:
                incomplete = True
    return pending, incomplete


def group_is_settled(group: dict, now: Optional[datetime] = None) -> bool:
    if group.get("expected_count") and len(group["messages"]) < group["expected_count"]:
        return False
    if not group["messages"]:
        return False
    now = now or datetime.now(timezone.utc)
    newest = group.get("newest_date") or max(message.date for message in group["messages"])
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return (now - newest.astimezone(timezone.utc)).total_seconds() >= SETTLE_SECONDS


def safe_error(exc: Exception) -> str:
    text = re.sub(r"\s+", " ", str(exc)).strip()
    return f"{type(exc).__name__}: {text[:240]}" if text else type(exc).__name__


def bot_api(token: str, method: str, **values):
    encoded = {
        key: json.dumps(value, ensure_ascii=False)
        if isinstance(value, (dict, list))
        else str(value)
        for key, value in values.items()
    }
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urllib.parse.urlencode(encoded).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError):
        raise RuntimeError(f"Bot API {method} 请求失败") from None
    if not payload.get("ok"):
        description = str(payload.get("description") or "未知错误")[:160]
        raise RuntimeError(f"Bot API {method} 失败：{description}")
    return payload.get("result")


def menu_keyboard() -> dict:
    return {
        "keyboard": [list(row) for row in UNIFIED_KEYBOARD_ROWS],
        "resize_keyboard": True,
        "is_persistent": True,
    }


async def send_text(
    token: str,
    chat_id: int,
    text: str,
    *,
    keyboard: bool = False,
) -> None:
    chunks = []
    current = ""
    for line in text.splitlines() or [""]:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > 3900 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    chunks.append(current)
    for index, chunk in enumerate(chunks):
        values = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if keyboard and index == 0:
            values["reply_markup"] = menu_keyboard()
        await asyncio.to_thread(bot_api, token, "sendMessage", **values)


async def install_bot_commands(token: str) -> None:
    managed = {row["command"] for row in WATCH_COMMANDS}
    for scope in (
        {"type": "default"},
        {"type": "all_private_chats"},
        {"type": "all_group_chats"},
    ):
        current = await asyncio.to_thread(
            bot_api,
            token,
            "getMyCommands",
            scope=scope,
        )
        existing = [
            row
            for row in (current or [])
            if row.get("command") not in managed
        ]
        await asyncio.to_thread(
            bot_api,
            token,
            "setMyCommands",
            commands=[*WATCH_COMMANDS, *existing],
            scope=scope,
        )


def source_label(source: dict) -> str:
    return f"{source.get('title') or source['id']}（{source['id']}）"


def list_sources_text(cfg: dict, state: dict) -> str:
    if not cfg["sources"]:
        return "当前没有监听频道。使用 /watch_add 添加。"
    rows = ["当前监听频道："]
    for index, source in enumerate(cfg["sources"], 1):
        slot = state["sources"].get(str(source["id"]), {})
        cursor = slot.get("cursor", "未初始化")
        marker = "处理中" if slot.get("inflight") else "就绪"
        rows.append(
            f"{index}. {source_label(source)}\n   游标 {cursor}，{marker}"
        )
    return "\n".join(rows)


def load_pipeline_config(path: Path = SUITE_CONFIG_FILE) -> dict:
    cfg = load_json(path, {})
    cfg["target_chat"] = str(cfg.get("target_chat") or "").strip()
    cfg["target_title"] = str(cfg.get("target_title") or "").strip()
    cfg["filter_senders"] = bool(cfg.get("filter_senders"))
    try:
        cfg["allowed_senders"] = [
            int(value) for value in (cfg.get("allowed_senders") or [])
        ]
    except (TypeError, ValueError):
        cfg["allowed_senders"] = []
    return cfg


def save_pipeline_config(cfg: dict, path: Path = SUITE_CONFIG_FILE) -> None:
    cfg["updated_at"] = now_iso()
    atomic_json_write(path, cfg)


def load_known_senders(path: Path = SENDERS_FILE) -> list[dict]:
    payload = load_json(path, {})
    rows = payload.get("senders") or []
    return [row for row in rows if isinstance(row, dict) and row.get("id") is not None]


def target_text(pipeline: dict) -> str:
    target = pipeline.get("target_chat") or "未设置"
    title = pipeline.get("target_title") or "未识别"
    return f"当前转发目标群：{title}\nID：{target}"


def allowed_users_text(pipeline: dict) -> str:
    known = {int(row["id"]): row for row in load_known_senders()}
    allowed = [int(value) for value in pipeline.get("allowed_senders") or []]
    lines = ["目标群上传 115 的指定发送者："]
    if not pipeline.get("filter_senders") or not allowed:
        lines.append("尚未启用指定用户过滤，115 上传会保持等待。")
    else:
        for sender_id in allowed:
            row = known.get(sender_id, {})
            name = row.get("name") or row.get("username") or sender_id
            lines.append(f"- {name}（{sender_id}）")
    if known:
        lines.append("\n目标群已识别的发送者：")
        for sender_id, row in list(known.items())[:40]:
            marker = " [已选]" if sender_id in allowed else ""
            name = row.get("name") or row.get("username") or sender_id
            tags = []
            if row.get("self"):
                tags.append("你自己")
            if row.get("bot"):
                tags.append("机器人")
            suffix = f"，{'/'.join(tags)}" if tags else ""
            lines.append(f"{sender_id}  {name}{suffix}{marker}")
    lines.append("\n小文件通常由转发机器人发送，大文件可能由你的个人账号发送，建议两者都加入。")
    return "\n".join(lines)


def status_text(cfg: dict, state: dict, settings: dict, pipeline: dict) -> str:
    service = state.get("service", {})
    ready_cookie = "已配置" if settings.get("cookie") else "未配置"
    cid = settings.get("transfer_cid") or "未配置"
    lines = [
        "目标群处理并上传 115 状态",
        f"监控：{'已暂停' if cfg['paused'] else '运行中'}",
        f"目标群：{pipeline.get('target_title') or pipeline.get('target_chat') or '未设置'}",
        f"指定发送者：{len(pipeline.get('allowed_senders') or [])} 个",
        f"115 登录：{ready_cookie}",
        f"保存目录 ID：{cid}",
    ]
    if settings.get("transfer_cid"):
        lines.append(f"目录来源：{settings.get('cid_source')}")
    if service.get("waiting_reason"):
        lines.append(f"等待：{service['waiting_reason']}")
    if service.get("last_scan"):
        lines.append(f"最近扫描：{service['last_scan']}")
    inflight = [
        slot["inflight"]
        for slot in state.get("sources", {}).values()
        if slot.get("inflight")
    ]
    if inflight:
        item = inflight[0]
        lines.append(f"当前批次：{item.get('dir') or item.get('key')}")
        uploaded = len(item.get("uploaded") or [])
        total = len(item.get("files") or [])
        if total:
            lines.append(f"上传进度：{uploaded}/{total}")
    errors = [
        slot.get("last_error")
        for slot in state.get("sources", {}).values()
        if slot.get("last_error")
    ]
    if errors:
        lines.append(f"最近错误：{errors[-1]}")
    if not settings.get("cookie"):
        lines.append("请先在本机器人完成官方 /set115。")
    if not settings.get("transfer_cid"):
        lines.append("然后用 /watch_cid 设置保存目录 ID。")
    if not pipeline.get("target_chat"):
        lines.append("请用 /watch_target_set 设置转发目标群。")
    if not pipeline.get("filter_senders") or not pipeline.get("allowed_senders"):
        lines.append("请用 /watch_user_add 设置目标群内允许上传的发送者。")
    return "\n".join(lines)


def output_name(message, counters: dict[str, int]) -> str:
    kind = archive.media_kind(message)
    if kind == "video":
        counters["video"] += 1
        return f"S01E{counters['video']:02d}{archive.media_ext(message)}"
    if kind == "photo":
        counters["photo"] += 1
        return f"img_{counters['photo']:02d}{archive.media_ext(message)}"
    if kind == "audio":
        counters["audio"] += 1
        return f"audio_{counters['audio']:02d}{archive.media_ext(message)}"
    counters["other"] += 1
    return f"file_{counters['other']:02d}{archive.media_ext(message)}"


def unique_watch_dir_name(
    base_name: str,
    key: str,
    first_id: int,
    source_id: int,
    state: dict,
) -> str:
    candidate = archive.unique_dir_name(base_name, key, first_id, state)
    owner = state["dirs"].get(candidate)
    if owner is None or owner == key:
        return candidate
    suffix = f"-{str(abs(source_id))[-8:]}"
    head = archive.truncate_bytes(
        candidate,
        archive.MAX_NAME_BYTES - len(suffix.encode("utf-8")),
    )
    return head.strip(archive.TRIM_CHARS) + suffix


def upload_file_to_115(
    path: Path,
    *,
    cookie: str,
    user_agent: str,
    transfer_cid: str,
    dirname: str,
) -> None:
    # Imported lazily so config/grouping tests do not require the upload SDK.
    from p115client import P115Client, check_response

    client = P115Client(cookie, console_qrcode=False)
    if user_agent:
        client.headers["User-Agent"] = user_agent
    response = client.upload_file(
        path,
        pid=transfer_cid,
        dirname=dirname,
        partsize=-1,
    )
    check_response(response)


class Watch115Service:
    def __init__(self, client: TelegramClient, token: str, admin_id: int, bot_peer):
        self.client = client
        self.token = token
        self.admin_id = admin_id
        self.bot_peer = bot_peer
        self.config = load_config()
        self.state = load_state()
        self.menu_state = ""
        self.retry_event = asyncio.Event()

    def persist(self) -> None:
        save_state(self.state)

    async def notify(self, text: str) -> None:
        """Notifications must never change upload/cursor success semantics."""
        try:
            await send_text(self.token, self.admin_id, text)
        except Exception:
            log.exception("failed to send watch115 notification")

    async def resolve_source(self, raw_value: str):
        normalized = normalize_target(raw_value)
        peer = int(normalized) if re.fullmatch(r"-?\d+", normalized) else normalized
        entity = await self.client.get_entity(peer)
        if getattr(entity, "bot", False) or not (
            getattr(entity, "broadcast", False)
            or getattr(entity, "megagroup", False)
            or entity.__class__.__name__ == "Chat"
        ):
            raise ValueError("只能监听已加入的频道或群组")
        source_id = int(utils.get_peer_id(entity))
        title = str(
            getattr(entity, "title", None)
            or getattr(entity, "username", None)
            or source_id
        )
        return entity, source_id, title, normalized

    async def add_source(self, raw_value: str) -> str:
        entity, source_id, title, normalized = await self.resolve_source(raw_value)
        self.config = load_config()
        if any(row["id"] == source_id for row in self.config["sources"]):
            raise ValueError(f"{title} 已在监听列表中")
        latest = await self.client.get_messages(entity, limit=1)
        cursor = latest[0].id if latest else 0
        self.config["sources"].append(
            {"id": source_id, "title": title, "input": normalized}
        )
        save_config(self.config)
        self.state["sources"][str(source_id)] = {
            "cursor": cursor,
            "added_at": now_iso(),
        }
        self.persist()
        self.retry_event.set()
        return f"已添加 {title}（{source_id}）。从消息 {cursor} 之后开始监听，不会自动导入旧内容。"

    async def remove_source(self, raw_value: str) -> str:
        self.config = load_config()
        selected = None
        if raw_value.isdigit() and 1 <= int(raw_value) <= len(self.config["sources"]):
            selected = self.config["sources"][int(raw_value) - 1]
        else:
            try:
                normalized = normalize_target(raw_value)
            except ValueError:
                normalized = raw_value.strip()
            for row in self.config["sources"]:
                if normalized in {str(row["id"]), row.get("input", "")}:
                    selected = row
                    break
                if normalized.lstrip("@").lower() == str(
                    row.get("input", "")
                ).lstrip("@").lower():
                    selected = row
                    break
        if not selected:
            raise ValueError("没有找到这个监听频道，可发送序号或频道 ID")
        self.config["sources"] = [
            row for row in self.config["sources"] if row["id"] != selected["id"]
        ]
        save_config(self.config)
        self.retry_event.set()
        return f"已停止监听 {source_label(selected)}。已下载的失败现场会保留，避免误删文件。"

    async def dialogs_text(self) -> str:
        rows = ["当前账号可用的频道和群组（添加时发送 ID）："]
        count = 0
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if getattr(entity, "bot", False):
                continue
            if not (dialog.is_channel or dialog.is_group):
                continue
            peer_id = int(utils.get_peer_id(entity))
            title = re.sub(r"\s+", " ", dialog.name or str(peer_id)).strip()
            rows.append(f"{peer_id}  {title[:80]}")
            count += 1
            if count >= 50:
                rows.append("仅显示前 50 个对话。")
                break
        if count == 0:
            rows.append("没有找到可用频道或群组。")
        return "\n".join(rows)

    async def set_pipeline_target(self, raw_value: str) -> str:
        normalized = normalize_target(raw_value)
        peer = int(normalized) if re.fullmatch(r"-?\d+", normalized) else normalized
        entity = await self.client.get_entity(peer)
        if not (
            getattr(entity, "broadcast", False)
            or getattr(entity, "megagroup", False)
            or entity.__class__.__name__ == "Chat"
        ):
            raise ValueError("目标必须是频道或群组")
        target_id = int(utils.get_peer_id(entity))
        title = str(
            getattr(entity, "title", None)
            or getattr(entity, "username", None)
            or target_id
        )
        await asyncio.to_thread(
            bot_api,
            self.token,
            "sendMessage",
            chat_id=target_id,
            text="TG Media Suite 目标群验证成功",
        )
        pipeline = load_pipeline_config()
        pipeline["target_chat"] = str(target_id)
        pipeline["target_title"] = title
        save_pipeline_config(pipeline)
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "supervisorctl",
                "-c",
                "/etc/supervisor/conf.d/tg-media-suite.conf",
                "restart",
                "forwarder",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError("目标群已保存，但转发器重载失败")
        self.retry_event.set()
        return f"目标群已设置为 {title}（{target_id}）。转发器、群晖归档和 115 上传将共用这个目标。"

    async def resolve_sender_id(self, raw_value: str) -> tuple[int, str]:
        value = raw_value.strip()
        if value.lower() in {"me", "self", "我"}:
            return self.admin_id, "你自己"
        if re.fullmatch(r"-?\d+", value):
            sender_id = int(value)
            known = next(
                (
                    row
                    for row in load_known_senders()
                    if int(row.get("id")) == sender_id
                ),
                {},
            )
            return sender_id, str(
                known.get("name") or known.get("username") or sender_id
            )
        entity = await self.client.get_entity(value)
        sender_id = int(utils.get_peer_id(entity))
        name = str(
            getattr(entity, "title", None)
            or " ".join(
                part
                for part in (
                    getattr(entity, "first_name", ""),
                    getattr(entity, "last_name", ""),
                )
                if part
            ).strip()
            or getattr(entity, "username", None)
            or sender_id
        )
        return sender_id, name

    async def add_allowed_sender(self, raw_value: str) -> str:
        sender_id, name = await self.resolve_sender_id(raw_value)
        pipeline = load_pipeline_config()
        allowed = {int(value) for value in pipeline.get("allowed_senders") or []}
        if sender_id in allowed:
            raise ValueError(f"{name} 已经在指定用户列表中")
        allowed.add(sender_id)
        pipeline["allowed_senders"] = sorted(allowed)
        pipeline["filter_senders"] = True
        save_pipeline_config(pipeline)
        self.retry_event.set()
        return f"已添加指定用户：{name}（{sender_id}）。"

    async def remove_allowed_sender(self, raw_value: str) -> str:
        sender_id, name = await self.resolve_sender_id(raw_value)
        pipeline = load_pipeline_config()
        allowed = {int(value) for value in pipeline.get("allowed_senders") or []}
        if sender_id not in allowed:
            raise ValueError(f"{name} 不在指定用户列表中")
        if len(allowed) <= 1:
            raise ValueError("至少保留一个指定用户，避免误上传群内其他人的内容")
        allowed.remove(sender_id)
        pipeline["allowed_senders"] = sorted(allowed)
        pipeline["filter_senders"] = True
        save_pipeline_config(pipeline)
        self.retry_event.set()
        return f"已删除指定用户：{name}（{sender_id}）。"

    async def handle_command(self, text: str) -> bool:
        stripped = (text or "").strip()
        foreign_command = stripped.partition(" ")[0].split("@", 1)[0].lower()
        if stripped in FORWARDER_BUTTONS or foreign_command in FORWARDER_COMMANDS:
            return False
        command, argument = parse_command(text)
        if not command and self.menu_state:
            command = self.menu_state
            argument = text.strip()
        if not command:
            return False

        command = {
            "/watch_add": "/watch_user_add",
            "/watch_remove": "/watch_user_remove",
            "/watch_list": "/watch_target",
            "/watch_dialogs": "/watch_users",
        }.get(command, command)

        try:
            if command == "/watch_menu":
                self.menu_state = ""
                await send_text(
                    self.token,
                    self.admin_id,
                    "统一管理菜单\n\n可管理来源频道或文件机器人、目标群、指定用户和 115 目录。来源媒体下回复“转发”后，媒体进入目标群并按白名单生成 poster.jpg 后上传 115。",
                    keyboard=True,
                )
            elif command == "/watch_target":
                self.menu_state = ""
                await send_text(
                    self.token,
                    self.admin_id,
                    target_text(load_pipeline_config()),
                    keyboard=True,
                )
            elif command == "/watch_target_set":
                if not argument:
                    self.menu_state = "/watch_target_set"
                    await send_text(
                        self.token,
                        self.admin_id,
                        "请发送目标群的 ID、@用户名或公开 t.me 链接。官方 115bot 必须在群内并有发消息权限。",
                    )
                else:
                    self.menu_state = ""
                    await send_text(
                        self.token,
                        self.admin_id,
                        await self.set_pipeline_target(argument),
                        keyboard=True,
                    )
            elif command == "/watch_users":
                self.menu_state = ""
                await send_text(
                    self.token,
                    self.admin_id,
                    allowed_users_text(load_pipeline_config()),
                    keyboard=True,
                )
            elif command == "/watch_user_add":
                if not argument:
                    self.menu_state = "/watch_user_add"
                    await send_text(
                        self.token,
                        self.admin_id,
                        allowed_users_text(load_pipeline_config())
                        + "\n\n请发送要添加的用户 ID、@用户名，或发送 me 添加你自己。",
                    )
                else:
                    self.menu_state = ""
                    await send_text(
                        self.token,
                        self.admin_id,
                        await self.add_allowed_sender(argument),
                        keyboard=True,
                    )
            elif command == "/watch_user_remove":
                if not argument:
                    self.menu_state = "/watch_user_remove"
                    await send_text(
                        self.token,
                        self.admin_id,
                        allowed_users_text(load_pipeline_config())
                        + "\n\n请发送要删除的用户 ID。",
                    )
                else:
                    self.menu_state = ""
                    await send_text(
                        self.token,
                        self.admin_id,
                        await self.remove_allowed_sender(argument),
                        keyboard=True,
                    )
            elif command == "/watch_state":
                self.menu_state = ""
                cfg = load_config()
                pipeline = load_pipeline_config()
                await send_text(
                    self.token,
                    self.admin_id,
                    status_text(
                        cfg,
                        self.state,
                        read_115_settings(cfg),
                        pipeline,
                    ),
                    keyboard=True,
                )
            elif command in {"/watch_pause", "/watch_resume"}:
                self.menu_state = ""
                cfg = load_config()
                cfg["paused"] = command == "/watch_pause"
                save_config(cfg)
                self.config = cfg
                self.retry_event.set()
                await send_text(
                    self.token,
                    self.admin_id,
                    "监控已暂停，现有进度已保存。"
                    if cfg["paused"]
                    else "监控已继续。",
                    keyboard=True,
                )
            elif command == "/watch_cid":
                if not argument:
                    self.menu_state = "/watch_cid"
                    await send_text(
                        self.token,
                        self.admin_id,
                        "请发送 115 目标文件夹的数字 ID。发送 0 表示根目录；发送 reset 可恢复使用官方 115bot 的转存目录。",
                    )
                else:
                    value = argument.strip().lower()
                    cfg = load_config()
                    if value == "reset":
                        cfg["transfer_cid"] = ""
                        message = "已恢复使用官方 115bot 的转存目录设置。"
                    elif re.fullmatch(r"\d+", value):
                        cfg["transfer_cid"] = value
                        message = f"115 保存目录 ID 已设置为 {value}。"
                    else:
                        raise ValueError("目录 ID 必须是数字，或发送 reset")
                    save_config(cfg)
                    self.config = cfg
                    self.menu_state = ""
                    self.retry_event.set()
                    await send_text(
                        self.token,
                        self.admin_id,
                        message,
                        keyboard=True,
                    )
            elif command == "/watch_retry":
                self.menu_state = ""
                self.retry_event.set()
                await send_text(
                    self.token,
                    self.admin_id,
                    "已安排立即扫描；失败批次会从保存的进度继续。",
                    keyboard=True,
                )
            return True
        except Exception as exc:
            self.menu_state = ""
            log.exception("watch command failed: %s", command)
            await send_text(
                self.token,
                self.admin_id,
                f"操作失败：{safe_error(exc)}",
                keyboard=True,
            )
            return True

    async def menu_loop(self) -> None:
        if "menu_cursor" not in self.state:
            recent = await self.client.get_messages(self.bot_peer, limit=1)
            self.state["menu_cursor"] = recent[0].id if recent else 0
            self.persist()
            await self.notify(
                "目标群处理与 115 上传模块已启动。发送 /watch_menu 管理目标群、指定用户和 115 目录。",
            )
        last_commands = 0.0
        while True:
            try:
                now = time.monotonic()
                if now - last_commands >= COMMAND_REFRESH_SECONDS:
                    last_commands = now
                    try:
                        await install_bot_commands(self.token)
                    except Exception:
                        log.exception("failed to merge watch115 bot commands")
                cursor = int(self.state.get("menu_cursor") or 0)
                messages = await self.client.get_messages(
                    self.bot_peer,
                    min_id=cursor,
                    limit=100,
                    reverse=True,
                )
                for message in messages:
                    self.state["menu_cursor"] = max(
                        int(self.state.get("menu_cursor") or 0), message.id
                    )
                    self.persist()
                    if message.out and message.sender_id == self.admin_id:
                        await self.handle_command(message.raw_text or "")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watch menu polling failed")
            await asyncio.sleep(MENU_POLL_SECONDS)

    async def prepare_batch_files(
        self,
        group: dict,
        local_dir: Path,
        source_slot: dict,
    ) -> list[dict]:
        inflight = source_slot["inflight"]
        manifest = inflight.get("files") or []
        if manifest and all((local_dir / row["name"]).is_file() for row in manifest):
            log.info("reuse staged batch %s (%d files)", group["key"], len(manifest))
            return manifest

        local_dir.mkdir(parents=True, exist_ok=True)
        download_dir = local_dir / ".downloads"
        download_dir.mkdir(exist_ok=True)
        downloaded = {
            str(name): int(size)
            for name, size in (inflight.get("downloaded") or {}).items()
        }
        counters = {"video": 0, "photo": 0, "audio": 0, "other": 0}
        files: list[dict] = []
        for message in group["messages"]:
            name = output_name(message, counters)
            target = local_dir / name
            recorded_size = downloaded.get(name, 0)
            if not (
                target.is_file()
                and recorded_size > 0
                and target.stat().st_size == recorded_size
            ):
                temporary = download_dir / name
                temporary.unlink(missing_ok=True)
                await message.download_media(file=str(temporary))
                if not temporary.is_file() or temporary.stat().st_size <= 0:
                    raise RuntimeError(f"消息 {message.id} 未下载出文件")
                temporary.replace(target)
                downloaded[name] = target.stat().st_size
                inflight["downloaded"] = downloaded
                self.persist()
            if not target.is_file() or target.stat().st_size <= 0:
                raise RuntimeError(f"消息 {message.id} 未下载出文件")
            files.append(
                {
                    "name": name,
                    "size": target.stat().st_size,
                    "kind": archive.media_kind(message),
                    "msg_id": message.id,
                }
            )
            log.info("downloaded %s (%d bytes)", name, target.stat().st_size)

        poster_source = await archive.make_poster(group, local_dir, files)
        poster = local_dir / "poster.jpg"
        if poster.is_file() and poster.stat().st_size > 0:
            files.insert(
                0,
                {
                    "name": "poster.jpg",
                    "size": poster.stat().st_size,
                    "kind": "poster",
                    "msg_id": 0,
                },
            )
            log.info("poster generated via %s", poster_source or "unknown")
        else:
            log.warning("poster generation failed for %s", group["key"])
        inflight["files"] = files
        self.persist()
        return files

    async def process_group(
        self,
        source: dict,
        group: dict,
        settings: dict,
    ) -> bool:
        source_key = str(source["id"])
        slot = self.state["sources"].setdefault(source_key, {})
        inflight = slot.get("inflight")
        if inflight and inflight.get("key") == group["key"]:
            dir_name = inflight["dir"]
        else:
            base_name = archive.batch_title(group)
            dir_name = unique_watch_dir_name(
                base_name,
                group["key"],
                group["first_id"],
                source["id"],
                self.state,
            )
            self.state["dirs"][dir_name] = group["key"]
            inflight = {
                "key": group["key"],
                "dir": dir_name,
                "started_at": now_iso(),
                "uploaded": [],
                "files": [],
            }
            slot["inflight"] = inflight
            slot.pop("last_error", None)
            self.persist()
            await self.notify(
                f"开始处理：{source['title']} / {dir_name}\n媒体 {len(group['messages'])} 个，约 {group['size'] / 1048576:.1f} MB",
            )

        local_dir = STAGING / dir_name
        try:
            files = await self.prepare_batch_files(group, local_dir, slot)
            uploaded = set(inflight.get("uploaded") or [])
            for item in files:
                if item["name"] in uploaded:
                    continue
                await asyncio.to_thread(
                    upload_file_to_115,
                    local_dir / item["name"],
                    cookie=settings["cookie"],
                    user_agent=settings["user_agent"],
                    transfer_cid=settings["transfer_cid"],
                    dirname=dir_name,
                )
                inflight.setdefault("uploaded", []).append(item["name"])
                self.persist()
                log.info("uploaded %s/%s", dir_name, item["name"])
        except Exception as exc:
            error = safe_error(exc)
            previous = slot.get("last_error")
            slot["last_error"] = error
            slot["last_attempt"] = now_iso()
            self.persist()
            log.exception("batch failed: %s", group["key"])
            if previous != error:
                await self.notify(
                    f"上传失败，将自动重试：{source['title']} / {dir_name}\n{error}",
                )
            return False

        slot["last_success"] = {
            "key": group["key"],
            "dir": dir_name,
            "at": now_iso(),
            "files": len(files),
        }
        slot.pop("last_error", None)
        slot.pop("inflight", None)
        self.persist()
        shutil.rmtree(local_dir, ignore_errors=True)
        manifest_path = group.get("manifest_path")
        if manifest_path:
            try:
                Path(manifest_path).unlink(missing_ok=True)
            except OSError:
                log.exception("failed to remove processed batch manifest %s", manifest_path)
        await self.notify(
            f"已上传到 115：{source['title']} / {dir_name}\n共 {len(files)} 个文件（含 poster.jpg 时已计入）。",
        )
        return True

    async def scan_source(
        self,
        source: dict,
        settings: dict,
        allowed_senders: set[int],
    ) -> None:
        source_key = str(source["id"])
        slot = self.state["sources"].setdefault(source_key, {})
        try:
            entity = await self.client.get_entity(source["id"])
            if "cursor" not in slot:
                latest = await self.client.get_messages(entity, limit=1)
                slot["cursor"] = latest[0].id if latest else 0
                slot["added_at"] = now_iso()
                self.persist()
                return
            manifest_groups, manifest_consumed = await load_manifest_groups(
                self.client, entity, source["id"]
            )
            manifest_waiting = False
            for group in manifest_groups:
                if (
                    len(group["messages"]) < group["expected_count"]
                    or not all(is_visual_media(item) for item in group["messages"])
                    or not group_is_settled(group)
                ):
                    manifest_waiting = True
                    continue
                senders = set(group.get("senders") or {group.get("sender")})
                if not (senders & allowed_senders):
                    log.warning("skip manifest batch %s: sender not allowed", group["key"])
                    continue
                if not await self.process_group(source, group, settings):
                    return
                slot["cursor"] = max(
                    int(slot.get("cursor") or 0), max(group["message_ids"])
                )
                self.persist()
            pending, incomplete = source_manifest_barrier(source["id"])
            if pending or incomplete or manifest_waiting:
                log.info(
                    "waiting for forward batch before ordinary scan: source=%s "
                    "pending=%s incomplete=%s messages_waiting=%s",
                    source["id"],
                    pending,
                    incomplete,
                    manifest_waiting,
                )
                return
            cursor = int(slot.get("cursor") or 0)
            messages = await self.client.get_messages(
                entity,
                min_id=cursor,
                limit=FETCH_LIMIT,
                reverse=True,
            )
            if not messages:
                return
            messages = [item for item in messages if item.id not in manifest_consumed]
            if not messages:
                return
            groups = build_groups(messages, source["id"])
            safe_cursor = cursor
            for group in groups:
                before = [
                    m.id
                    for m in messages
                    if safe_cursor < m.id < group["first_id"]
                ]
                if before:
                    safe_cursor = max(before)
                # A full fetch window can end in the middle of a Telegram album.
                # Leave that boundary group for the next cycle after earlier
                # messages have advanced the cursor.
                if (
                    len(messages) >= FETCH_LIMIT
                    and getattr(messages[-1], "grouped_id", None)
                    and messages[-1].id
                    in {member.id for member in group["messages"]}
                ):
                    break
                if not group_is_settled(group):
                    break
                senders = set(group.get("senders") or {group.get("sender")})
                if not (senders & allowed_senders):
                    safe_cursor = max(message.id for message in group["messages"])
                    slot["cursor"] = safe_cursor
                    self.persist()
                    log.info(
                        "skip target batch %s from sender %s",
                        group["key"],
                        group.get("sender"),
                    )
                    continue
                if not await self.process_group(source, group, settings):
                    break
                safe_cursor = max(message.id for message in group["messages"])
                slot["cursor"] = safe_cursor
                self.persist()
            else:
                safe_cursor = max(message.id for message in messages)
            if safe_cursor > cursor:
                slot["cursor"] = safe_cursor
                self.persist()
        except Exception as exc:
            error = safe_error(exc)
            previous = slot.get("last_error")
            slot["last_error"] = error
            slot["last_attempt"] = now_iso()
            self.persist()
            log.exception("target group scan failed: %s", source["id"])
            if previous != error:
                await self.notify(
                    f"目标群扫描失败：{source_label(source)}\n{error}",
                )

    async def worker_cycle(self) -> None:
        self.config = load_config()
        service = self.state["service"]
        service["last_scan"] = now_iso()
        if self.config["paused"]:
            service["waiting_reason"] = "监控已暂停"
            self.persist()
            return
        pipeline = load_pipeline_config()
        if not pipeline["target_chat"]:
            service["waiting_reason"] = "等待设置转发目标群"
            self.persist()
            return
        allowed_senders = {
            int(value) for value in pipeline.get("allowed_senders") or []
        }
        if not pipeline.get("filter_senders") or not allowed_senders:
            service["waiting_reason"] = "等待设置目标群的指定上传用户"
            self.persist()
            return
        try:
            settings = read_115_settings(self.config)
        except Exception as exc:
            service["waiting_reason"] = f"读取官方 115 配置失败：{safe_error(exc)}"
            self.persist()
            return
        if not settings["cookie"]:
            service["waiting_reason"] = "等待在官方机器人完成 /set115"
            self.persist()
            return
        if not re.fullmatch(r"\d+", settings["transfer_cid"]):
            service["waiting_reason"] = "等待设置 115 保存目录 ID"
            self.persist()
            return
        service.pop("waiting_reason", None)
        self.persist()
        normalized = normalize_target(pipeline["target_chat"])
        peer = int(normalized) if re.fullmatch(r"-?\d+", normalized) else normalized
        entity = await self.client.get_entity(peer)
        target_id = int(utils.get_peer_id(entity))
        source = {
            "id": target_id,
            "title": str(
                pipeline.get("target_title")
                or getattr(entity, "title", None)
                or target_id
            ),
        }
        await self.scan_source(source, settings, allowed_senders)

    async def worker_loop(self) -> None:
        while True:
            try:
                await self.worker_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watch worker cycle failed")
            self.retry_event.clear()
            try:
                await asyncio.wait_for(self.retry_event.wait(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        menu_task = asyncio.create_task(self.menu_loop())
        worker_task = asyncio.create_task(self.worker_loop())
        try:
            await self.client.run_until_disconnected()
        finally:
            menu_task.cancel()
            worker_task.cancel()
            await asyncio.gather(menu_task, worker_task, return_exceptions=True)


async def main_async() -> None:
    load_env()
    BASE.mkdir(parents=True, exist_ok=True)
    STAGING.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [watch115] %(message)s",
    )
    token = read_bot_token()
    client = TelegramClient(
        SESSION,
        int(os.environ["TG_API_ID"]),
        os.environ["TG_API_HASH"],
    )
    await client.start()
    me = await client.get_me()
    bot_info = await asyncio.to_thread(bot_api, token, "getMe")
    bot_peer = await client.get_entity(f"@{bot_info['username']}")
    log.info("watch115 ready for bot @%s; admin=%s", bot_info["username"], me.id)
    service = Watch115Service(client, token, int(me.id), bot_peer)
    await service.run()


if __name__ == "__main__":
    asyncio.run(main_async())
