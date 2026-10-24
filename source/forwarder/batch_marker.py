"""Backward-compatible parser for batches created by older deployments."""

from __future__ import annotations

import base64
import json

PREFIX = "\u2063\u2063"


def make_marker(batch_id: str, count: int, title: str = "") -> str:
    payload = {"id": str(batch_id), "count": int(count), "title": str(title or "")}
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return PREFIX + encoded + PREFIX


def parse_marker(text: str) -> dict | None:
    text = text or ""
    if not text.startswith(PREFIX) or not text.endswith(PREFIX):
        return None
    encoded = text[len(PREFIX) : -len(PREFIX)]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        value = json.loads(raw.decode())
        if not isinstance(value, dict):
            return None
        return {
            "id": str(value["id"]),
            "count": int(value["count"]),
            "title": str(value.get("title") or ""),
        }
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return None
