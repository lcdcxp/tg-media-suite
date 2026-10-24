from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
FORWARDER_DIR = ROOT / "source" / "forwarder"
if not FORWARDER_DIR.is_dir():
    FORWARDER_DIR = ROOT / "forwarder"
sys.path.insert(0, str(FORWARDER_DIR))

try:
    import telethon  # noqa: F401
except ModuleNotFoundError:
    telethon_module = types.ModuleType("telethon")
    telethon_module.TelegramClient = object
    telethon_module.events = SimpleNamespace(NewMessage=lambda **_kwargs: object())
    telethon_module.utils = SimpleNamespace(get_peer_id=lambda entity: entity.id)
    tl_module = types.ModuleType("telethon.tl")
    custom_module = types.ModuleType("telethon.tl.custom")
    message_module = types.ModuleType("telethon.tl.custom.message")
    types_module = types.ModuleType("telethon.tl.types")

    class Message:
        pass

    class DocumentAttributeFilename:
        pass

    class MessageMediaWebPage:
        pass

    message_module.Message = Message
    types_module.DocumentAttributeFilename = DocumentAttributeFilename
    types_module.MessageMediaWebPage = MessageMediaWebPage
    sys.modules.update(
        {
            "telethon": telethon_module,
            "telethon.tl": tl_module,
            "telethon.tl.custom": custom_module,
            "telethon.tl.custom.message": message_module,
            "telethon.tl.types": types_module,
        }
    )

try:
    import bot_sender  # noqa: F401
except ModuleNotFoundError:
    sender_stub = types.ModuleType("bot_sender")

    async def send_media_batches(*_args, **_kwargs):
        return None

    sender_stub.send_media_batches = send_media_batches
    sys.modules["bot_sender"] = sender_stub

import manual_forwarder


