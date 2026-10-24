"""Shared forward-batch manifests for the forwarder and watch115."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def manifest_dir() -> Path:
    base = Path(os.environ.get("FORWARDER_DATA_DIR", "/data/forwarder"))
    return Path(os.environ.get("FORWARD_BATCH_DIR", str(base / "batches")))


def _name(batch_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(batch_id)).strip("._")
    return (safe or "batch")[:180] + ".json"


def write_manifest(payload: dict, directory: Path | None = None) -> Path:
    directory = directory or manifest_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / _name(str(payload["id"]))
    data = dict(payload)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(target)
    return target


def load_manifests(directory: Path | None = None) -> list[dict]:
    directory = directory or manifest_dir()
    rows: list[dict] = []
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return rows
    for path in paths:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(row, dict) or not row.get("id"):
                continue
            row["_path"] = str(path)
            # Manifests created before the state machine was introduced were
            # written only after a successful send, so they are complete.
            row["status"] = str(row.get("status") or "complete").lower()
            row["expected_count"] = int(row.get("expected_count") or 0)
            row["message_ids"] = [int(value) for value in row.get("message_ids") or []]
            rows.append(row)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return rows
