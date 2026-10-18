"""Offline regression tests for the music poller sharing the news container."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.music import parse_music_listing, parse_release, music_content_signature
from scripts.local_official_news_poller import (
    OfficialNewsPoller,
    PollerConfig,
    PollerConfigError,
    build_config,
    parse_args,
)
from scripts.music_poll_worker import (
    MUSIC_STATE_VERSION,
    MusicPoller,
    default_music_state,
    load_music_state,
    music_row_to_api,
    public_music_cover,
    save_music_state,
)


def music_config(**overrides) -> PollerConfig:
    with patch.dict(
        os.environ,
        {
            "NIJIDB_BASE_URL": "https://nijidb.example.test",
            "NIJIDB_INGEST_API_KEY": "news-key",
            "NIJIDB_MUSIC_POLLER_API_KEY": "music-key-that-is-at-least-32-characters-long",
            "NEWS_POLL_PROXY": "http://192.168.10.75:7890",
            "R2_ENDPOINT": "https://account.r2.example.test",
            "R2_BUCKET": "nijidb",
            "R2_ACCESS_KEY_ID": "access-key",
            "R2_SECRET_ACCESS_KEY": "secret-key",
            "R2_PUBLIC_BASE_URL": "https://images.example.test",
            "NEWS_POLL_STATE_FILE": str(overrides.get("state_file", "/tmp/news-state.json")),
            "MUSIC_POLL_STATE_FILE": str(overrides.get("music_state_file", "/tmp/music-state.json")),
            "MUSIC_POLL_ENABLED": "1",
            "ONEBOT_URL": "http://bot.example.test:3000",
            "ONEBOT_TOKEN": "test-token",
            "ONEBOT_TARGET": "group:12345",
            "ONEBOT_PROFILE": "Nijidb",
        },
        clear=True,
    ):
        config = build_config(parse_args(["--dry-run"]))
    values = {"dry_run": False, "music_article_delay": 0, "music_detail_delay": 0, **overrides}
    return replace(config, **values)


def music_record(record_id="cd01_9999"):
    html = f"""
    <div class="box" id="{record_id}">
      <div class="cover"><img src="/images/cover.jpg" alt="cover"></div>
      <h3 class="title">虹咲音乐发行</h3>
      <div class="spec">
        <dl><dt>アーティスト</dt><dd>虹ヶ咲学園スクールアイドル同好会</dd></dl>
        <ul class="track"><li>曲目测试<dl><dt>作词</dt><dd>作词者</dd></dl></li></ul>
      </div>
    </div>
    """
    from bs4 import BeautifulSoup

    return parse_release(record_id, BeautifulSoup(html, "html.parser"))


class MusicParserTests(unittest.TestCase):
    def test_listing_collects_deferred_ids_once(self):
        entries, missing = parse_music_listing("""
        <ul class="list">
          <li><a href="#cd01">one</a></li><li><a href="#cd02">two</a></li>
          <li><a href="#cd02">duplicate</a></li><li><a href="https://example.test">ignore</a></li>
        </ul>
        <div class="box" id="cd01"><h3 class="title">ready</h3></div>
        """)
        self.assertEqual([item[0] for item in entries], ["cd01", "cd02"])
        self.assertEqual(missing, ["cd02"])

    def test_row_to_api_transforms_database_json_fields(self):
        row = {
            "id": "cd01",
            "title": "title",
            "tracks_json": '[{"number":1,"title":"song"}]',
            "spec_json": '{"品番":"x"}',
            "extras_json": '[{"type":"notice"}]',
        }
        payload = music_row_to_api(row)
        self.assertEqual(payload["tracks"][0]["title"], "song")
        self.assertEqual(payload["specs"]["品番"], "x")
        self.assertEqual(payload["extras"][0]["type"], "notice")
        self.assertNotIn("tracks_json", payload)

    def test_public_cover_recognizes_historical_release_cover_names(self):
        config = music_config()
        self.assertTrue(public_music_cover("https://images.example.test/images/cd01.jpg", config, "cd01"))
        self.assertTrue(public_music_cover("https://images.example.test/images/cd01-a91747769671.jpg", config, "cd01"))
        self.assertTrue(public_music_cover("https://images.example.test/images/music/cd01.jpg", config, "cd01"))
        self.assertFalse(public_music_cover("https://other.example.test/images/cd01.jpg", config, "cd01"))
        self.assertFalse(public_music_cover("https://images.example.test/images/collabo/a.jpg", config, "cd01"))
        self.assertFalse(public_music_cover("https://images.example.test/images/news/a.jpg", config, "cd01"))


class MusicPollerTests(unittest.IsolatedAsyncioTestCase):
    def make_poller(self, directory, **overrides):
        config = music_config(
            music_state_file=Path(directory) / "music-state.json", **overrides
        )
        with patch("scripts.music_poll_worker.boto3.client", return_value=MagicMock()):
            poller = MusicPoller(config)
        return poller

    async def test_run_pass_with_real_config_completes_and_marks_initialized(self):
        poller = self.make_poller(tempfile.mkdtemp())
        poller.api_json = AsyncMock(return_value={"version": 1, "sent": 0, "pending": 0})
        poller.load_site_releases = AsyncMock(return_value={})
        poller.get_catalog = AsyncMock(return_value=([], {}, set()))
        result = await poller.run_pass()
        self.assertEqual(result["inspected"], 0)
        self.assertTrue(poller.state["initialized"])
        self.assertEqual(poller.state["stats"]["passes"], 1)
        self.assertTrue(poller.state_file.exists())

    async def test_run_pass_releases_shared_lock_between_official_requests(self):
        poller = self.make_poller(tempfile.mkdtemp())
        lock = asyncio.Lock()
        observed = []

        async def catalog(official, cycle_lock=None):
            observed.append(lock.locked())
            return [], {}, set()

        poller.get_catalog = catalog
        poller.load_site_releases = AsyncMock(return_value={})
        poller.api_json = AsyncMock(return_value={"version": 1})
        await poller.run_pass(lock)
        self.assertEqual(observed, [False])
        self.assertFalse(lock.locked())

    async def test_existing_r2_catalog_is_seeded_then_skipped(self):
        record = music_record()
        poller = self.make_poller(tempfile.mkdtemp())
        previous = {"id": record["id"], "cover_url": "https://images.example.test/images/cd01_9999-a91747769671.jpg"}
        poller.fetch_cover = AsyncMock(return_value=(None, {"cover_sha256": "cover-hash"}))
        result = await poller.process_release(None, None, record, previous)
        self.assertEqual(result, "seeded")
        self.assertEqual(poller.state["releases"][record["id"]]["cover_url"], previous["cover_url"])
        poller.fetch_cover.assert_awaited_once()
        poller.api_json = AsyncMock()
        self.assertEqual(await poller.process_release(None, None, record, previous), "skipped")
        poller.api_json.assert_not_awaited()

    async def test_metadata_update_reuses_cover_and_content_change_replaces_it(self):
        record = music_record()
        poller = self.make_poller(tempfile.mkdtemp())
        poller.state["initialized"] = True
        cover = "https://images.example.test/images/music/original.png"
        poller.state["releases"][record["id"]] = {
            "source_signature": music_content_signature(record),
            "cover_source_url": record["cover_url"], "cover_url": cover,
            "cover_sha256": "old-hash",
        }
        poller.fetch_cover = AsyncMock(return_value=(None, {"cover_sha256": "old-hash"}))
        poller.api_json = AsyncMock(return_value={"id": record["id"], "changed": True})
        updated = {**record, "price": "4400円"}
        poller.archive_cover = MagicMock(return_value="https://images.example.test/images/music/new.png")
        self.assertEqual(await poller.process_release(None, None, updated, {"id": record["id"], "cover_url": cover}), "changed")
        self.assertEqual(poller.api_json.await_args.kwargs["json"]["release"]["cover_url"], cover)

    def test_r2_archive_uses_sha256_key_and_skips_existing_content(self):
        poller = self.make_poller(tempfile.mkdtemp())
        data = b"\x89PNG\r\n\x1a\n" + b"x" * 32
        poller.validate_image = MagicMock(return_value=(".png", "image/png"))
        poller.s3.head_object.return_value = {"ContentLength": len(data)}
        url = poller.archive_cover(data)
        self.assertIn(hashlib.sha256(data).hexdigest(), url)
        poller.s3.put_object.assert_not_called()

    async def test_failed_baseline_is_not_checkpointed_as_complete(self):
        record = music_record()
        poller = self.make_poller(tempfile.mkdtemp())
        poller.fetch_cover = AsyncMock(side_effect=httpx.ConnectError("offline"))
        with self.assertRaises(httpx.ConnectError):
            await poller.process_release(None, None, record, {"id": record["id"], "cover_url": "https://images.example.test/images/cd01_9999.jpg"})
        self.assertNotIn(record["id"], poller.state["releases"])

    async def test_unexpected_music_failure_does_not_stop_news(self):
        with tempfile.TemporaryDirectory() as directory:
            config = music_config(
                once=True,
                dry_run=True,
                state_file=Path(directory) / "news.json",
                music_state_file=Path(directory) / "music.json",
            )
        news = OfficialNewsPoller(config)
        news.run_pass = AsyncMock(
            return_value={"handled": 0, "stats": news.state["stats"]}
        )

        class BrokenMusic:
            state_file = config.music_state_file
            s3 = None

            async def run(self, *args, **kwargs):
                raise AttributeError("simulated defect")

        with patch(
            "scripts.music_poll_worker.MusicPoller", return_value=BrokenMusic()
        ):
            self.assertEqual(await news.run(), 1)
        news.run_pass.assert_awaited_once()

    async def test_news_only_config_does_not_require_music_key_or_construct_worker(self):
        with patch.dict(os.environ, {}, clear=True):
            config = build_config(parse_args(["--dry-run", "--once"]))
        self.assertFalse(config.music_enabled)
        poller = OfficialNewsPoller(config)
        poller.run_pass = AsyncMock(return_value={"handled": 0, "stats": poller.state["stats"]})
        with patch("scripts.music_poll_worker.MusicPoller") as worker:
            await poller.run()
        worker.assert_not_called()

    async def test_music_and_news_share_lock_and_finish_once_together(self):
        with tempfile.TemporaryDirectory() as directory:
            config = music_config(once=True, dry_run=True, state_file=Path(directory) / "news.json", music_state_file=Path(directory) / "music.json")
            await self._run_shared_worker(config)

    async def _run_shared_worker(self, config):
        news = OfficialNewsPoller(config)
        news.run_pass = AsyncMock(return_value={"handled": 0, "stats": news.state["stats"]})
        finished = []
        async def fake_music_run(stop_event, cycle_lock, state_lock, sleep_fn):
            await asyncio.sleep(0.01)
            self.assertIs(cycle_lock, news.official_cycle_lock)
            self.assertIsNotNone(state_lock)
            finished.append(True)
            return 0
        worker = MagicMock()
        worker.state_file = config.music_state_file
        worker.s3 = None
        worker.run = fake_music_run
        with patch("scripts.music_poll_worker.MusicPoller", return_value=worker):
            self.assertEqual(await news.run(), 0)
        self.assertEqual(finished, [True])
        news.run_pass.assert_awaited_once()


class MusicStateTests(unittest.TestCase):
    def test_every_config_attribute_used_by_music_exists(self):
        source = Path("scripts/music_poll_worker.py").read_text(encoding="utf-8")
        names = set(re.findall(r"(?:self|config)\.config\.([a-z_][a-z0-9_]*)", source))
        names |= set(re.findall(r"\bconfig\.([a-z_][a-z0-9_]*)", source))
        config = music_config()
        missing = sorted(name for name in names if not hasattr(config, name))
        self.assertEqual(missing, [], f"unknown PollerConfig attributes: {missing}")

    def test_corrupt_state_is_not_reinitialized_over_existing_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "music.json"
            path.write_text("corrupt")
            with self.assertRaises(PollerConfigError):
                load_music_state(path)
            self.assertEqual(path.read_text(), "corrupt")

    def test_music_state_is_atomic_and_rejects_news_path_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "music.json"
            save_music_state(path, default_music_state())
            self.assertEqual(load_music_state(path)["version"], MUSIC_STATE_VERSION)
        with patch.dict(os.environ, {"MUSIC_POLL_ENABLED": "1", "NIJIDB_MUSIC_POLLER_API_KEY": "x", "NIJIDB_BASE_URL": "https://nijidb.example.test", "NIJIDB_INGEST_API_KEY": "news-key", "NEWS_POLL_PROXY": "http://192.168.10.75:7890", "R2_ENDPOINT": "https://r2.example.test", "R2_BUCKET": "nijidb", "R2_ACCESS_KEY_ID": "a", "R2_SECRET_ACCESS_KEY": "b", "R2_PUBLIC_BASE_URL": "https://images.example.test", "NEWS_POLL_STATE_FILE": "/tmp/shared", "MUSIC_POLL_STATE_FILE": "/tmp/shared"}, clear=True):
            with self.assertRaises(PollerConfigError):
                build_config(parse_args(["--dry-run"]))


if __name__ == "__main__":
    unittest.main()
