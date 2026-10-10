"""Offline safety checks for the one-shot R2 PNG-to-JPEG migration worker."""

from __future__ import annotations

import sqlite3
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from scripts import migrate_r2_png_to_jpg as migration

PUBLIC_BASE = "https://images.example.test"
PREFIX = "images"


def png_rgba(width: int, height: int, pixels: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + pixels[y * width * 4 : (y + 1) * width * 4] for y in range(height))
    return (
        migration.PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class R2PngReferenceTests(unittest.TestCase):
    def test_public_url_parser_is_scoped_to_exact_origin_prefix_and_png_path(self):
        self.assertEqual(
            migration.key_from_public_url(PUBLIC_BASE + "/images/news-remote/a.png", PUBLIC_BASE, PREFIX),
            "images/news-remote/a.png",
        )
        for url in (
            "https://evil.example/images/news-remote/a.png",
            PUBLIC_BASE + "/other/a.png",
            PUBLIC_BASE + "/images/../secret.png",
            PUBLIC_BASE + "/images/a.png?download=1",
            "https://user:pass@images.example.test/images/a.png",
            PUBLIC_BASE + "/images/a.jpg",
        ):
            self.assertIsNone(migration.key_from_public_url(url, PUBLIC_BASE, PREFIX), url)

    def test_url_scanner_finds_multiple_r2_pngs_but_skips_jpgs(self):
        text = f'<img src="{PUBLIC_BASE}/images/a.png"> {PUBLIC_BASE}/images/b.png) and {PUBLIC_BASE}/images/c.jpg'
        self.assertEqual(
            migration.png_urls_in_text(text, PUBLIC_BASE, PREFIX),
            [
                (PUBLIC_BASE + "/images/a.png", "images/a.png"),
                (PUBLIC_BASE + "/images/b.png", "images/b.png"),
            ],
        )

    def test_content_addressed_local_objects_get_new_jpeg_keys_but_release_ids_stay_stable(self):
        png_hash = "a" * 64
        jpeg_hash = "b" * 64
        program_ref = {"table": "program_occurrence_images", "metadata": {"local_path": f"programs/{png_hash}.png"}}
        collabo_ref = {"table": "collaboration_images", "metadata": {"path": f"assets/{png_hash}.png"}}
        release_ref = {"table": "releases", "metadata": {}}
        self.assertEqual(
            migration.preferred_jpeg_key(f"images/programs/{png_hash}.png", jpeg_hash, PREFIX, [program_ref]),
            f"images/programs/{jpeg_hash}.jpg",
        )
        self.assertEqual(
            migration.preferred_jpeg_key(
                f"images/illustrations/assets/{png_hash}.png", jpeg_hash, PREFIX, [collabo_ref]
            ),
            f"images/illustrations/assets/{jpeg_hash}.jpg",
        )
        self.assertEqual(
            migration.preferred_jpeg_key("images/cd82.png", jpeg_hash, PREFIX, [release_ref]), "images/cd82.jpg"
        )
        self.assertEqual(
            migration.local_destination_for_ref(
                program_ref, f"images/programs/{png_hash}.png", f"images/programs/{jpeg_hash}.jpg", PREFIX
            ),
            ("media", f"programs/{jpeg_hash}.jpg"),
        )
        self.assertEqual(
            migration.local_destination_for_ref(
                collabo_ref,
                f"images/illustrations/assets/{png_hash}.png",
                f"images/illustrations/assets/{jpeg_hash}.jpg",
                PREFIX,
            ),
            ("illustrations", f"assets/{jpeg_hash}.jpg"),
        )

    def test_database_scan_finds_only_supported_references_and_flags_unknown_columns(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE releases (id TEXT PRIMARY KEY, cover_url TEXT, detail_html TEXT);
            CREATE TABLE program_occurrence_images (id INTEGER PRIMARY KEY, local_path TEXT, public_url TEXT, sha256 TEXT, bytes INTEGER, width INTEGER, height INTEGER);
            CREATE TABLE misc (id INTEGER PRIMARY KEY, note TEXT);
            INSERT INTO releases VALUES ('cd1', 'https://images.example.test/images/cd1.png', '<img src="https://images.example.test/images/cd1.png">');
            INSERT INTO program_occurrence_images VALUES (2, 'programs/a.png', 'https://images.example.test/images/programs/a.png', 'old', 12, 2, 3);
            INSERT INTO misc VALUES (1, 'see https://images.example.test/images/unhandled.png');
        """)
        rows = migration.scan_db_references(connection, PUBLIC_BASE, PREFIX)
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(not row["supported"] for row in rows), 1)
        image = next(row for row in rows if row["table"] == "program_occurrence_images")
        self.assertEqual(image["metadata"]["local_path"], "programs/a.png")
        self.assertTrue(all(row["supported"] for row in rows if row["table"] == "releases"))
        connection.close()


@unittest.skipUnless(
    __import__("shutil").which("ffmpeg") and __import__("shutil").which("ffprobe"), "requires ffmpeg and ffprobe"
)
class PngConversionTests(unittest.TestCase):
    def test_transparency_is_matted_white_and_converted_to_valid_jpeg(self):
        pixels = bytes((255, 0, 0, 128)) * 16
        png = png_rgba(4, 4, pixels)
        details = migration.probe_png(png)
        self.assertEqual((details["width"], details["height"]), (4, 4))
        jpeg = migration.convert_png(png, details)
        self.assertTrue(jpeg.startswith(migration.JPEG_SIGNATURE))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample.jpg"
            target.write_bytes(jpeg)
            import json
            import subprocess

            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    str(target),
                ],
                capture_output=True,
                check=True,
            )
            stream = json.loads(probe.stdout)["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (4, 4))

    def test_rejects_bad_png_signature_before_ffmpeg(self):
        with self.assertRaisesRegex(ValueError, "PNG object"):
            migration.probe_png(b"not a PNG")


class DatabaseRewriteTests(unittest.TestCase):
    def test_updates_r2_urls_and_content_addressed_local_paths_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "nijidb.sqlite3"
            old_hashes = {"program": "a" * 64, "collaboration": "b" * 64}
            urls = {
                "program": PUBLIC_BASE + f"/images/programs/{old_hashes['program']}.png",
                "collaboration": PUBLIC_BASE + f"/images/illustrations/assets/{old_hashes['collaboration']}.png",
                "release": PUBLIC_BASE + "/images/cd82.png",
            }
            with sqlite3.connect(database) as connection:
                connection.executescript("""
                    CREATE TABLE program_occurrence_images (id INTEGER PRIMARY KEY, local_path TEXT, public_url TEXT, sha256 TEXT, bytes INTEGER, width INTEGER, height INTEGER);
                    CREATE TABLE collaboration_images (id TEXT PRIMARY KEY, path TEXT, public_url TEXT, sha256 TEXT, bytes INTEGER, width INTEGER, height INTEGER);
                    CREATE TABLE releases (id TEXT PRIMARY KEY, cover_url TEXT, detail_html TEXT);
                    CREATE TABLE news_articles (id TEXT PRIMARY KEY, body_markdown TEXT, summary TEXT);
                """)
                connection.execute(
                    "INSERT INTO program_occurrence_images VALUES (1, ?, ?, ?, ?, 4, 4)",
                    (f"programs/{old_hashes['program']}.png", urls["program"], old_hashes["program"], 12),
                )
                connection.execute(
                    "INSERT INTO collaboration_images VALUES ('collabo-id', ?, ?, ?, ?, 4, 4)",
                    (
                        f"assets/{old_hashes['collaboration']}.png",
                        urls["collaboration"],
                        old_hashes["collaboration"],
                        12,
                    ),
                )
                connection.execute(
                    "INSERT INTO releases VALUES ('cd82', ?, ?)", (urls["release"], f'<img src="{urls["release"]}">')
                )
                connection.execute(
                    "INSERT INTO news_articles VALUES ('article', ?, '')", (f"![cover]({urls['release']})",)
                )
            jpeg = b"jpeg-content"
            digest = migration.sha256(jpeg)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            references = migration.scan_db_references(connection, PUBLIC_BASE, PREFIX)
            connection.close()
            grouped = {}
            for reference in references:
                item = grouped.setdefault(reference["key"], {"key": reference["key"], "references": []})
                item["references"].append(reference)
            objects = {}
            for old_key, item in grouped.items():
                new_key = migration.preferred_jpeg_key(old_key, digest, PREFIX, item["references"])
                objects[old_key] = {
                    "new_key": new_key,
                    "new_url": PUBLIC_BASE + "/" + new_key,
                    "sha256": digest,
                    "bytes": len(jpeg),
                    "width": 4,
                    "height": 4,
                    "converted_backup": "converted/" + migration.sha256(old_key.encode()) + ".jpg",
                }
            work = root / "work"
            work.mkdir()
            for entry in objects.values():
                staged = work / entry["converted_backup"]
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(jpeg)
            plan = {
                "public_base": PUBLIC_BASE,
                "prefix": PREFIX,
                "objects": list(grouped.values()),
                "reference_digest": migration.reference_digest(references),
            }
            with (
                patch.object(migration, "DATABASE", database),
                patch.object(migration, "MEDIA_ROOT", root / "images"),
                patch.object(migration, "WORK_ROOT", work),
            ):
                changed = migration.update_database(plan, objects, PREFIX)
                with sqlite3.connect(database) as connection:
                    program = connection.execute(
                        "SELECT local_path,public_url,sha256,bytes FROM program_occurrence_images WHERE id=1"
                    ).fetchone()
                    collaboration = connection.execute(
                        "SELECT path,public_url,sha256,bytes FROM collaboration_images WHERE id='collabo-id'"
                    ).fetchone()
                    release = connection.execute(
                        "SELECT cover_url,detail_html FROM releases WHERE id='cd82'"
                    ).fetchone()
                    article = connection.execute(
                        "SELECT body_markdown FROM news_articles WHERE id='article'"
                    ).fetchone()[0]
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                expected_program_url = objects[f"images/programs/{old_hashes['program']}.png"]["new_url"]
                expected_collaboration_url = objects[f"images/illustrations/assets/{old_hashes['collaboration']}.png"][
                    "new_url"
                ]
                expected_release_url = objects["images/cd82.png"]["new_url"]
                self.assertEqual(program, (f"programs/{digest}.jpg", expected_program_url, digest, len(jpeg)))
                self.assertEqual(collaboration, (f"assets/{digest}.jpg", expected_collaboration_url, digest, len(jpeg)))
                self.assertEqual(release, (expected_release_url, f'<img src="{expected_release_url}">'))
                self.assertEqual(article, f"![cover]({expected_release_url})")
                self.assertGreater(changed["program_occurrence_images.image_metadata"], 0)
                self.assertTrue((root / "images" / program[0]).is_file())
                self.assertTrue((root / "images" / "illustrations" / collaboration[0]).is_file())
