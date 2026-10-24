from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
WATCH_DIR = ROOT / "source" / "watch115"
ARCHIVE_DIR = ROOT / "source" / "archive"
if not WATCH_DIR.is_dir():
    WATCH_DIR = ROOT / "watch115"
if not ARCHIVE_DIR.is_dir():
    ARCHIVE_DIR = ROOT / "archive"
sys.path.insert(0, str(WATCH_DIR))
sys.path.insert(0, str(ARCHIVE_DIR))

try:
    import telethon  # noqa: F401
except ModuleNotFoundError:
    telethon_stub = types.ModuleType("telethon")
    telethon_stub.TelegramClient = object
    telethon_stub.utils = types.SimpleNamespace(get_peer_id=lambda entity: entity.id)
    sys.modules["telethon"] = telethon_stub

try:
    import archiver  # noqa: F401
except ImportError:
    archive_stub = types.ModuleType("archiver")
    archive_stub.is_media = lambda message: message.media is not None
    archive_stub.media_size = lambda message: message.size
    archive_stub.media_kind = lambda message: message.kind

    def merge_continuations(groups):
        for group in groups:
            group["last_id"] = group["messages"][-1].id if group["messages"] else group["first_id"]
            group["last_album_size"] = len(group["messages"])
            group["merged_keys"] = [group["key"]]
        return groups

    archive_stub.merge_continuations = merge_continuations
    sys.modules["archiver"] = archive_stub

import watch115
from batch_manifest import load_manifests, write_manifest


