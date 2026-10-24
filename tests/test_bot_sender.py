from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest import mock

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
FORWARDER_DIR = ROOT / "source" / "forwarder"
if not FORWARDER_DIR.is_dir():
    FORWARDER_DIR = ROOT / "forwarder"
sys.path.insert(0, str(FORWARDER_DIR))

try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    httpx_stub = types.ModuleType("httpx")

    class AsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    httpx_stub.AsyncClient = AsyncClient
    sys.modules["httpx"] = httpx_stub

import bot_sender


class BotSenderTests(unittest.TestCase):
    def test_classifies_audio_formats_without_treating_them_as_photos(self):
        self.assertEqual(bot_sender._classify("cover.jpg"), "photo")
        self.assertEqual(bot_sender._classify("track.mp3"), "audio")
        self.assertEqual(bot_sender._classify("track.m4a"), "audio")
        self.assertEqual(bot_sender._classify("track.aac"), "document")
        self.assertEqual(bot_sender._classify("unknown.bin"), "document")

    def test_mixed_media_is_split_into_telegram_compatible_ordered_chunks(self):
        calls = []

        async def fake_send(_client, _token, _chat_id, paths, caption):
            calls.append((list(paths), caption))

        async def exercise():
            with mock.patch.object(bot_sender, "_send_chunk_resilient", new=fake_send):
                await bot_sender.send_media_batches(
                    "token",
                    123,
                    ["one.jpg", "track.mp3", "track.aac", "two.mp4", "three.jpg"],
                    "folder title",
                )

        asyncio.run(exercise())
        self.assertEqual(
            calls,
            [
                (["one.jpg"], "folder title"),
                (["track.mp3"], None),
                (["track.aac"], None),
                (["two.mp4", "three.jpg"], None),
            ],
        )

    def test_contiguous_mp3_files_can_share_an_audio_album(self):
        calls = []

        async def fake_send(_client, _token, _chat_id, paths, caption):
            calls.append((list(paths), caption))

        async def exercise():
            with mock.patch.object(bot_sender, "_send_chunk_resilient", new=fake_send):
                await bot_sender.send_media_batches(
                    "token", 123, ["one.mp3", "two.mp3"], None
                )

        asyncio.run(exercise())
        self.assertEqual(calls, [(["one.mp3", "two.mp3"], None)])


if __name__ == "__main__":
    unittest.main()