class ForwarderSourceTests(unittest.TestCase):
    def test_any_oversized_media_selects_mtproto_for_the_batch(self):
        messages = [
            SimpleNamespace(document=SimpleNamespace(size=1)),
            SimpleNamespace(
                document=SimpleNamespace(size=manual_forwarder.MAX_BOT_UPLOAD_BYTES + 1)
            ),
        ]
        self.assertTrue(manual_forwarder.batch_requires_mtproto(messages))

    def test_mtproto_chunks_keep_compatible_types_and_album_limit(self):
        paths = [f"video-{index}.mp4" for index in range(11)] + ["track.mp3"]
        self.assertEqual(
            [len(chunk) for chunk in manual_forwarder.mtproto_media_chunks(paths)],
            [10, 1, 1],
        )

    def test_oversized_batch_uses_only_mtproto_and_completes_manifest(self):
        statuses = []
        media = []
        for message_id, size in [(1, 10), (2, manual_forwarder.MAX_BOT_UPLOAD_BYTES + 1)]:
            async def download_media(file, current_id=message_id, **_kwargs):
                path = Path(file)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"video")
                return str(path)

            media.append(
                SimpleNamespace(
                    id=message_id,
                    grouped_id=99,
                    document=SimpleNamespace(size=size),
                    file=SimpleNamespace(name=f"{message_id}.mp4"),
                    download_media=download_media,
                )
            )

        class Client:
            async def get_messages(self, _bot, ids):
                return [item for item in media if item.id in ids]

            async def get_entity(self, chat_id):
                return chat_id

            async def send_file(self, _target, paths, **_kwargs):
                rows = paths if isinstance(paths, list) else [paths]
                return [SimpleNamespace(id=500 + index) for index, _ in enumerate(rows, 1)]

            async def edit_message(self, *_args):
                return None

        def write(payload):
            statuses.append((payload["status"], list(payload["message_ids"])))
            return Path("manifest.json")

        batch = manual_forwarder.ResolvedBatch(
            start_id=1, token="batch", media=media, caption="title", complete=True
        )
        with mock.patch.dict(
            os.environ,
            {"TG_BOT_TOKEN": "token", "TG_BOT_CHAT_ID": "-1001"},
        ), mock.patch.object(manual_forwarder, "write_manifest", side_effect=write), mock.patch.object(
            manual_forwarder, "load_manifests", return_value=[]
        ), mock.patch.object(
            manual_forwarder, "send_media_batches", new=mock.AsyncMock()
        ) as bot_send, mock.patch.object(
            manual_forwarder, "load_processed", return_value=set()
        ), mock.patch.object(
            manual_forwarder, "save_processed"
        ):
            asyncio.run(
                manual_forwarder.deliver_batch(
                    Client(), SimpleNamespace(id=7), SimpleNamespace(id=9), batch
                )
            )
        self.assertEqual(statuses[0], ("pending", []))
        self.assertEqual(statuses[-1], ("complete", [501, 502]))
        bot_send.assert_not_awaited()

    def test_failed_batch_retains_manifest_and_is_not_marked_processed(self):
        statuses = []
        media = []
        for message_id, suffix in [(1, ".mp4"), (2, ".mp3")]:
            async def download_media(file, current_id=message_id, current_suffix=suffix, **_kwargs):
                path = Path(file)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((str(current_id) + current_suffix).encode())
                return str(path)

            media.append(
                SimpleNamespace(
                    id=message_id,
                    grouped_id=99,
                    document=SimpleNamespace(
                        size=manual_forwarder.MAX_BOT_UPLOAD_BYTES + 1
                        if message_id == 1
                        else 10
                    ),
                    file=SimpleNamespace(name=f"{message_id}{suffix}"),
                    download_media=download_media,
                )
            )

        class Client:
            calls = 0

            async def get_messages(self, _bot, ids):
                return [item for item in media if item.id in ids]

            async def get_entity(self, chat_id):
                return chat_id

            async def send_file(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("upload failed")
                return SimpleNamespace(id=601)

            async def edit_message(self, *_args):
                return None

        def write(payload):
            statuses.append((payload["status"], list(payload["message_ids"])))
            return Path("manifest.json")

        batch = manual_forwarder.ResolvedBatch(
            start_id=1, token="batch", media=media, caption="title", complete=True
        )
        with mock.patch.dict(
            os.environ,
            {"TG_BOT_TOKEN": "token", "TG_BOT_CHAT_ID": "-1001"},
        ), mock.patch.object(manual_forwarder, "write_manifest", side_effect=write), mock.patch.object(
            manual_forwarder, "load_manifests", return_value=[]
        ), mock.patch.object(
            manual_forwarder, "load_processed", return_value=set()
        ), mock.patch.object(
            manual_forwarder, "save_processed"
        ) as save_processed:
            asyncio.run(
                manual_forwarder.deliver_batch(
                    Client(), SimpleNamespace(id=7), SimpleNamespace(id=9), batch
                )
            )
        self.assertEqual(statuses[-1], ("failed", [601]))
        save_processed.assert_not_called()

    def test_management_bot_token_is_read_from_official_properties(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "application.properties"
            path.write_text(
                "# official bot\nbot.token = official-token\n",
                encoding="utf-8",
            )
            self.assertEqual(
                manual_forwarder.read_management_bot_token(path),
                "official-token",
            )

    def test_forwarder_uses_the_unified_management_keyboard(self):
        self.assertIn(
            ("来源管理", "已监听来源"),
            manual_forwarder.UNIFIED_KEYBOARD_ROWS,
        )
        self.assertIn(
            ("目标群", "设置目标群"),
            manual_forwarder.UNIFIED_KEYBOARD_ROWS,
        )

    def test_normalize_source_accepts_channel_id_and_links(self):
        self.assertEqual(manual_forwarder.normalize_username("-100123"), "-100123")
        self.assertEqual(
            manual_forwarder.normalize_username("https://t.me/c/456789/20"),
            "-100456789",
        )
        self.assertEqual(
            manual_forwarder.normalize_username("t.me/public_channel"),
            "@public_channel",
        )

    def test_processed_keys_are_scoped_by_source(self):
        values = {
            manual_forwarder.processed_key(100, 55),
            manual_forwarder.processed_key(200, 55),
        }
        self.assertTrue(manual_forwarder.was_processed(values, 100, 55))
        self.assertTrue(manual_forwarder.was_processed(values, 200, 55))
        self.assertFalse(manual_forwarder.was_processed(values, 300, 55))

    def test_processed_state_migrates_legacy_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = manual_forwarder.PROCESSED_PATH
            path = Path(tmp) / "processed.json"
            path.write_text(
                json.dumps({"completed_start_ids": [77]}),
                encoding="utf-8",
            )
            manual_forwarder.PROCESSED_PATH = path
            try:
                values = manual_forwarder.load_processed()
                self.assertTrue(manual_forwarder.was_processed(values, 999, 77))
                values.add(manual_forwarder.processed_key(999, 88))
                manual_forwarder.save_processed(values)
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("999:88", payload["completed_keys"])
                self.assertIn(77, payload["completed_start_ids"])
            finally:
                manual_forwarder.PROCESSED_PATH = old_path

    def test_direct_channel_album_is_resolved_as_one_batch(self):
        now = datetime.now(timezone.utc) - timedelta(minutes=2)
        messages = [
            SimpleNamespace(
                id=10,
                grouped_id=900,
                media=object(),
                raw_text="album title",
                date=now,
            ),
            SimpleNamespace(
                id=11,
                grouped_id=900,
                media=object(),
                raw_text="",
                date=now,
            ),
        ]

        class Client:
            async def get_messages(self, _source, ids):
                return messages

        batch = asyncio.run(
            manual_forwarder.resolve_direct_media_batch(
                Client(),
                SimpleNamespace(id=123),
                messages[1],
            )
        )
        self.assertIsNotNone(batch)
        self.assertEqual([message.id for message in batch.media], [10, 11])
        self.assertEqual(batch.caption, "album title")
        self.assertTrue(batch.complete)


if __name__ == "__main__":
    unittest.main()
