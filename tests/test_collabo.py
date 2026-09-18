"""Offline regressions for collaboration records and search."""

from __future__ import annotations

import json
import sqlite3
import unittest

from app.collabo import (
    collaboration_combination_matches,
    collaboration_rows,
    ensure_collaboration_schema,
    upsert_collaboration_item,
)


class CollaborationStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_collaboration_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_approved_item_does_not_require_image_review(self) -> None:
        item_id = "a" * 16
        upsert_collaboration_item(
            self.conn,
            {
                "id": item_id,
                "title": "测试联动",
                "date": "2026-09-11",
                "review_status": "approved",
                "images": [
                    {
                        "id": "pending-image",
                        "url": "https://example.com/illustration.jpg",
                        "review_status": "pending",
                    }
                ],
            },
        )
        item = self.conn.execute(
            "SELECT review_status FROM collaboration_items WHERE id = ?", (item_id,)
        ).fetchone()
        image = self.conn.execute(
            "SELECT review_status FROM collaboration_images WHERE item_id = ?", (item_id,)
        ).fetchone()
        self.assertEqual(item["review_status"], "approved")
        self.assertEqual(image["review_status"], "approved")

    def test_character_tags_and_multiple_periods_are_normalized(self) -> None:
        item_id = "c" * 16
        upsert_collaboration_item(
            self.conn,
            {
                "id": item_id,
                "title": "角色时间测试",
                "date": "2026-09-11",
                "tags": ["上原歩夢", "lanzhu"],
                "periods": [
                    {
                        "start_date": "2026-09-09",
                        "end_date": "2026-09-12",
                        "title": "第一期",
                    },
                    {
                        "start_date": "2026-10-01",
                        "end_date": "2026-10-05",
                        "description": "返场活动",
                    },
                ],
            },
        )
        row = self.conn.execute(
            "SELECT tags_json, periods_json FROM collaboration_items WHERE id = ?", (item_id,)
        ).fetchone()
        self.assertEqual(json.loads(row["tags_json"]), ["ayumu", "lanzhu"])
        self.assertEqual(len(json.loads(row["periods_json"])), 2)
        self.assertEqual([entry["id"] for entry in collaboration_rows(self.conn, tags="ayumu")], [item_id])
        self.assertEqual(collaboration_rows(self.conn, tags="kasumi"), [])

    def test_combination_filters_keep_core_groups_and_use_exact_member_sets(self) -> None:
        all_tags = [
            "ayumu",
            "kasumi",
            "shizuku",
            "karin",
            "ai",
            "kanata",
            "setsuna",
            "emma",
            "rina",
            "shioriko",
            "mia",
            "lanzhu",
            "yu",
        ]
        idol_tags = [tag for tag in all_tags if tag != "yu"]
        initial9 = all_tags[:9]
        anime10 = initial9 + ["yu"]
        shioriko10 = initial9 + ["shioriko"]
        movie1 = ["ayumu", "shizuku", "kanata", "emma", "lanzhu"]
        movie2 = ["ai", "rina", "setsuna", "shioriko", "mia"]
        self.assertTrue(collaboration_combination_matches(all_tags, "all"))
        self.assertFalse(collaboration_combination_matches(all_tags, "idol12"))
        self.assertTrue(collaboration_combination_matches(idol_tags, "idol12"))
        self.assertTrue(collaboration_combination_matches(movie1, "movie1"))
        self.assertTrue(collaboration_combination_matches(movie1 + ["kasumi", "yu"], "movie1"))
        self.assertTrue(collaboration_combination_matches(movie2, "movie2"))
        self.assertTrue(collaboration_combination_matches(movie2 + ["karin"], "movie2"))
        self.assertTrue(collaboration_combination_matches(initial9, "initial9"))
        self.assertTrue(collaboration_combination_matches(anime10, "anime10"))
        self.assertTrue(collaboration_combination_matches(shioriko10, "shioriko10"))
        self.assertFalse(collaboration_combination_matches(initial9 + ["mia"], "initial9"))
        self.assertFalse(collaboration_combination_matches(anime10 + ["shioriko"], "anime10"))
        self.assertFalse(collaboration_combination_matches(shioriko10 + ["yu"], "shioriko10"))
        self.assertFalse(collaboration_combination_matches(initial9[:-1], "initial9"))
        self.assertFalse(collaboration_combination_matches(["ayumu", "shizuku", "setsuna"], "azuna"))

    def test_invalid_related_link_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "有效的 HTTP/HTTPS 地址"):
            upsert_collaboration_item(
                self.conn,
                {
                    "id": "e" * 16,
                    "title": "非法链接",
                    "date": "2026-09-11",
                    "links": [{"title": "恶意", "url": "javascript:alert(1)"}],
                },
            )

    def test_period_requires_title_or_description(self) -> None:
        with self.assertRaisesRegex(ValueError, "标题或描述"):
            upsert_collaboration_item(
                self.conn,
                {
                    "id": "d" * 16,
                    "title": "缺少时间段说明",
                    "date": "2026-09-11",
                    "periods": [{"start_date": "2026-09-09", "end_date": "2026-09-12"}],
                },
            )

    def test_search_matches_record_notes_and_image_metadata(self) -> None:
        item_id = "b" * 16
        upsert_collaboration_item(
            self.conn,
            {
                "id": item_id,
                "title": "测试联动",
                "date": "2026-09-11",
                "note": "正文中的独特关键词",
                "images": [
                    {
                        "url": "https://example.com/illustration.jpg",
                        "caption": "图片说明关键词",
                    }
                ],
            },
        )
        self.assertEqual([row["id"] for row in collaboration_rows(self.conn, "独特关键词")], [item_id])
        self.assertEqual([row["id"] for row in collaboration_rows(self.conn, "说明关键词")], [item_id])


if __name__ == "__main__":
    unittest.main()
