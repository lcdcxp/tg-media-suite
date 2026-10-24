"""Fetch full media from a configured file bot given a getFile_ token.

Flow observed:
  1. user sends `/start getFile_TOKEN`
  2. bot replies with a status message like
     "⌛️ 正在获取文件..." then sends photos/videos grouped as albums
  3. after each group, bot posts either
        "✅ 已获取第N组文件（共M组）  👇点击下方按钮获取下一组"  (with a callback button)
     or "✅ 文件全部获取成功！📁 共收到 N 组文件"                     (terminal)
  4. clicking the callback button `/get N TOKEN` triggers the next group
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Iterable, List, Optional

from telethon.tl.custom.message import Message
from telethon.tl.types import MessageMediaWebPage

log = logging.getLogger(__name__)

NEXT_GROUP_TEXT = "下一组"
GROUP_DONE_TEXT = "全部获取成功"
GROUP_PROGRESS_TEXT = "已获取第"
DEFAULT_PER_GROUP_TIMEOUT = 90
DEFAULT_MAX_GROUPS = 30

# Matches the bot deep-link token e.g. `getFile_LUoBwasxghxOZb`.
_TOKEN_RE = re.compile(r"getFile_([A-Za-z0-9_-]+)")


def extract_bot_token(message) -> Optional[str]:
    """Pull the getFile_TOKEN payload out of a source channel message.

    Scans the message text plus any URLs found in entities / web-page previews /
    inline buttons for the first `getFile_XXXX` occurrence.
    """
    candidates: List[str] = []

    raw_text = message.raw_text or ""
    if raw_text:
        candidates.append(raw_text)

    web = getattr(message, "web_preview", None)
    if web is not None:
        for attr in ("url", "display_url", "embed_url"):
            value = getattr(web, attr, None)
            if value:
                candidates.append(value)

    entities = getattr(message, "entities", None) or []
    for entity in entities:
        url = getattr(entity, "url", None)
        if url:
            candidates.append(url)

    buttons = getattr(message, "buttons", None) or []
    for row in buttons:
        for button in row:
            url = getattr(button, "url", None)
            if url:
                candidates.append(url)

    for text in candidates:
        match = _TOKEN_RE.search(text)
        if match:
            return match.group(1)
    return None


async def _wait_for_status(
    client,
    bot,
    since_id: int,
    timeout: float,
) -> Optional[Message]:
    """Poll bot chat until a status message with buttons or done marker appears."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(2.0)
        batch: List[Message] = await client.get_messages(bot, min_id=since_id, limit=100)
        # get_messages returns newest first; reverse to chronological
        chronological = list(reversed(batch))
        for message in chronological:
            text = message.raw_text or ""
            if GROUP_DONE_TEXT in text:
                return message
            if GROUP_PROGRESS_TEXT in text and message.buttons:
                return message
    return None


def _collect_media(messages: Iterable[Message]) -> List[Message]:
    media: List[Message] = []
    for message in messages:
        if message.media is None:
            continue
        if isinstance(message.media, MessageMediaWebPage):
            continue
        media.append(message)
    return media


async def fetch_media_from_bot(
    client,
    bot,
    token: str,
    per_group_timeout: float = DEFAULT_PER_GROUP_TIMEOUT,
    max_groups: int = DEFAULT_MAX_GROUPS,
) -> List[Message]:
    """Return the full ordered list of media messages produced for `token`."""

    baseline = await client.get_messages(bot, limit=1)
    since_id = baseline[0].id if baseline else 0

    await client.send_message(bot, f"/start getFile_{token}")
    log.info("dispatched getFile_%s to bot (since_id=%s)", token, since_id)

    all_media: List[Message] = []

    for group_index in range(max_groups):
        status = await _wait_for_status(client, bot, since_id, per_group_timeout)
        if status is None:
            raise RuntimeError(
                f"timed out waiting for group {group_index + 1} of token {token}"
            )

        # Fetch everything between the baseline and the status message inclusive
        window = await client.get_messages(bot, min_id=since_id, limit=200)
        window = list(reversed(window))
        group_slice = [m for m in window if m.id <= status.id]
        all_media.extend(_collect_media(group_slice))

        text = status.raw_text or ""
        if GROUP_DONE_TEXT in text:
            log.info("token %s finished, %d media items", token, len(all_media))
            return all_media

        # Find the "next group" callback button
        has_next = False
        if status.buttons:
            for row in status.buttons:
                for button in row:
                    button_text = button.text or ""
                    if NEXT_GROUP_TEXT in button_text and getattr(button, "data", None):
                        has_next = True
                        break
                if has_next:
                    break

        if not has_next:
            raise RuntimeError(
                f"token {token}: no next-group button after group {group_index + 1}"
            )

        since_id = status.id
        try:
            await status.click(
                filter=lambda button: (
                    button.text
                    and NEXT_GROUP_TEXT in button.text
                    and getattr(button, "data", None)
                )
            )
        except Exception as error:
            raise RuntimeError(
                f"failed to click next-group button for token {token}"
            ) from error

    raise RuntimeError(f"token {token} hit max_groups={max_groups}")
