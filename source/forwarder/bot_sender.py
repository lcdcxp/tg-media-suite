"""Send local media files into a target chat via the official 115bot.

The user-account session pulls the real media from the configured file bot
(because bots can't talk to other bots). Delivery to the destination group
then goes through the Bot API so the visible sender is our own bot instead
of the user account.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
from typing import Iterable, List, Sequence

import httpx

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

API_BASE = "https://api.telegram.org"
ALBUM_LIMIT = 10  # sendMediaGroup allows at most 10 items per call
DEFAULT_TIMEOUT = 120.0


def _classify(path: str) -> str:
    """Return the Bot API media type for a local file path.

    Bot API audio messages are limited to formats Telegram accepts as audio
    (notably mp3/m4a). Other audio files, such as AAC, are sent as documents so
    the original bytes are still delivered instead of rejecting the batch.
    """
    mime, _ = mimetypes.guess_type(path)
    if mime and mime.startswith("video/"):
        return "video"
    if mime and mime.startswith("image/"):
        return "photo"
    suffix = Path(path).suffix.lower()
    if (mime and mime.startswith("audio/")) or suffix in {
        ".mp3",
        ".m4a",
        ".aac",
        ".flac",
        ".ogg",
        ".oga",
        ".opus",
        ".wav",
        ".wma",
    }:
        return "audio" if suffix in {".mp3", ".m4a"} else "document"
    # A document with an unknown extension is safer as a file than as a photo;
    # Telegram will reject sendPhoto for arbitrary audio/application files.
    return "document"


class BotAPIError(RuntimeError):
    def __init__(self, method: str, payload: dict) -> None:
        super().__init__(f"{method} failed: {payload}")
        self.method = method
        self.payload = payload
        self.error_code = payload.get("error_code")


async def _post(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    data: dict,
    files: dict | None = None,
) -> dict:
    url = f"{API_BASE}/bot{token}/{method}"
    response = await client.post(url, data=data, files=files, timeout=DEFAULT_TIMEOUT)
    payload = response.json()
    if not payload.get("ok"):
        raise BotAPIError(method, payload)
    return payload["result"]


async def _send_single(
    client: httpx.AsyncClient,
    token: str,
    chat_id: int,
    path: str,
    caption: str | None,
) -> list[int]:
    kind = _classify(path)
    method_by_kind = {
        "photo": ("sendPhoto", "photo"),
        "video": ("sendVideo", "video"),
        "audio": ("sendAudio", "audio"),
        "document": ("sendDocument", "document"),
    }
    method, field = method_by_kind[kind]
    data: dict = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if kind == "video":
        data["supports_streaming"] = "true"
    with open(path, "rb") as handle:
        result = await _post(client, token, method, data, files={field: handle})
    return [int(result["message_id"])] if isinstance(result, dict) else []


async def _send_album(
    client: httpx.AsyncClient,
    token: str,
    chat_id: int,
    paths: Sequence[str],
    caption: str | None,
) -> list[int]:
    media: List[dict] = []
    open_files: list = []
    files_payload: dict = {}
    try:
        for index, path in enumerate(paths):
            kind = _classify(path)
            attach_name = f"file{index}"
            entry: dict = {"type": kind, "media": f"attach://{attach_name}"}
            if kind == "video":
                entry["supports_streaming"] = True
            if index == 0 and caption:
                entry["caption"] = caption
            media.append(entry)
            handle = open(path, "rb")
            open_files.append(handle)
            files_payload[attach_name] = handle
        result = await _post(
            client,
            token,
            "sendMediaGroup",
            data={"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)},
            files=files_payload,
        )
        return [int(item["message_id"]) for item in result if item.get("message_id")]
    finally:
        for handle in open_files:
            try:
                handle.close()
            except Exception:
                pass


async def send_message(
    client: httpx.AsyncClient,
    token: str,
    chat_id: int,
    text: str,
) -> list[int]:
    result = await _post(
        client,
        token,
        "sendMessage",
        data={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        },
    )
    return [int(result["message_id"])] if isinstance(result, dict) and result.get("message_id") else []


async def send_text(token: str, chat_id: int, text: str) -> None:
    """Send a text message with a short-lived Bot API client."""
    async with httpx.AsyncClient() as client:
        await send_message(client, token, chat_id, text)


async def _send_chunk_resilient(
    client: httpx.AsyncClient,
    token: str,
    chat_id: int,
    paths: Sequence[str],
    caption: str | None,
) -> list[int]:
    """Send a chunk, recursively splitting albums rejected as too large."""
    if len(paths) == 1:
        return await _send_single(client, token, chat_id, paths[0], caption)
    try:
        return await _send_album(client, token, chat_id, paths, caption)
    except BotAPIError as error:
        if error.error_code != 413:
            raise
        midpoint = len(paths) // 2
        log.warning(
            "album request too large (%d files); splitting into %d + %d",
            len(paths),
            midpoint,
            len(paths) - midpoint,
        )
        first_ids = await _send_chunk_resilient(
            client, token, chat_id, paths[:midpoint], caption
        )
        second_ids = await _send_chunk_resilient(
            client, token, chat_id, paths[midpoint:], None
        )
        return first_ids + second_ids


async def send_media_batches(
    token: str,
    chat_id: int,
    paths: Sequence[str],
    caption: str | None,
) -> list[int]:
    """Send a batch of local media files as one or more albums.

    Splits into chunks of up to ALBUM_LIMIT items; caption is placed on the
    first item of the first chunk only. Single-file batches use the matching
    sendPhoto, sendVideo, sendAudio, or sendDocument method.
    """
    if not paths:
        return []
    async with httpx.AsyncClient() as client:
        all_ids: list[int] = []
        remaining = list(paths)
        caption_left = caption
        first = True
        while remaining:
            # Telegram only accepts homogeneous audio/document albums; photos
            # and videos may share an album. Split at type boundaries while
            # keeping the source order intact.
            first_kind = _classify(remaining[0])
            compatible = {"photo", "video"} if first_kind in {"photo", "video"} else {first_kind}
            end = 0
            while end < len(remaining) and end < ALBUM_LIMIT:
                if _classify(remaining[end]) not in compatible:
                    break
                end += 1
            chunk = remaining[:max(1, end)]
            remaining = remaining[len(chunk):]
            active_caption = caption_left if first else None
            sent_ids = await _send_chunk_resilient(
                client, token, chat_id, chunk, active_caption
            )
            all_ids.extend(sent_ids or [])
            if first:
                first = False
                caption_left = None
        return all_ids
