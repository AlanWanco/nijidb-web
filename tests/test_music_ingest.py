"""Music-worker auth, idempotence and OneBot HTTP tests; all data and HTTP are isolated."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from tests import test_external_ingest as external

main = external.main


class MusicWorkerApiTests(unittest.IsolatedAsyncioTestCase):
    login = external.ExternalIngestApiTests.login
    music_payload = staticmethod(external.ExternalIngestApiTests.music_payload)

    async def asyncSetUp(self):
        await external.ExternalIngestApiTests.asyncSetUp(self)
        lock = patch.object(main, "music_notification_lock", asyncio.Lock())
        lock.start()
        self.addCleanup(lock.stop)
        environment = patch.dict(
            os.environ,
            {
                "NIJIDB_MUSIC_POLLER_API_KEY": "worker-test",
                "NIJIDB_MUSIC_INGEST_API_KEY": "legacy-test",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        main.save_settings(
            {
                "onebot_url": "https://bot.example.test",
                "onebot_target": "group:12345",
                "onebot_token": "server-test-token",
            }
        )
        self.headers = {"X-Nijidb-API-Key": "worker-test"}

    async def post(self, payload=None, headers=None):
        return await self.client.post(
            "/api/ingest/music-worker",
            json=payload or self.music_payload(),
            headers=headers or self.headers,
        )

    async def test_unconfigured_worker_does_not_register_request_key(self):
        with patch.dict(os.environ, {"NIJIDB_MUSIC_POLLER_API_KEY": ""}):
            response = await self.post(
                headers={
                    "X-Nijidb-API-Key": "arbitrary-untrusted-key-over-32-characters"
                }
            )
        self.assertEqual(response.status_code, 503)
        with main.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM external_api_keys").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0], 0
            )

    async def test_worker_key_generation_preserves_legacy_music_key_and_access(self):
        self.login()
        old = (await self.client.post("/api/admin/external-api-keys/music")).json()[
            "key"
        ]
        before = main.external_api_key_record("music")
        new = (
            await self.client.post("/api/admin/external-api-keys/music_worker")
        ).json()["key"]
        self.assertEqual(before, main.external_api_key_record("music"))
        self.assertNotEqual(old, new)
        self.assertEqual(
            (await self.post(headers={"X-Nijidb-API-Key": old})).status_code, 401
        )
        with patch.object(main, "notify", new_callable=AsyncMock) as notify:
            accepted = await self.post(headers={"X-Nijidb-API-Key": new})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        notify.assert_awaited_once()
        legacy = await self.client.post(
            "/api/ingest/music",
            json=self.music_payload(),
            headers={"X-Nijidb-API-Key": old},
        )
        self.assertEqual(legacy.status_code, 200)
        docs = (await self.client.get("/api/admin/external-api-docs")).json()
        for resource in docs["resources"]:
            self.assertIn(resource["id"], main.EXTERNAL_INGEST_KEY_CONFIG)
        status = await self.client.get(
            "/api/ingest/music-worker", headers={"X-Nijidb-API-Key": new}
        )
        self.assertEqual(status.json()["version"], 1)
        self.assertNotIn("server-test-token", status.text)
        settings = await self.client.get("/api/admin/settings")
        self.assertNotIn("server-test-token", settings.text)

    async def test_idempotent_update_detects_price_and_cover_and_keeps_position(self):
        payload = self.music_payload()
        with patch.object(main, "notify", new_callable=AsyncMock) as notify:
            first = await self.post(payload)
            self.assertEqual(first.status_code, 200, first.text)
            with main.db() as conn:
                old_time = conn.execute("SELECT updated_at FROM releases").fetchone()[0]
            same = await self.post(payload)
            self.assertFalse(same.json()["changed"])
            with main.db() as conn:
                self.assertEqual(
                    old_time,
                    conn.execute("SELECT updated_at FROM releases").fetchone()[0],
                )
            payload["release"]["price"] = "4400円"
            payload["release"]["cover_url"] = (
                "https://images.example.test/images/music/new-cover.jpg"
            )
            payload["release"]["position"] = 0
            updated = await self.post(payload)
            self.assertTrue(updated.json()["changed"])
            self.assertEqual(updated.json()["position"], first.json()["position"])
            self.assertEqual(notify.await_count, 2)
            with main.db() as conn:
                row = conn.execute("SELECT cover_url,price FROM releases").fetchone()
            self.assertEqual(row["cover_url"], payload["release"]["cover_url"])
            self.assertEqual(row["price"], "4400円")
            self.assertTrue(notify.await_args.args[0][0]["_cover_changed"])

    async def test_duplicate_concurrent_imports_send_once(self):
        with patch.object(main, "notify", new_callable=AsyncMock) as notify:
            results = await asyncio.gather(self.post(), self.post())
        self.assertEqual(sum(result.json()["changed"] for result in results), 1)
        notify.assert_awaited_once()

    async def test_failed_delivery_retries_durably_without_rewriting_data(self):
        with patch.object(
            main,
            "notify",
            new_callable=AsyncMock,
            side_effect=ValueError("OneBot failed"),
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["notifications"]["pending"], 1)
        with main.db() as conn:
            old_time = conn.execute("SELECT updated_at FROM releases").fetchone()[0]
        with patch.object(main, "notify", new_callable=AsyncMock) as notify:
            retried = await self.client.post(
                "/api/ingest/music-worker/notifications", headers=self.headers
            )
            repeat = await self.post()
        self.assertEqual(retried.json(), {"sent": 1, "pending": 0})
        self.assertFalse(repeat.json()["changed"])
        notify.assert_awaited_once()
        with main.db() as conn:
            self.assertEqual(
                old_time, conn.execute("SELECT updated_at FROM releases").fetchone()[0]
            )

    async def test_missing_onebot_config_leaves_notification_pending(self):
        main.save_settings({"onebot_url": ""})
        with patch.object(main, "notify", new_callable=AsyncMock) as notify:
            result = await self.post()
        notify.assert_not_awaited()
        self.assertEqual(result.json()["notifications"]["pending"], 1)

    async def test_worker_rejects_external_and_local_covers_but_legacy_keeps_contract(
        self,
    ):
        payload = self.music_payload()
        payload["release"]["cover_url"] = "https://www.example.com/cover.jpg"
        self.assertEqual((await self.post(payload)).status_code, 400)
        legacy = await self.client.post(
            "/api/ingest/music",
            json=payload,
            headers={"X-Nijidb-API-Key": "legacy-test"},
        )
        self.assertEqual(legacy.status_code, 200, legacy.text)
        payload = self.music_payload()
        payload["release"]["cover_local_path"] = "runtime:music-cover-cache/a.jpg"
        self.assertEqual((await self.post(payload)).status_code, 400)


class OneBotForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_and_private_forward_use_text_and_image_nodes(self):
        config = {
            "onebot_url": "https://bot.example.test",
            "onebot_token": "server-test-token",
            "onebot_target": "group:12345",
            "onebot_profile": "Nijidb",
        }
        item = {
            "id": "cd01",
            "title": "新音乐",
            "cover_url": "https://images.example.test/images/cd01.jpg",
            "_previous": None,
        }
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/get_login_info":
                return httpx.Response(
                    200,
                    json={"status": "ok", "retcode": 0, "data": {"user_id": 56789}},
                )
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"message_id": 1}}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with (
                patch.object(
                    main.httpx,
                    "AsyncClient",
                    return_value=AsyncMock(__aenter__=AsyncMock(return_value=client)),
                ),
                patch.object(main, "R2_PUBLIC_BASE_URL", "https://images.example.test"),
                patch.object(main, "R2_IMAGE_PREFIX", "images"),
            ):
                await main.notify([item], config, "音乐目录更新")
                await main.notify(
                    [item], {**config, "onebot_target": "private:12345"}, "音乐目录更新"
                )
        self.assertEqual(
            [request.url.path for request in requests],
            [
                "/get_login_info",
                "/send_group_forward_msg",
                "/get_login_info",
                "/send_private_forward_msg",
            ],
        )
        sent = json.loads(requests[1].content)
        self.assertEqual(sent["group_id"], 12345)
        nodes = sent["messages"]
        self.assertEqual(
            [node["data"]["content"][0]["type"] for node in nodes], ["text", "image"]
        )
        self.assertEqual(
            nodes[1]["data"]["content"][0]["data"]["file"], item["cover_url"]
        )
        self.assertTrue(all(node["data"]["uin"] == "56789" for node in nodes))
        self.assertEqual(
            requests[1].headers["authorization"], "Bearer server-test-token"
        )
        self.assertNotIn("server-test-token", requests[1].content.decode())
        self.assertNotIn("X-Nijidb-API-Key", requests[1].headers)
        self.assertEqual(json.loads(requests[3].content)["user_id"], 12345)

    def test_http_200_with_failure_retcode_is_not_delivery_success(self):
        request = httpx.Request(
            "POST", "https://bot.example.test/send_group_forward_msg"
        )
        for result in (
            {"status": "failed", "retcode": 100},
            {"status": "ok", "retcode": 1},
            {},
        ):
            with self.assertRaises(ValueError):
                main.onebot_result(httpx.Response(200, request=request, json=result))


if __name__ == "__main__":
    unittest.main()