class Watch115Tests(unittest.TestCase):
    def test_legacy_manifest_without_status_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "old.json").write_text(
                '{"id":"old","target_chat_id":-1001,"message_ids":[1]}',
                encoding="utf-8",
            )
            self.assertEqual(load_manifests(directory)[0]["status"], "complete")

    def test_pending_manifest_creates_source_barrier(self):
        old_loader = watch115.load_manifests
        watch115.load_manifests = lambda: [
            {
                "id": "pending",
                "status": "pending",
                "target_chat_id": -1001,
                "expected_count": 4,
                "message_ids": [],
            }
        ]
        try:
            self.assertEqual(watch115.source_manifest_barrier(-1001), (True, False))
        finally:
            watch115.load_manifests = old_loader

    def test_manifest_groups_exact_ids_across_senders(self):
        now = datetime.now(timezone.utc) - timedelta(minutes=2)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_manifest(
                {
                    "id": "batch-1",
                    "target_chat_id": -1001,
                    "title": "four videos",
                    "expected_count": 4,
                    "message_ids": [21, 22, 23, 24],
                },
                directory,
            )
            messages = [
                SimpleNamespace(
                    id=message_id,
                    date=now,
                    sender_id=sender_id,
                    media=object(),
                    kind="video",
                    size=100,
                )
                for message_id, sender_id in zip(
                    range(21, 25), [1, 2, 1, 2]
                )
            ]

            class Client:
                async def get_messages(self, _entity, ids):
                    return [item for item in messages if item.id in ids]

            old_loader = watch115.load_manifests
            watch115.load_manifests = lambda: [
                {
                    "id": "batch-1",
                    "target_chat_id": -1001,
                    "title": "four videos",
                    "expected_count": 4,
                    "message_ids": [21, 22, 23, 24],
                    "_path": str(directory / "batch-1.json"),
                }
            ]
            try:
                groups, consumed = asyncio.run(
                    watch115.load_manifest_groups(Client(), object(), -1001)
                )
            finally:
                watch115.load_manifests = old_loader
            self.assertEqual(len(groups), 1)
            self.assertEqual([item.id for item in groups[0]["messages"]], [21, 22, 23, 24])
            self.assertEqual(groups[0]["expected_count"], 4)
            self.assertEqual(consumed, {21, 22, 23, 24})

    def test_manifest_waits_for_all_expected_messages(self):
        now = datetime.now(timezone.utc) - timedelta(minutes=2)

        class Client:
            async def get_messages(self, _entity, ids):
                if ids != [31, 32]:
                    raise AssertionError(ids)
                return [
                    SimpleNamespace(
                        id=31,
                        date=now,
                        sender_id=1,
                        media=object(),
                        kind="video",
                        size=100,
                    )
                ]

        old_loader = watch115.load_manifests
        watch115.load_manifests = lambda: [
            {
                "id": "batch-incomplete",
                "target_chat_id": -1001,
                "title": "two videos",
                "expected_count": 2,
                "message_ids": [31, 32],
            }
        ]
        try:
            groups, _consumed = asyncio.run(
                watch115.load_manifest_groups(Client(), object(), -1001)
            )
        finally:
            watch115.load_manifests = old_loader
        self.assertEqual(len(groups[0]["messages"]), 1)
        self.assertFalse(watch115.group_is_settled(groups[0], now=now))

    def test_scan_pending_manifest_does_not_fetch_ordinary_messages(self):
        calls = []

        class Client:
            async def get_entity(self, source_id):
                calls.append(("entity", source_id))
                return object()

            async def get_messages(self, _entity, **kwargs):
                calls.append(("messages", kwargs))
                return []

        service = watch115.Watch115Service.__new__(watch115.Watch115Service)
        service.client = Client()
        service.state = {
            "sources": {"-1001": {"cursor": 10}},
            "dirs": {},
            "service": {},
        }
        service.persist = lambda: None
        service.notify = mock.AsyncMock()
        old_loader = watch115.load_manifests
        watch115.load_manifests = lambda: [
            {
                "id": "pending",
                "status": "pending",
                "target_chat_id": -1001,
                "expected_count": 4,
                "message_ids": [],
            }
        ]
        try:
            asyncio.run(
                service.scan_source(
                    {"id": -1001, "title": "target"}, {}, {1}
                )
            )
        finally:
            watch115.load_manifests = old_loader
        self.assertFalse(any("min_id" in kwargs for kind, kwargs in calls if kind == "messages"))

    def test_empty_after_manifest_filter_returns_without_max_error(self):
        now = datetime.now(timezone.utc) - timedelta(minutes=2)
        message = SimpleNamespace(
            id=21,
            date=now,
            sender_id=1,
            media=object(),
            kind="video",
            size=100,
            grouped_id=None,
            raw_text="title",
        )

        class Client:
            async def get_entity(self, _source_id):
                return object()

            async def get_messages(self, _entity, **kwargs):
                if "ids" in kwargs:
                    return [message]
                return [message]

        service = watch115.Watch115Service.__new__(watch115.Watch115Service)
        service.client = Client()
        service.state = {
            "sources": {"-1001": {"cursor": 20}},
            "dirs": {},
            "service": {},
        }
        service.persist = lambda: None
        service.notify = mock.AsyncMock()
        service.process_group = mock.AsyncMock()
        old_loader = watch115.load_manifests
        watch115.load_manifests = lambda: [
            {
                "id": "complete",
                "status": "complete",
                "target_chat_id": -1001,
                "title": "one",
                "expected_count": 1,
                "message_ids": [21],
            }
        ]
        try:
            asyncio.run(
                service.scan_source(
                    {"id": -1001, "title": "target"}, {}, {999}
                )
            )
        finally:
            watch115.load_manifests = old_loader
        self.assertNotIn("last_error", service.state["sources"]["-1001"])
        service.process_group.assert_not_awaited()

    def test_parse_properties(self):
        values = watch115.parse_properties(
            """
            # comment
            bot.token = token-value
            bot.apiId: 12345
            ignored line
            """
        )
        self.assertEqual(values["bot.token"], "token-value")
        self.assertEqual(values["bot.apiId"], "12345")

    def test_read_115_settings_from_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "115bot.db"
            connection = sqlite3.connect(path)
            connection.execute('CREATE TABLE config ("key" TEXT, "value" TEXT)')
            connection.executemany(
                'INSERT INTO config ("key", "value") VALUES (?, ?)',
                [
                    ("account.cookie", "UID=secret"),
                    ("account.userAgent", "test-agent"),
                    ("account.transfercid", "789"),
                ],
            )
            connection.commit()
            connection.close()
            settings = watch115.read_115_settings(
                {"transfer_cid": ""},
                path,
            )
            self.assertEqual(settings["cookie"], "UID=secret")
            self.assertEqual(settings["user_agent"], "test-agent")
            self.assertEqual(settings["transfer_cid"], "789")

    def test_local_cid_overrides_official_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "115bot.db"
            connection = sqlite3.connect(path)
            connection.execute('CREATE TABLE config ("key" TEXT, "value" TEXT)')
            connection.execute(
                'INSERT INTO config ("key", "value") VALUES (?, ?)',
                ("account.transfercid", "789"),
            )
            connection.commit()
            connection.close()
            settings = watch115.read_115_settings(
                {"transfer_cid": "456"},
                path,
            )
            self.assertEqual(settings["transfer_cid"], "456")
            self.assertEqual(settings["cid_source"], "增强模块")

    def test_command_parsing(self):
        self.assertEqual(
            watch115.parse_command("/watch_add@tgto115_115bot @source"),
            ("/watch_add", "@source"),
        )
        self.assertEqual(
            watch115.parse_command("指定用户"),
            ("/watch_users", ""),
        )
        self.assertEqual(watch115.parse_command("hello"), ("", ""))

    def test_unified_bot_menu_registers_forwarder_commands(self):
        commands = {row["command"] for row in watch115.WATCH_COMMANDS}
        self.assertIn("forwarder_menu", commands)
        self.assertIn("forwarder_list", commands)
        keyboard = watch115.menu_keyboard()["keyboard"]
        self.assertIn(["来源管理", "已监听来源"], keyboard)
        self.assertIn(["目标群", "设置目标群"], keyboard)

    def test_forwarder_inputs_are_not_claimed_by_pending_watch_action(self):
        service = watch115.Watch115Service.__new__(watch115.Watch115Service)
        service.menu_state = "/watch_user_add"
        self.assertFalse(asyncio.run(service.handle_command("来源管理")))
        self.assertFalse(asyncio.run(service.handle_command("/forwarder_menu")))

    def test_target_normalization(self):
        self.assertEqual(
            watch115.normalize_target("https://t.me/c/123456/77"),
            "-100123456",
        )
        self.assertEqual(watch115.normalize_target("t.me/example_chan"), "@example_chan")
        self.assertEqual(watch115.normalize_target("-100123"), "-100123")

    def test_visual_media_filter(self):
        with mock.patch.object(watch115.archive, "is_media", return_value=True), mock.patch.object(
            watch115.archive,
            "media_kind",
            side_effect=lambda message: message.kind,
        ):
            self.assertTrue(watch115.is_visual_media(SimpleNamespace(kind="video")))
            self.assertTrue(watch115.is_visual_media(SimpleNamespace(kind="photo")))
            self.assertTrue(watch115.is_visual_media(SimpleNamespace(kind="audio")))
            self.assertFalse(watch115.is_visual_media(SimpleNamespace(kind="other")))

    def test_grouping_keeps_albums_together(self):
        now = datetime.now(timezone.utc) - timedelta(minutes=2)
        messages = [
            SimpleNamespace(id=10, grouped_id=99, raw_text="title", sender_id=1, date=now),
            SimpleNamespace(id=11, grouped_id=99, raw_text="", sender_id=1, date=now),
            SimpleNamespace(id=12, grouped_id=None, raw_text="single", sender_id=1, date=now),
        ]
        groups = watch115.build_groups(
            messages,
            -1001,
            media_predicate=lambda _message: True,
            size_func=lambda _message: 100,
        )
        self.assertEqual(len(groups), 2)
        self.assertEqual([message.id for message in groups[0]["messages"]], [10, 11])
        self.assertEqual(groups[0]["size"], 200)
        self.assertEqual(groups[1]["key"], "-1001:s:12")


if __name__ == "__main__":
    unittest.main()
