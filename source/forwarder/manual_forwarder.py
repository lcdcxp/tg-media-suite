#!/usr/bin/env python3
"""Manually forward a complete file-bot batch after a user's reply.

Workflow:
  1. The logged-in Telegram user starts a supported file-bot request.
  2. The user replies ``转发`` to any media returned by that bot.
  3. This process resolves the surrounding request, collects every media item
     in it, and sends eligible items through the official 115bot identity to
     the configured target chat.

The old source-channel cursor is deliberately not used here. A batch is
identified by its /start message and the next /start boundary in the bot chat,
which also works when the returned media is split into multiple groups.

The ePan folder bot delivers folder contents asynchronously, so a folder card
and the media it describes are NOT adjacent in the message id space: the user
can open several cards before any of their contents arrive. Folder batches are
therefore anchored on the contiguous media run around the replied message, and
the card is only used to supply a caption and to corroborate the run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from telethon import TelegramClient, events, utils
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    DocumentAttributeFilename,
    MessageMediaWebPage,
)

from batch_manifest import load_manifests, write_manifest
from bot_sender import send_media_batches, send_text


BASE = Path(
    os.environ.get("FORWARDER_DATA_DIR", Path(__file__).resolve().parent)
)
ENV_PATH = BASE / ".env"
PROCESSED_PATH = BASE / "manual_forwarded.json"
SOURCES_PATH = BASE / "manual_sources.json"
CONTROL_REQUESTS = BASE / "control" / "requests"
CONTROL_RESPONSES = BASE / "control" / "responses"
SUITE_CONFIG_PATH = Path(
    os.environ.get("SUITE_CONFIG_PATH", "/data/archive/config.json")
)
BOT_PROPERTIES = Path(
    os.environ.get(
        "FORWARDER_BOT_PROPERTIES",
        "/run/115bot/application.properties",
    )
)
def _bot_usernames() -> tuple[str, ...]:
    """File bots the user account talks to, from FORWARDER_BOT_USERNAMES."""
    raw = os.environ.get("FORWARDER_BOT_USERNAMES", "").strip()
    names = []
    for item in raw.split(","):
        name = item.strip().lstrip("@").lower()
        if name:
            names.append(f"@{name}")
    return tuple(dict.fromkeys(names))


BOT_USERNAMES: tuple[str, ...] = _bot_usernames()
FOLDER_BOT_USERNAME = (
    os.environ.get("FORWARDER_FOLDER_BOT", "").strip().lstrip("@").lower()
)
SOURCE_TYPES: dict[str, str] = (
    {FOLDER_BOT_USERNAME: "folder"} if FOLDER_BOT_USERNAME else {}
)
TRIGGER_TEXT = "转发"
MENU_COMMAND = "/forwarder_menu"
MENU_BUTTONS = (
    "来源管理",
    "添加来源",
    "已监听来源",
    "删除来源",
)
UNIFIED_KEYBOARD_ROWS = (
    ("来源管理", "已监听来源"),
    ("添加来源", "删除来源"),
    ("目标群", "设置目标群"),
    ("指定用户", "添加用户", "删除用户"),
    ("运行状态", "设置115目录"),
    ("暂停监控", "继续监控", "立即重试"),
)
MODE_BUTTONS = ("普通文件模式", "文件夹模式", "取消")
START_RE = re.compile(r"^/start(?:\s+getFile_([A-Za-z0-9_-]+))?\s*$")
DESCRIPTION_RE = re.compile(r"文件描述\s*[：:]\s*(.*)")
FOLDER_COUNT_RE = re.compile(r"消息数\s*[：:]\s*(\d+)\s*条")
FOLDER_PHOTOS_RE = re.compile(r"图片数\s*[：:]\s*(\d+)\s*张")
FOLDER_VIDEOS_RE = re.compile(r"视频数\s*[：:]\s*(\d+)\s*个")
NEXT_GROUP_TEXT = "下一组"
GROUP_DONE_TEXT = "全部获取成功"
GROUP_PROGRESS_TEXT = "已获取第"
FOLDER_DONE_TEXT = "文件夹内容发送完成"
HISTORY_LIMIT = 300
POLL_INTERVAL = 20.0
WAIT_FOR_COMPLETE = 120.0
# A single large upload can sit minutes behind its neighbours, so only a gap
# well past that splits one folder's media into two runs.
MEDIA_RUN_GAP_SECONDS = 300.0
# Fallback completion signal when neither a closing boundary nor a matching
# card is available: the run has simply stopped growing.
FOLDER_QUIESCENCE_SECONDS = 45.0
# Keep a margin below the cloud Bot API's 50 MB upload limit.
MAX_BOT_UPLOAD_BYTES = 49_000_000
DOWNLOAD_CONCURRENCY = max(
    1, int(os.environ.get("FORWARDER_DOWNLOAD_CONCURRENCY", "3"))
)
DOWNLOAD_RETRIES = max(
    1, int(os.environ.get("FORWARDER_DOWNLOAD_RETRIES", "3"))
)
BATCH_STAGING_DIR = Path(
    os.environ.get("FORWARDER_BATCH_STAGING", str(BASE / "batch-staging"))
)

log = logging.getLogger("manual-forwarder")


@dataclass
class ResolvedBatch:
    start_id: int
    token: str
    media: list[Message] = field(default_factory=list)
    caption: str = ""
    complete: bool = False
    progress_messages: list[Message] = field(default_factory=list)
    card_id: Optional[int] = None
    complete_reason: str = ""


@dataclass
class FolderCard:
    id: int
    count: int
    title: str
    photos: Optional[int] = None
    videos: Optional[int] = None


@dataclass(frozen=True)
class SourceConfig:
    username: str
    parser_type: str = "file"

    @property
    def key(self) -> str:
        return self.username.lstrip("@").lower()


def load_env() -> None:
    for raw_line in ENV_PATH.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


def parse_properties(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        match = re.match(r"^([^:=\s][^:=]*?)\s*[:=]\s*(.*)$", line)
        if match:
            values[match.group(1).strip()] = match.group(2).strip()
    return values


def read_management_bot_token(path: Path = BOT_PROPERTIES) -> str:
    try:
        values = parse_properties(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"无法读取官方 115bot 配置：{path}") from exc
    token = values.get("bot.token", "").strip()
    if not token:
        raise RuntimeError("官方 115bot 未配置 bot.token")
    return token


def normalize_username(value: str) -> str:
    """Normalize a forwarding source: bot, channel, or group."""
    return normalize_target(value)


def source_peer(value: str):
    return int(value) if re.fullmatch(r"-?\d+", value) else value


def is_channel_or_group(entity) -> bool:
    return bool(
        getattr(entity, "broadcast", False)
        or getattr(entity, "megagroup", False)
        or entity.__class__.__name__ == "Chat"
    )


def normalize_target(value: str) -> str:
    """接受频道/群组 ID、@用户名或公开 t.me 链接。"""
    value = value.strip()
    if "t.me/" in value:
        path = urllib.parse.urlparse(
            value if "://" in value else f"https://{value}"
        ).path.strip("/")
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "c" and parts[1].isdigit():
            return f"-100{parts[1]}"
        if parts and re.fullmatch(r"[A-Za-z0-9_]{5,32}", parts[0]):
            return f"@{parts[0]}"
        raise ValueError("只支持频道/群组 ID、@用户名或公开 t.me 链接")
    if re.fullmatch(r"-?\d+", value):
        return value
    if re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", value):
        return f"@{value.lstrip('@')}"
    raise ValueError("请输入频道/群组 ID、@用户名或公开 t.me 链接")


def load_suite_config() -> dict:
    try:
        payload = json.loads(SUITE_CONFIG_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def configured_target() -> str:
    cfg = load_suite_config()
    value = str(cfg.get("target_chat") or "").strip()
    if value:
        return value
    return str(
        os.environ.get("TG_BOT_CHAT_ID") or os.environ.get("TG_TARGET") or ""
    ).strip()


def save_target(target_id: int, title: str) -> None:
    cfg = load_suite_config()
    cfg["target_chat"] = str(target_id)
    cfg["target_title"] = title
    cfg["updated_at"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    SUITE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUITE_CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    tmp.replace(SUITE_CONFIG_PATH)


async def resolve_target(client: TelegramClient, raw_value: str):
    normalized = normalize_target(raw_value)
    peer = int(normalized) if re.fullmatch(r"-?\d+", normalized) else normalized
    entity = await client.get_entity(peer)
    target_id = int(utils.get_peer_id(entity))
    title = (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or str(target_id)
    )
    return entity, target_id, str(title)


def load_sources() -> list[SourceConfig]:
    defaults = [
        SourceConfig(
            username=username,
            parser_type=("folder" if username.lstrip("@").lower() == FOLDER_BOT_USERNAME else "file"),
        )
        for username in BOT_USERNAMES
    ]
    try:
        payload = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
        rows = payload.get("sources", []) if isinstance(payload, dict) else []
        sources = [
            SourceConfig(
                username=normalize_username(str(row["username"])),
                parser_type=(
                    row.get("parser_type")
                    if row.get("parser_type") in ("file", "folder", "channel")
                    else "file"
                ),
            )
            for row in rows
            if isinstance(row, dict) and row.get("username")
        ]
        return sources or defaults
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, KeyError):
        return defaults


def save_sources(sources: Iterable[SourceConfig]) -> None:
    tmp = SOURCES_PATH.with_suffix(".tmp")
    rows = sorted(sources, key=lambda source: source.key)
    tmp.write_text(
        json.dumps(
            {
                "sources": [
                    {"username": source.username, "parser_type": source.parser_type}
                    for source in rows
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    tmp.replace(SOURCES_PATH)


def write_control_response(request_id: str, payload: dict) -> None:
    CONTROL_RESPONSES.mkdir(parents=True, exist_ok=True)
    os.chmod(CONTROL_RESPONSES.parent, 0o700)
    os.chmod(CONTROL_RESPONSES, 0o700)
    target = CONTROL_RESPONSES / f"{request_id}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    tmp.replace(target)


def bot_api(method: str, **values):
    token = os.environ["TG_BOT_TOKEN"]
    data = urllib.parse.urlencode(
        {
            key: json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else str(value)
            for key, value in values.items()
        }
    ).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    if not payload.get("ok"):
        raise RuntimeError(f"Bot API {method} failed")
    return payload.get("result")


async def send_menu_message(
    text: str,
    *,
    chat_id: Optional[int] = None,
    keyboard: bool = True,
) -> None:
    values = {
        "chat_id": chat_id if chat_id is not None else os.environ["TG_BOT_CHAT_ID"],
        "text": text,
    }
    if keyboard:
        values["reply_markup"] = {
            "keyboard": [list(row) for row in UNIFIED_KEYBOARD_ROWS],
            "resize_keyboard": True,
            "is_persistent": True,
        }
    await asyncio.to_thread(bot_api, "sendMessage", **values)


def load_processed() -> set[str]:
    try:
        payload = json.loads(PROCESSED_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    if not isinstance(payload, dict):
        return set()
    values = {
        str(value)
        for value in payload.get("completed_keys", [])
        if str(value).strip()
    }
    values.update(
        f"legacy:{int(value)}"
        for value in payload.get("completed_start_ids", [])
        if str(value).isdigit()
    )
    return values


def save_processed(values: set[str]) -> None:
    tmp = PROCESSED_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "completed_keys": sorted(
                    value for value in values if not value.startswith("legacy:")
                ),
                "completed_start_ids": sorted(
                    int(value.split(":", 1)[1])
                    for value in values
                    if value.startswith("legacy:")
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    os.chmod(tmp, 0o600)
    tmp.replace(PROCESSED_PATH)


def processed_key(source_id: int, start_id: int) -> str:
    return f"{source_id}:{start_id}"


def was_processed(values: set[str], source_id: int, start_id: int) -> bool:
    return (
        processed_key(source_id, start_id) in values
        or f"legacy:{start_id}" in values
    )


def extract_start_token(text: str) -> Optional[str]:
    match = START_RE.match((text or "").strip())
    return match.group(1) if match else None


def is_start_message(text: str) -> bool:
    return START_RE.match((text or "").strip()) is not None


def is_real_media(message: Message) -> bool:
    return message.media is not None and not isinstance(
        message.media, MessageMediaWebPage
    )


def group_media(messages: Iterable[Message]) -> list[list[Message]]:
    groups: list[list[Message]] = []
    current_key = object()
    for message in messages:
        key = message.grouped_id or message.id
        if not groups or key != current_key:
            groups.append([message])
            current_key = key
        else:
            groups[-1].append(message)
    return groups


def document_size(message: Message) -> int:
    document = getattr(message, "document", None)
    return int(getattr(document, "size", 0) or 0)


def batch_requires_mtproto(messages: Iterable[Message]) -> bool:
    return any(document_size(message) > MAX_BOT_UPLOAD_BYTES for message in messages)


def mtproto_media_kind(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if mime and mime.startswith("image/"):
        return "visual"
    if mime and mime.startswith("video/"):
        return "visual"
    if mime and mime.startswith("audio/"):
        return "audio"
    return "document"


def mtproto_media_chunks(paths: Iterable[str]) -> list[list[str]]:
    """Keep Telegram-compatible album types together, at most ten per send."""
    chunks: list[list[str]] = []
    for path in paths:
        kind = mtproto_media_kind(path)
        if (
            not chunks
            or len(chunks[-1]) >= 10
            or mtproto_media_kind(chunks[-1][0]) != kind
        ):
            chunks.append([path])
        else:
            chunks[-1].append(path)
    return chunks


def message_size(message: Message) -> int:
    """Best-effort media size for progress reporting."""
    value = document_size(message)
    if value:
        return value
    file = getattr(message, "file", None)
    try:
        return int(getattr(file, "size", 0) or 0)
    except (TypeError, ValueError):
        return 0


def format_bytes(value: int) -> str:
    value = max(0, int(value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{value}B"
        value /= 1024
    return f"{value:.1f}TB"


def safe_batch_name(batch_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(batch_id)).strip("._")[:180] or "batch"


def batch_stage_dir(batch_id: str) -> Path:
    return BATCH_STAGING_DIR / safe_batch_name(batch_id)


def stage_file_name(index: int, message: Message) -> str:
    source = media_name(message)
    source = re.sub(r"[^A-Za-z0-9_.-]+", "_", source).strip("._")
    if not source or source.startswith("media-") or "." not in source:
        file_obj = getattr(message, "file", None)
        candidate = str(getattr(file_obj, "name", "") or "")
        if "." in candidate:
            source = re.sub(
                r"[^A-Za-z0-9_.-]+", "_", Path(candidate).name
            ).strip("._")
        if "." not in source:
            mime = str(
                getattr(getattr(message, "document", None), "mime_type", "") or ""
            )
            source = f"media-{message.id}{mimetypes.guess_extension(mime) or '.bin'}"
    return f"{index:03d}-{source[:150]}"


def load_stage_state(directory: Path) -> dict:
    path = directory / ".progress.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            value.setdefault("files", {})
            return value
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return {"version": 1, "files": {}}


def save_stage_state(directory: Path, state: dict) -> None:
    path = directory / ".progress.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def is_file_reference_error(exc: BaseException) -> bool:
    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.__class__.__name__ == "FileReferenceExpiredError":
            return True
        current = current.__cause__ or current.__context__
    return "FileReferenceExpiredError" in repr(exc)


def normalize_messages(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [item for item in value if item is not None]
    return [value]


async def refresh_batch_messages(client, bot, messages: list[Message]) -> list[Message]:
    """Fetch fresh Message objects so Telegram file references are current."""
    ids = [int(message.id) for message in messages]
    if not ids:
        return []
    try:
        fetched = normalize_messages(await client.get_messages(bot, ids=ids))
    except Exception:
        log.warning("failed to refresh batch message references", exc_info=True)
        return list(messages)
    by_id = {int(message.id): message for message in fetched}
    return [by_id.get(int(message.id), message) for message in messages]


async def download_batch_files(
    client,
    bot,
    messages: list[Message],
    stage_dir: Path,
    on_progress=None,
) -> tuple[list[Message], list[str], dict]:
    """Download a batch with persistent files, refreshes, retries and bounded concurrency."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    refreshed = await refresh_batch_messages(client, bot, messages)
    state = load_stage_state(stage_dir)
    files_state = state.setdefault("files", {})
    active_bytes: dict[int, int] = {}
    progress_lock = asyncio.Lock()
    state_lock = asyncio.Lock()
    refresh_lock = asyncio.Lock()
    total_bytes = sum(message_size(message) for message in refreshed)
    completed_bytes = 0
    completed_count = 0

    for index, message in enumerate(refreshed):
        key = str(index)
        row = files_state.get(key) or {}
        path = stage_dir / stage_file_name(index, message)
        if row.get("path") and Path(str(row["path"])).name == path.name:
            path = stage_dir / str(row["path"])
        expected = int(row.get("size") or message_size(message) or 0)
        if row.get("complete") and path.is_file() and path.stat().st_size > 0:
            if not expected or path.stat().st_size == expected:
                completed_bytes += path.stat().st_size
                completed_count += 1
                continue
        files_state[key] = {
            "message_id": int(message.id),
            "path": path.name,
            "size": expected,
            "complete": False,
        }
    save_stage_state(stage_dir, state)

    async def update_progress(index: int, got: int, *, complete: bool = False) -> None:
        nonlocal completed_bytes, completed_count
        async with progress_lock:
            active_bytes[index] = max(0, int(got))
            if complete:
                completed_bytes += max(0, int(got))
                completed_count += 1
                active_bytes.pop(index, None)
            done_bytes = completed_bytes + sum(active_bytes.values())
            if on_progress is not None:
                on_progress(completed_count, len(refreshed), done_bytes, total_bytes)

    async def refresh_one(index: int) -> Message:
        async with refresh_lock:
            fresh = await refresh_batch_messages(client, bot, [refreshed[index]])
            if fresh:
                refreshed[index] = fresh[0]
            return refreshed[index]

    semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    async def download_one(index: int) -> str:
        message = refreshed[index]
        row = files_state[str(index)]
        path = stage_dir / row["path"]
        if row.get("complete") and path.is_file() and path.stat().st_size > 0:
            return str(path)
        async with semaphore:
            for attempt in range(DOWNLOAD_RETRIES):
                part = path.with_name(path.name + ".part")
                part.unlink(missing_ok=True)
                try:
                    try:
                        result = await message.download_media(
                            file=str(part),
                            progress_callback=lambda got, _total, i=index: asyncio.create_task(
                                update_progress(i, got)
                            ),
                        )
                    except TypeError as exc:
                        # Small test doubles and a few Telethon-compatible
                        # wrappers do not expose progress_callback.
                        if "progress_callback" not in str(exc):
                            raise
                        result = await message.download_media(file=str(part))
                    returned = Path(str(result)) if result else part
                    if not part.exists() and returned.is_file():
                        shutil.move(str(returned), str(part))
                    if not part.is_file() or part.stat().st_size <= 0:
                        raise RuntimeError(f"media message {message.id} produced no file")
                    part.replace(path)
                    row["complete"] = True
                    row["size"] = path.stat().st_size
                    async with state_lock:
                        save_stage_state(stage_dir, state)
                    await update_progress(index, row["size"], complete=True)
                    log.info(
                        "downloaded batch file %d/%d: %s (%s)",
                        index + 1,
                        len(refreshed),
                        path.name,
                        format_bytes(row["size"]),
                    )
                    return str(path)
                except Exception as exc:
                    part.unlink(missing_ok=True)
                    if attempt + 1 >= DOWNLOAD_RETRIES:
                        raise
                    if is_file_reference_error(exc):
                        message = await refresh_one(index)
                    delay = min(15, 2 ** attempt)
                    log.warning(
                        "batch file %d/%d download failed (attempt %d/%d), retrying in %ss: %s",
                        index + 1,
                        len(refreshed),
                        attempt + 1,
                        DOWNLOAD_RETRIES,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
        raise RuntimeError(f"download worker exited for message {message.id}")

    paths = await asyncio.gather(*(download_one(index) for index in range(len(refreshed))))
    return refreshed, paths, {
        "count": completed_count,
        "total": len(refreshed),
        "bytes": completed_bytes,
        "total_bytes": total_bytes,
    }


def media_name(message: Message) -> str:
    document = getattr(message, "document", None)
    if document:
        for attribute in getattr(document, "attributes", ()) or ():
            if isinstance(attribute, DocumentAttributeFilename):
                return attribute.file_name
    return f"media-{message.id}"


def has_next_button(message: Message) -> bool:
    for row in message.buttons or []:
        for button in row:
            if (
                NEXT_GROUP_TEXT in (button.text or "")
                and getattr(button, "data", None)
            ):
                return True
    return False


async def click_next_group(message: Message) -> bool:
    if not has_next_button(message):
        return False
    try:
        await message.click(
            filter=lambda button: (
                button.text
                and NEXT_GROUP_TEXT in button.text
                and getattr(button, "data", None)
            )
        )
        log.info("requested next media group after status %s", message.id)
        return True
    except Exception:
        log.exception("failed to request next media group after status %s", message.id)
        return False


async def resolve_file_bot_batch(
    client: TelegramClient,
    bot,
    replied_media: Message,
) -> Optional[ResolvedBatch]:
    """Resolve the complete file-bot batch containing ``replied_media``.

    The closest preceding outgoing /start is the batch start. The next
    outgoing /start is its upper boundary, so repeated requests for the same
    token remain separate and cannot be mixed together.
    """
    older = await client.get_messages(
        bot,
        limit=HISTORY_LIMIT,
        offset_id=replied_media.id,
    )
    starts = [
        message
        for message in older
        if message.out and is_start_message(message.raw_text or "")
    ]
    if not starts:
        return None
    start = max(starts, key=lambda message: message.id)
    token = extract_start_token(start.raw_text or "") or "(plain /start)"

    # reverse=True is required: without it Telethon returns the NEWEST
    # HISTORY_LIMIT messages above min_id, not the ones right after start.
    after = await client.get_messages(
        bot,
        min_id=start.id,
        limit=HISTORY_LIMIT,
        reverse=True,
    )
    media: list[Message] = []
    progress_messages: list[Message] = []
    caption = ""
    complete = False
    for message in sorted(after, key=lambda item: item.id):
        if message.id <= start.id:
            continue
        # A later request marks the end of this request's chat history.
        if message.out and is_start_message(message.raw_text or ""):
            break
        text = message.raw_text or ""
        description = DESCRIPTION_RE.search(text)
        if description and not caption:
            caption = description.group(1).strip()
        if is_real_media(message):
            media.append(message)
        if GROUP_DONE_TEXT in text:
            complete = True
            break
        if GROUP_PROGRESS_TEXT in text and has_next_button(message):
            progress_messages.append(message)

    if replied_media.id not in {message.id for message in media}:
        return None
    return ResolvedBatch(
        start_id=start.id,
        token=token,
        media=media,
        caption=caption,
        complete=complete,
        progress_messages=progress_messages,
    )


def parse_folder_card(message: Message) -> Optional[FolderCard]:
    if message.out or is_real_media(message):
        return None
    text = message.raw_text or ""
    count = FOLDER_COUNT_RE.search(text)
    if not count:
        return None
    photos = FOLDER_PHOTOS_RE.search(text)
    videos = FOLDER_VIDEOS_RE.search(text)
    lines = text.splitlines()
    title = lines[0].strip().removeprefix("📁").strip() if lines else ""
    return FolderCard(
        id=message.id,
        count=int(count.group(1)),
        title=title,
        photos=int(photos.group(1)) if photos else None,
        videos=int(videos.group(1)) if videos else None,
    )


def is_folder_boundary(message: Message) -> bool:
    """A bot message that separates one folder's media from the next."""
    if message.out:
        return False
    if parse_folder_card(message) is not None:
        return True
    return FOLDER_DONE_TEXT in (message.raw_text or "")


def extend_media_run(
    run: list[Message],
    candidates: Iterable[Message],
    backward: bool,
) -> bool:
    """Grow ``run`` through ``candidates`` (nearest first). True if a boundary stopped it."""
    for message in candidates:
        if is_folder_boundary(message):
            return True
        if not is_real_media(message):
            continue
        edge = run[0] if backward else run[-1]
        if abs((message.date - edge.date).total_seconds()) > MEDIA_RUN_GAP_SECONDS:
            return False
        if backward:
            run.insert(0, message)
        else:
            run.append(message)
    return False


def collect_media_run(
    ordered: list[Message],
    replied_media: Message,
) -> tuple[list[Message], bool]:
    """The contiguous media block containing ``replied_media``.

    Returns the run and whether a folder boundary closes it on the newer side.
    """
    index = next(
        (position for position, message in enumerate(ordered)
         if message.id == replied_media.id),
        None,
    )
    if index is None:
        return [], False
    run = [ordered[index]]
    extend_media_run(run, reversed(ordered[:index]), backward=True)
    closed = extend_media_run(run, ordered[index + 1:], backward=False)
    return run, closed


def folder_run_fingerprint(run: list[Message]) -> tuple[int, int, int]:
    photos = sum(1 for message in run if getattr(message, "photo", None) is not None)
    videos = sum(1 for message in run if getattr(message, "video", None) is not None)
    return len(run), photos, videos


def match_owner_card(
    cards: list[FolderCard],
    run: list[Message],
) -> Optional[FolderCard]:
    """The card describing ``run``, or None. Never falls back to 'nearest card'.

    The card may sit on either side of its media: the bot delivers folder
    contents asynchronously, so id adjacency says nothing about ownership.
    Total size is deliberately not compared — document_size() cannot see photos.
    """
    count, photos, videos = folder_run_fingerprint(run)
    exact: list[FolderCard] = []
    loose: list[FolderCard] = []
    for card in cards:
        if card.count != count:
            continue
        if (
            card.photos is not None
            and card.videos is not None
            and photos + videos == count
            and (card.photos, card.videos) == (photos, videos)
        ):
            exact.append(card)
        else:
            loose.append(card)
    pool = exact or loose
    if not pool:
        return None
    low, high = run[0].id, run[-1].id

    def distance(card: FolderCard) -> int:
        if card.id < low:
            return low - card.id
        if card.id > high:
            return card.id - high
        return 0

    return min(pool, key=distance)


def folder_batch_complete(
    run: list[Message],
    card: Optional[FolderCard],
    closed: bool,
) -> tuple[bool, str]:
    if closed:
        return True, "closed-by-boundary"
    if card is not None and card.count == len(run):
        return True, f"count-match:{card.id}"
    newest = max(message.date for message in run)
    age = (datetime.now(timezone.utc) - newest).total_seconds()
    if age >= FOLDER_QUIESCENCE_SECONDS:
        return True, f"quiescent:{int(age)}s"
    return False, f"growing: {len(run)} media, newest {int(age)}s old"


def run_caption(run: list[Message], card: Optional[FolderCard]) -> str:
    """Prefer the album's own caption — it cannot be misattributed."""
    for message in run:
        text = (message.raw_text or "").strip()
        if text:
            if card is not None and card.title and card.title != text:
                log.warning(
                    "album caption %r disagrees with card %s title %r",
                    text, card.id, card.title,
                )
            return text
    if card is not None:
        return card.title
    log.warning("no caption for media run %s-%s", run[0].id, run[-1].id)
    return ""


async def resolve_folder_bot_batch(
    client: TelegramClient,
    bot,
    replied_media: Message,
) -> Optional[ResolvedBatch]:
    """Resolve one ePan folder from the media run around ``replied_media``."""
    if not is_real_media(replied_media):
        return None
    older = await client.get_messages(
        bot,
        limit=HISTORY_LIMIT,
        offset_id=replied_media.id,
    )
    newer = await client.get_messages(
        bot,
        min_id=replied_media.id,
        limit=HISTORY_LIMIT,
        reverse=True,
    )
    window = {message.id: message for message in older}
    window.update({message.id: message for message in newer})
    window[replied_media.id] = replied_media
    ordered = sorted(window.values(), key=lambda message: message.id)

    run, closed = collect_media_run(ordered, replied_media)
    if not run:
        return None
    cards = [
        card for card in (parse_folder_card(message) for message in ordered)
        if card is not None
    ]
    card = match_owner_card(cards, run)
    complete, reason = folder_batch_complete(run, card, closed)
    return ResolvedBatch(
        start_id=run[0].id,
        token=f"folder:{run[0].id}-{run[-1].id}",
        media=run,
        caption=run_caption(run, card),
        complete=complete,
        card_id=card.id if card else None,
        complete_reason=reason,
    )


async def resolve_direct_media_batch(
    client: TelegramClient,
    source,
    replied_media: Message,
) -> Optional[ResolvedBatch]:
    """Resolve a media item or Telegram album directly in a channel/group."""
    if not is_real_media(replied_media):
        return None
    if replied_media.grouped_id:
        ids = list(range(max(1, replied_media.id - 20), replied_media.id + 21))
        window = await client.get_messages(source, ids=ids)
        media = sorted(
            [
                message
                for message in window
                if message
                and message.grouped_id == replied_media.grouped_id
                and is_real_media(message)
            ],
            key=lambda message: message.id,
        )
    else:
        media = [replied_media]
    if not media:
        return None
    caption = next(
        ((message.raw_text or "").strip() for message in media if (message.raw_text or "").strip()),
        "",
    )
    newest = max(message.date for message in media)
    age = (datetime.now(timezone.utc) - newest).total_seconds()
    complete = not replied_media.grouped_id or age >= FOLDER_QUIESCENCE_SECONDS
    return ResolvedBatch(
        start_id=media[0].id,
        token=f"direct:{source.id}:{media[0].id}-{media[-1].id}",
        media=media,
        caption=caption,
        complete=complete,
        complete_reason=("single" if not replied_media.grouped_id else f"settled:{int(age)}s"),
    )


async def resolve_batch(
    client: TelegramClient,
    bot,
    replied_media: Message,
) -> Optional[ResolvedBatch]:
    username = (getattr(bot, "username", "") or "").lower()
    source_type = SOURCE_TYPES.get(str(bot.id), SOURCE_TYPES.get(username, "file"))
    if source_type == "channel":
        return await resolve_direct_media_batch(client, bot, replied_media)
    if source_type == "folder":
        return await resolve_folder_bot_batch(client, bot, replied_media)
    return await resolve_file_bot_batch(client, bot, replied_media)


async def edit_trigger(client: TelegramClient, bot, message_id: int, text: str) -> None:
    try:
        await client.edit_message(bot, message_id, text)
    except Exception:
        log.exception("failed to update trigger message %s", message_id)


async def deliver_oversized_media(
    client: TelegramClient,
    target,
    message: Message,
    workdir: str,
    caption: Optional[str],
) -> Optional[tuple[str, int]]:
    """Deliver media outside the cloud Bot API's 50 MB upload limit.

    A server-side MTProto forward is effectively instant and preserves the
    original media. If the source protects forwarding, fall back to downloading
    and uploading through the logged-in Telegram user session.
    """
    try:
        result = await client.forward_messages(target, message)
        sent_message_id = _sent_message_id(result)
        if sent_message_id is None:
            raise RuntimeError("Telegram forward did not return a message ID")
        log.info(
            "server-side forwarded oversized %s (message %s, %d bytes)",
            media_name(message),
            message.id,
            document_size(message),
        )
        return "forwarded", sent_message_id
    except Exception:
        log.warning(
            "server-side forward failed for oversized message %s; "
            "falling back to MTProto re-upload",
            message.id,
            exc_info=True,
        )

    try:
        downloaded = await message.download_media(file=workdir)
        if not downloaded:
            log.warning(
                "oversized media message %s produced no downloaded file",
                message.id,
            )
            return None
        result = await client.send_file(
            target,
            downloaded,
            caption=caption,
            supports_streaming=True,
        )
        sent_message_id = _sent_message_id(result)
        if sent_message_id is None:
            raise RuntimeError("Telegram upload did not return a message ID")
        log.info(
            "MTProto re-uploaded oversized %s (message %s, %d bytes)",
            media_name(message),
            message.id,
            document_size(message),
        )
        return "reuploaded", sent_message_id
    except Exception:
        log.exception("failed to deliver oversized media message %s", message.id)
    return None


def _sent_message_id(result) -> Optional[int]:
    """Normalize Telethon's single-message/list return values to an ID."""
    if isinstance(result, (list, tuple)):
        result = result[0] if result else None
    value = getattr(result, "id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _sent_message_ids(result) -> list[int]:
    rows = result if isinstance(result, (list, tuple)) else [result]
    ids = [_sent_message_id(row) for row in rows]
    return [value for value in ids if value is not None]


async def deliver_batch(
    client: TelegramClient,
    bot,
    trigger: Message,
    batch: ResolvedBatch,
) -> None:
    bot_token = os.environ["TG_BOT_TOKEN"]
    bot_chat_id = int(os.environ["TG_BOT_CHAT_ID"])
    sent = 0
    skipped = 0
    oversized_sent = 0
    oversized_reuploaded = 0
    caption_pending = batch.caption or None
    target = None
    batch_id = f"{bot.id}:{batch.start_id}"
    previous = next(
        (
            row
            for row in load_manifests()
            if str(row.get("id")) == batch_id
            and str(row.get("status") or "complete").lower() in {"pending", "failed"}
            and int(row.get("expected_count") or 0) == len(batch.media)
            and int(row.get("target_chat_id") or 0) == bot_chat_id
        ),
        None,
    )
    delivered_message_ids = list((previous or {}).get("message_ids") or [])
    resume_count = min(len(delivered_message_ids), len(batch.media))
    sent = resume_count
    if resume_count:
        caption_pending = None
        log.info(
            "resuming forward batch %s after %d delivered messages",
            batch.start_id,
            resume_count,
        )
    manifest_payload = {
        "id": batch_id,
        "status": "pending",
        "source_id": int(bot.id),
        "target_chat_id": bot_chat_id,
        "title": batch.caption or "未命名",
        "expected_count": len(batch.media),
        "message_ids": list(delivered_message_ids),
        "created_at": (previous or {}).get("created_at")
        or datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = write_manifest(manifest_payload)
    has_oversized = batch_requires_mtproto(batch.media)

    def update_manifest(status: str, error: Optional[Exception] = None) -> None:
        manifest_payload["status"] = status
        manifest_payload["message_ids"] = list(delivered_message_ids)
        if error is not None:
            manifest_payload["error"] = f"{type(error).__name__}: {error}"[:500]
        else:
            manifest_payload.pop("error", None)
        write_manifest(manifest_payload)

    success = False
    stage_dir = batch_stage_dir(batch_id)
    last_progress_notice = 0.0
    last_trigger_notice = 0.0

    def report_download_progress(done: int, total: int, done_bytes: int, total_bytes: int) -> None:
        nonlocal last_progress_notice, last_trigger_notice
        now = time.monotonic()
        if now - last_progress_notice < 5 and done < total:
            return
        last_progress_notice = now
        manifest_payload.update(
            {
                "downloaded_count": done,
                "download_total": total,
                "downloaded_bytes": done_bytes,
                "download_total_bytes": total_bytes,
                "download_percent": int(done_bytes * 100 / total_bytes) if total_bytes else 0,
            }
        )
        write_manifest(manifest_payload)
        log.info(
            "batch %s download progress: %d/%d files, %s/%s (%d%%)",
            batch.start_id,
            done,
            total,
            format_bytes(done_bytes),
            format_bytes(total_bytes),
            manifest_payload["download_percent"],
        )
        if now - last_trigger_notice >= 30 or done >= total:
            last_trigger_notice = now
            asyncio.create_task(
                edit_trigger(
                    client,
                    bot,
                    trigger.id,
                    "转发（下载中："
                    f"已完成 {done}/{total} 个文件，"
                    f"{format_bytes(done_bytes)}/{format_bytes(total_bytes)}，"
                    f"{manifest_payload['download_percent']}%）",
                )
            )

    try:
        refreshed_media, staged_paths, download_stats = await download_batch_files(
            client,
            bot,
            list(batch.media),
            stage_dir,
            on_progress=report_download_progress,
        )
        manifest_payload.update(
            {
                "downloaded_count": download_stats["count"],
                "download_total": download_stats["total"],
                "downloaded_bytes": download_stats["bytes"],
                "download_total_bytes": download_stats["total_bytes"],
                "download_percent": 100,
            }
        )
        write_manifest(manifest_payload)
        remaining_paths = staged_paths[resume_count:]
        if has_oversized:
            # Once one item crosses the Bot API limit, send the whole logical
            # batch through MTProto. Files are already staged and can be sent
            # in compatible chunks without redownloading them.
            target = await client.get_entity(bot_chat_id)
            for paths in mtproto_media_chunks(remaining_paths):
                payload = paths if len(paths) > 1 else paths[0]
                result = await client.send_file(
                    target,
                    payload,
                    caption=caption_pending,
                    supports_streaming=True,
                )
                ids = _sent_message_ids(result)
                if len(ids) != len(paths):
                    raise RuntimeError(
                        "Telegram upload returned an unexpected number of message IDs"
                    )
                delivered_message_ids.extend(ids)
                sent += len(ids)
                caption_pending = None
            oversized_sent = sum(
                document_size(message) > MAX_BOT_UPLOAD_BYTES
                for message in refreshed_media
            )
            oversized_reuploaded = oversized_sent
        else:
            plan: list[list[str]] = []
            remaining_messages = refreshed_media[resume_count:]
            # Preserve source album boundaries while using the staged paths.
            offset = 0
            for group in group_media(remaining_messages):
                size = len(group)
                paths = remaining_paths[offset : offset + size]
                offset += size
                if paths:
                    plan.append(paths)

            log.info(
                "batch %s prepared: %d deliveries queued, sending back-to-back",
                batch.start_id,
                len(plan),
            )
            for paths in plan:
                ids = await send_media_batches(
                    bot_token, bot_chat_id, paths, caption_pending
                )
                if len(ids) != len(paths):
                    raise RuntimeError(
                        "Bot API returned an unexpected number of message IDs"
                    )
                delivered_message_ids.extend(ids)
                sent += len(ids)
                caption_pending = None

        if sent != len(batch.media) or len(delivered_message_ids) != len(batch.media):
            raise RuntimeError(
                f"batch incomplete: expected {len(batch.media)}, sent {sent}"
            )
        update_manifest("complete")
        success = True
        shutil.rmtree(stage_dir, ignore_errors=True)
        log.info(
            "completed forward batch manifest %s (%d messages, mtproto=%s)",
            manifest_path,
            len(batch.media),
            has_oversized,
        )
    except Exception as exc:
        skipped = max(0, len(batch.media) - len(delivered_message_ids))
        update_manifest("failed", exc)
        log.exception(
            "failed to deliver batch %s; manifest retained at %s",
            batch.start_id,
            manifest_path,
        )

    if success:
        completed = load_processed()
        completed.add(processed_key(bot.id, batch.start_id))
        save_processed(completed)
    await edit_trigger(
        client,
        bot,
        trigger.id,
        (
            "转发（完成："
            f"已发送 {sent} 个媒体"
            + (f"，其中大文件重传 {oversized_sent} 个" if oversized_sent else "")
            + "）"
            if success
            else f"转发（失败：已发送 {sent} 个，剩余 {skipped} 个；可重新转发）"
        ),
    )
    log.info(
        "manual batch start=%s token=%s sent=%s oversized=%s "
        "oversized_reuploaded=%s skipped=%s",
        batch.start_id,
        batch.token,
        sent,
        oversized_sent,
        oversized_reuploaded,
        skipped,
    )


async def process_trigger(
    client: TelegramClient,
    bot,
    trigger: Message,
    processing: set[tuple[int, int]],
) -> None:
    processing_key = (bot.id, trigger.id)
    if processing_key in processing:
        return
    processing.add(processing_key)
    try:
        replied = await trigger.get_reply_message()
        if not replied or not is_real_media(replied):
            await edit_trigger(client, bot, trigger.id, "转发（失败：请回复来源对话里的照片、视频或音频）")
            return
        await edit_trigger(client, bot, trigger.id, "转发（处理中，请稍候）")
        deadline = asyncio.get_running_loop().time() + WAIT_FOR_COMPLETE
        batch: Optional[ResolvedBatch] = None
        while asyncio.get_running_loop().time() < deadline:
            batch = await resolve_batch(client, bot, replied)
            if batch and batch.complete:
                break
            if batch:
                for status in batch.progress_messages[-1:]:
                    await click_next_group(status)
            await asyncio.sleep(5)
        if not batch:
            log.warning(
                "no batch resolved: trigger=%s replied=%s bot=%s",
                trigger.id,
                replied.id,
                getattr(bot, "username", bot.id),
            )
            await edit_trigger(client, bot, trigger.id, "转发（失败：无法定位媒体批次）")
            return
        if not batch.complete:
            log.warning(
                "batch not complete: trigger=%s replied=%s token=%s media=%s "
                "card=%s reason=%s",
                trigger.id,
                replied.id,
                batch.token,
                len(batch.media),
                batch.card_id,
                batch.complete_reason,
            )
            await edit_trigger(
                client,
                bot,
                trigger.id,
                f"转发（失败：批次未确认完成，已收到 {len(batch.media)} 个媒体）",
            )
            return
        log.info(
            "batch resolved: trigger=%s token=%s media=%s card=%s reason=%s",
            trigger.id,
            batch.token,
            len(batch.media),
            batch.card_id,
            batch.complete_reason,
        )
        if was_processed(load_processed(), bot.id, batch.start_id):
            await edit_trigger(client, bot, trigger.id, "转发（该批次已经处理过）")
            return
        await deliver_batch(client, bot, trigger, batch)
    except Exception:
        log.exception("failed to process trigger %s", trigger.id)
        await edit_trigger(client, bot, trigger.id, "转发（失败：请查看 VPS 日志）")
    finally:
        processing.discard(processing_key)


async def maybe_click_progress(message: Message, clicked: set[int]) -> None:
    if message.out or message.id in clicked:
        return
    text = message.raw_text or ""
    if GROUP_PROGRESS_TEXT not in text or not has_next_button(message):
        return
    clicked.add(message.id)
    await click_next_group(message)


async def main(
    check_trigger: Optional[int] = None,
    process_trigger_id: Optional[int] = None,
    selected_bot: Optional[str] = None,
    session: str = "service",
) -> None:
    load_env()
    if not BOT_USERNAMES:
        raise SystemExit(
            "FORWARDER_BOT_USERNAMES is empty: list the file/folder bots the "
            "user account should talk to, comma separated (e.g. filebot,folderbot)."
        )
    # The legacy forwarding token may still exist in the persisted .env for
    # unrelated services. This process always uses the official 115bot.
    os.environ["TG_BOT_TOKEN"] = read_management_bot_token()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    client = TelegramClient(
        str(BASE / session),
        int(os.environ["TG_API_ID"]),
        os.environ["TG_API_HASH"],
    )
    await client.start()
    me = await client.get_me()
    target_entity, target_chat_id, target_title = await resolve_target(
        client, configured_target()
    )
    # 旧发送函数继续从环境变量取值；这里写入当前动态配置，避免重复改动
    # 成熟的发送/重传路径。
    os.environ["TG_BOT_CHAT_ID"] = str(target_chat_id)
    os.environ["TG_TARGET"] = str(target_chat_id)
    management_bot = await asyncio.to_thread(bot_api, "getMe")
    management_peer = await client.get_entity(f"@{management_bot['username']}")
    # Telethon identifies a bot private dialog by the bot's user id, while the
    # Bot API sends replies to the human user's id.
    menu_peers = {management_peer.id: management_peer}
    menu_reply_chat_ids = {management_peer.id: me.id}
    sources = (
        [SourceConfig(normalize_username(selected_bot))]
        if selected_bot
        else load_sources()
    )
    SOURCE_TYPES.clear()
    SOURCE_TYPES.update({source.key: source.parser_type for source in sources})
    bot_usernames = tuple(source.username for source in sources)
    bots = [await client.get_entity(source_peer(username)) for username in bot_usernames]
    for source, entity in zip(sources, bots):
        SOURCE_TYPES[str(entity.id)] = source.parser_type

    if check_trigger is not None or process_trigger_id is not None:
        bot = bots[0]
        trigger_id = check_trigger if check_trigger is not None else process_trigger_id
        assert trigger_id is not None
        trigger = await client.get_messages(bot, ids=trigger_id)
        if process_trigger_id is not None:
            if not trigger:
                print("NO_TRIGGER")
            else:
                await process_trigger(client, bot, trigger, set())
                print("PROCESSED")
            await client.disconnect()
            return
        replied = await trigger.get_reply_message() if trigger else None
        batch = await resolve_batch(client, bot, replied) if replied else None
        if batch:
            oversized = [
                (message.id, media_name(message), document_size(message))
                for message in batch.media
                if document_size(message) > MAX_BOT_UPLOAD_BYTES
            ]
            print(
                json.dumps(
                    {
                        "start_id": batch.start_id,
                        "token": batch.token,
                        "complete": batch.complete,
                        "complete_reason": batch.complete_reason,
                        "card_id": batch.card_id,
                        "media_count": len(batch.media),
                        "media_ids": [message.id for message in batch.media],
                        "already_processed": was_processed(
                            load_processed(), bot.id, batch.start_id
                        ),
                        "caption": batch.caption,
                        "oversized": oversized,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print("NO_BATCH")
        await client.disconnect()
        return

    recent_by_bot = {
        bot.id: await client.get_messages(bot, limit=100)
        for bot in bots
    }
    seen_triggers = {
        (bot.id, message.id)
        for bot in bots
        for message in recent_by_bot[bot.id]
        if message.out and (message.raw_text or "").strip() == TRIGGER_TEXT
    }
    clicked_progress: set[int] = set()
    processing: set[tuple[int, int]] = set()
    trigger_lock = asyncio.Lock()
    last_seen_ids = {
        bot.id: max((message.id for message in recent_by_bot[bot.id]), default=0)
        for bot in bots
    }
    bots_by_id = {bot.id: bot for bot in bots}
    menu_states: dict[int, str] = {}
    source_lock = asyncio.Lock()
    last_menu_message_ids: dict[int, int] = {}
    for chat_id, peer in menu_peers.items():
        recent_menu_messages = await client.get_messages(peer, limit=1)
        last_menu_message_ids[chat_id] = max(
            (message.id for message in recent_menu_messages),
            default=0,
        )

    async def list_sources_text() -> str:
        rows = []
        for index, source in enumerate(load_sources(), 1):
            parser_name = {
                "folder": "文件夹机器人",
                "channel": "频道/群组媒体",
            }.get(source.parser_type, "普通文件机器人")
            rows.append(f"{index}. {source.username}（{parser_name}模式）")
        return "已监听来源：\n" + ("\n".join(rows) if rows else "暂无")

    async def target_text() -> str:
        cfg = load_suite_config()
        title = str(cfg.get("target_title") or target_title)
        return f"当前目标群：{title}\nID：{target_chat_id}"

    async def add_source(raw_value: str, parser_type: str) -> SourceConfig:
        username = normalize_username(raw_value)
        entity = await client.get_entity(source_peer(username))
        if getattr(entity, "bot", False):
            if parser_type not in ("file", "folder"):
                raise ValueError("文件机器人请选择普通模式或文件夹模式")
        elif is_channel_or_group(entity):
            parser_type = "channel"
        else:
            raise ValueError("只支持文件机器人、频道或群组")
        current = load_sources()
        if any(source.key == username.lstrip("@").lower() for source in current):
            raise ValueError(f"{username} 已经在监听来源列表中")
        selected = SourceConfig(username=username, parser_type=parser_type)
        current.append(selected)
        save_sources(current)
        return selected

    async def remove_source(raw_value: str) -> SourceConfig:
        username = normalize_username(raw_value)
        current = load_sources()
        selected = next((source for source in current if source.key == username.lstrip("@").lower()), None)
        if not selected:
            raise ValueError(f"{username} 不在监听来源列表中")
        if len(current) <= 1:
            raise ValueError("至少需要保留一个监听对话")
        save_sources(source for source in current if source.key != selected.key)
        return selected

    async def handle_menu_message(message: Message, reply_chat_id: int) -> bool:
        text = (message.raw_text or "").strip()
        lowered = text.split("@", 1)[0].lower()
        menu_state = menu_states.get(reply_chat_id)
        if lowered in {
            MENU_COMMAND,
            "/forwarder_list",
            "/forwarder_target",
            "/forwarder_target_set",
        } or text in MENU_BUTTONS:
            pass
        elif menu_state and (
            text.startswith("@")
            or "t.me/" in text
            or re.fullmatch(r"-?\d+", text)
            or text in MODE_BUTTONS
        ):
            pass
        else:
            return False

        if lowered == MENU_COMMAND or text == "来源管理":
            menu_states.pop(reply_chat_id, None)
            await send_menu_message(
                "统一管理菜单\n\n可添加文件机器人、频道或群组。"
                "在来源媒体下面回复“转发”，系统会送到目标群。\n"
                "目标群、指定用户和 115 目录也可以在这里管理。",
                chat_id=reply_chat_id,
            )
        elif lowered == "/forwarder_list" or text == "已监听来源":
            menu_states.pop(reply_chat_id, None)
            await send_menu_message(
                await list_sources_text(),
                chat_id=reply_chat_id,
            )
        elif text == "添加来源":
            menu_states[reply_chat_id] = "add"
            await send_menu_message(
                "请发送文件机器人、频道或群组的 ID、@用户名或 t.me 链接。",
                chat_id=reply_chat_id,
                keyboard=False,
            )
        elif text == "删除来源":
            menu_states[reply_chat_id] = "remove"
            await send_menu_message(
                (await list_sources_text()) + "\n\n请发送要删除的 @用户名。",
                chat_id=reply_chat_id,
                keyboard=False,
            )
        elif lowered == "/forwarder_target":
            menu_states.pop(reply_chat_id, None)
            await send_menu_message(await target_text(), chat_id=reply_chat_id)
        elif lowered == "/forwarder_target_set":
            menu_states[reply_chat_id] = "target"
            await send_menu_message(
                "请发送目标群的 ID、@用户名或公开 t.me 链接。\n"
                "请先确认你的个人账号和转发机器人都能在该对话发消息。",
                chat_id=reply_chat_id,
                keyboard=False,
            )
        elif menu_state == "add":
            username = normalize_username(text)
            entity = await client.get_entity(source_peer(username))
            if not getattr(entity, "bot", False):
                if not is_channel_or_group(entity):
                    raise ValueError("只支持文件机器人、频道或群组")
                async with source_lock:
                    await add_source(username, "channel")
                menu_states.pop(reply_chat_id, None)
                await send_menu_message(
                    f"已添加 {username}（频道/群组媒体模式）。服务正在自动重载。",
                    chat_id=reply_chat_id,
                )
                asyncio.create_task(restart_after_menu())
                return True
            menu_states[reply_chat_id] = f"add_type:{username}"
            await send_menu_message(
                f"已识别 {username}。请选择消息模式。",
                chat_id=reply_chat_id,
                keyboard=False,
            )
            await asyncio.to_thread(
                bot_api,
                "sendMessage",
                chat_id=reply_chat_id,
                text="普通机器人通常用 /start 获取文件；文件夹机器人会显示文件夹详情和媒体数量。",
                reply_markup={
                    "keyboard": [[MODE_BUTTONS[0], MODE_BUTTONS[1]], [MODE_BUTTONS[2]]],
                    "resize_keyboard": True,
                    "one_time_keyboard": True,
                },
            )
        elif menu_state and menu_state.startswith("add_type:"):
            if text == "取消":
                menu_states.pop(reply_chat_id, None)
                await send_menu_message("已取消添加。", chat_id=reply_chat_id)
                return True
            if text not in MODE_BUTTONS[:2]:
                raise ValueError("请点击“普通文件模式”或“文件夹模式”")
            parser_type = "folder" if text == "文件夹模式" else "file"
            username = menu_state.split(":", 1)[1]
            async with source_lock:
                await add_source(username, parser_type)
            menu_states.pop(reply_chat_id, None)
            await send_menu_message(
                f"已添加 {username}。服务正在自动重载。",
                chat_id=reply_chat_id,
            )
            asyncio.create_task(restart_after_menu())
        elif menu_state == "remove":
            async with source_lock:
                removed = await remove_source(text)
            menu_states.pop(reply_chat_id, None)
            await send_menu_message(
                f"已删除 {removed.username}。服务正在自动重载。",
                chat_id=reply_chat_id,
            )
            asyncio.create_task(restart_after_menu())
        elif menu_state == "target":
            _entity, new_target_id, new_target_title = await resolve_target(
                client, text
            )
            if new_target_id == target_chat_id:
                raise ValueError("这就是当前目标群")
            # 个人会话能解析不代表机器人能发；用 Bot API 发一条探测消息，
            # 失败则不落配置。
            await asyncio.to_thread(
                bot_api,
                "sendMessage",
                chat_id=new_target_id,
                text="TG Media Suite 目标群验证成功",
            )
            save_target(new_target_id, new_target_title)
            menu_states.pop(reply_chat_id, None)
            await send_menu_message(
                f"目标群已改为 {new_target_title}（{new_target_id}）。\n"
                "转发器正在重载，归档器会在下一轮自动切换。",
                chat_id=reply_chat_id,
            )
            asyncio.create_task(restart_after_menu())
        return True

    async def restart_after_menu() -> None:
        await asyncio.sleep(2)
        os._exit(0)

    async def control_loop() -> None:
        CONTROL_REQUESTS.mkdir(parents=True, exist_ok=True)
        CONTROL_RESPONSES.mkdir(parents=True, exist_ok=True)
        os.chmod(CONTROL_REQUESTS.parent, 0o700)
        os.chmod(CONTROL_REQUESTS, 0o700)
        os.chmod(CONTROL_RESPONSES, 0o700)
        while True:
            try:
                for request_path in sorted(CONTROL_REQUESTS.glob("*.json")):
                    request_id = request_path.stem
                    restart = False
                    try:
                        payload = json.loads(request_path.read_text(encoding="utf-8"))
                        if float(payload.get("expires_at", 0)) < time.time():
                            raise ValueError("网页请求已过期，请重试")
                        action = payload.get("action")
                        if action == "source_add":
                            parser_type = payload.get("parser_type")
                            if parser_type not in ("file", "folder", "channel"):
                                raise ValueError("消息模式不合法")
                            async with source_lock:
                                added = await add_source(
                                    str(payload.get("username") or ""), parser_type
                                )
                            result = {
                                "ok": True,
                                "message": f"已添加 {added.username}（{added.parser_type}）",
                            }
                            restart = True
                        elif action == "source_remove":
                            async with source_lock:
                                removed = await remove_source(
                                    str(payload.get("username") or "")
                                )
                            result = {
                                "ok": True,
                                "message": f"已删除 {removed.username}",
                            }
                            restart = True
                        else:
                            raise ValueError("不支持的网页操作")
                    except Exception as exc:
                        log.exception("web control action failed: %s", request_id)
                        result = {"ok": False, "error": str(exc)[:240]}
                    write_control_response(request_id, result)
                    request_path.unlink(missing_ok=True)
                    if restart:
                        asyncio.create_task(restart_after_menu())
                        return
                await asyncio.sleep(0.75)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("web control polling failed")
                await asyncio.sleep(2)

    async def claim_trigger(bot, message: Message) -> bool:
        key = (bot.id, message.id)
        async with trigger_lock:
            if key in seen_triggers:
                return False
            seen_triggers.add(key)
            return True

    async def handle_message(bot, message: Message) -> None:
        last_seen_ids[bot.id] = max(last_seen_ids[bot.id], message.id)
        if message.out and (message.raw_text or "").strip() == TRIGGER_TEXT:
            if await claim_trigger(bot, message):
                await process_trigger(client, bot, message, processing)
            return
        await maybe_click_progress(message, clicked_progress)

    @client.on(events.NewMessage(chats=bots))
    async def on_new_message(event) -> None:
        bot = bots_by_id.get(event.chat_id)
        if bot is not None:
            await handle_message(bot, event.message)

    @client.on(events.NewMessage(chats=list(menu_peers.values()), from_users=me.id))
    async def on_admin_menu(event) -> None:
        try:
            chat_id = event.chat_id
            last_menu_message_ids[chat_id] = max(
                last_menu_message_ids.get(chat_id, 0),
                event.message.id,
            )
            await handle_menu_message(
                event.message,
                menu_reply_chat_ids[chat_id],
            )
        except Exception as exc:
            log.exception("menu action failed")
            reply_chat_id = menu_reply_chat_ids.get(event.chat_id, me.id)
            await send_menu_message(f"操作失败：{exc}", chat_id=reply_chat_id)

    async def poll() -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                for bot in bots:
                    messages = await client.get_messages(
                        bot,
                        min_id=last_seen_ids[bot.id],
                        limit=100,
                        reverse=True,
                    )
                    for message in messages:
                        await handle_message(bot, message)
                for chat_id, peer in menu_peers.items():
                    menu_messages = await client.get_messages(
                        peer,
                        min_id=last_menu_message_ids[chat_id],
                        limit=100,
                        reverse=True,
                    )
                    for message in menu_messages:
                        last_menu_message_ids[chat_id] = max(
                            last_menu_message_ids[chat_id],
                            message.id,
                        )
                        if message.out and message.sender_id == me.id:
                            await handle_menu_message(
                                message,
                                menu_reply_chat_ids[chat_id],
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("polling forwarding source failed")

    poll_task = asyncio.create_task(poll())
    control_task = asyncio.create_task(control_loop())
    log.info(
        "manual mode ready on @%s: reply %r in %s; target=%s",
        management_bot["username"],
        TRIGGER_TEXT,
        ", ".join(bot_usernames),
        f"{target_title} ({target_chat_id})",
    )
    try:
        await client.run_until_disconnected()
    finally:
        poll_task.cancel()
        control_task.cancel()
        await asyncio.gather(poll_task, control_task, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check-trigger", type=int)
    action.add_argument("--process-trigger", type=int)
    parser.add_argument("--bot")
    parser.add_argument(
        "--session",
        default="service",
        help="session file to use; pass 'user' to inspect without disturbing the service",
    )
    args = parser.parse_args()
    asyncio.run(
        main(
            check_trigger=args.check_trigger,
            process_trigger_id=args.process_trigger,
            selected_bot=args.bot,
            session=args.session,
        )
    )
