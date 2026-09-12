from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from markdownify import markdownify

NEWS_TOPICS_URL = "https://www.lovelive-anime.jp/nijigasaki/topics.php"
NEWS_SOURCES = {"niji_topics", "niji_news", "as_news"}
NEWS_SOURCE_LABELS = {
    "niji_topics": "Official Site News",
    "niji_news": "Official Site News",
    "official_site": "Official Site News",
    "as_news": "AS News",
}
NEWS_SOURCE_GROUPS = {"official_site": ("niji_topics", "niji_news"), "as_news": ("as_news",)}


def news_source_group(source: str) -> str:
    return "official_site" if source in {"niji_topics", "niji_news", "official_site"} else source


NEWS_CATEGORIES = {
    "その他",
    "メディア",
    "商品",
    "書籍・雑誌",
    "イベント",
    "配信番組",
    "ゲーム",
    "グッズ",
    "ブック",
    "ライブ/イベント",
    "キャスト配信/ラジオ",
    "アニメ放送/配信",
    "劇場",
    "キャスト映像商品",
    "アニメ映像商品",
    "音楽商品",
    "ＣＤ情報",
    "キャンペーン",
    "ご当地情報",
    "NEWS",
}
NEWS_EDITABLE_FIELDS = (
    "title",
    "published_at",
    "category",
    "tags_json",
    "summary",
    "body_markdown",
    "source_url",
)
NEWS_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?:<([^>]+)>|([^)\n]+))\)")
NEWS_HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
NEWS_HTML_IMAGE_ATTR_RE = re.compile(r"(?:data-src|data-original|src)\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
NEWS_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})日?")
NEWS_IMAGE_STYLE_RE = re.compile(r"(?:^|;)\s*(width|height)\s*:\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*(?:;|$)", re.IGNORECASE)
NEWS_IMAGE_MIN_WIDTH = 240
NEWS_IMAGE_MIN_HEIGHT = 120
NEWS_IMAGE_MAX_WIDTH = 10000
NEWS_IMAGE_MAX_HEIGHT = 10000
NEWS_SUMMARY_MAX_LENGTH = 200
NEWS_FILENAME_RE = re.compile(r"^\[(\d{8})\](niji_topics|niji_news|as_news)_(?:\d+)_([^/]+)\.md$")
NEWS_PAGE_ID_RE = re.compile(r"^\d+_[^/]+$")
NEWS_CHROME_LINES = NEWS_CATEGORIES | {
    "ニュース",
    "全てのニュース",
    "ニュース一覧へ",
    "最新のニュース一覧へ",
    "最新のニュースへ",
}
NEWS_CATEGORY_TAGS = {
    "グッズ": "goods",
    "商品": "goods",
    "音楽商品": "music",
    "ＣＤ情報": "music",
    "キャスト映像商品": "video",
    "アニメ映像商品": "video",
    "ブック": "book",
    "書籍・雑誌": "book",
    "メディア": "media",
    "ライブ/イベント": "event",
    "イベント": "event",
    "劇場": "theater",
    "キャンペーン": "campaign",
    "ご当地情報": "local",
    "ゲーム": "game",
    "キャスト配信/ラジオ": "voice-activity",
    "配信番組": "streaming",
    "NEWS": "announcement",
    "その他": "other",
}
NEWS_TAG_ORDER = (
    "game",
    "anime",
    "voice-activity",
    "voice:online",
    "voice:offline",
    "voice:radio",
    "collaboration",
    "apology",
    "goods",
    "music",
    "video",
    "book",
    "media",
    "event",
    "theater",
    "campaign",
    "local",
    "streaming",
    "announcement",
    "other",
)
NEWS_TAG_CATALOG_ORDER = (
    "game",
    "game:loveca",
    "game:other",
    "game:school-fes",
    "game:sukusta",
    "game:visual-novel",
    "anime",
    "anime:movie",
    "anime:movie-chapter-1",
    "anime:movie-chapter-2",
    "anime:movie-chapter-3",
    "anime:ova",
    "anime:spin-off",
    "anime:tv-season-1",
    "anime:tv-season-2",
    "anime:tv-season-3",
    "voice-activity",
    "voice:online",
    "voice:offline",
    "voice:radio",
    "collaboration",
    "apology",
    "goods",
    "music",
    "video",
    "book",
    "media",
    "event",
    "theater",
    "campaign",
    "local",
    "streaming",
    "announcement",
    "other",
)
NEWS_ALLOWED_TAGS = frozenset(NEWS_TAG_CATALOG_ORDER)
NEWS_TAG_LABELS = {
    "game": ("游戏", "ゲーム", "Games"),
    "game:loveca": ("Love Live! 卡牌", "ラブライブ！カードゲーム", "Love Live! Card Game"),
    "game:other": ("其他游戏", "その他のゲーム", "Other games"),
    "game:school-fes": ("学园偶像祭", "スクフェス", "School Idol Festival"),
    "game:sukusta": ("学园偶像祭 ALL STARS", "スクスタ", "ALL STARS"),
    "game:visual-novel": ("视觉小说", "ビジュアルノベル", "Visual novels"),
    "anime": ("动画", "アニメ", "Anime"),
    "anime:movie": ("剧场版", "劇場版", "Movie"),
    "anime:movie-chapter-1": ("剧场版第1章", "劇場版第1章", "Movie chapter 1"),
    "anime:movie-chapter-2": ("剧场版第2章", "劇場版第2章", "Movie chapter 2"),
    "anime:movie-chapter-3": ("剧场版第3章", "劇場版第3章", "Movie chapter 3"),
    "anime:ova": ("OVA", "OVA", "OVA"),
    "anime:spin-off": ("衍生动画", "スピンオフアニメ", "Spin-off anime"),
    "anime:tv-season-1": ("TV动画第1季", "TVアニメ第1期", "TV season 1"),
    "anime:tv-season-2": ("TV动画第2季", "TVアニメ第2期", "TV season 2"),
    "anime:tv-season-3": ("TV动画第3季", "TVアニメ第3期", "TV season 3"),
    "voice-activity": ("声优活动", "キャスト活動", "Cast activities"),
    "voice:online": ("声优线上活动", "キャスト配信", "Online cast events"),
    "voice:offline": ("声优线下活动", "キャスト現地イベント", "In-person cast events"),
    "voice:radio": ("声优广播", "キャストラジオ", "Cast radio"),
    "collaboration": ("联动", "コラボ", "Collaboration"),
    "apology": ("致歉与更正", "お詫び・訂正", "Apologies & corrections"),
    "goods": ("周边", "グッズ", "Merchandise"),
    "music": ("音乐", "音楽", "Music"),
    "video": ("影像商品", "映像商品", "Video releases"),
    "book": ("书籍杂志", "書籍・雑誌", "Books & magazines"),
    "media": ("媒体", "メディア", "Media"),
    "event": ("活动", "イベント", "Events"),
    "theater": ("影院", "劇場", "Theater"),
    "campaign": ("宣传活动", "キャンペーン", "Campaigns"),
    "local": ("地方资讯", "ご当地情報", "Local news"),
    "streaming": ("节目配信", "番組配信", "Streaming"),
    "announcement": ("公告", "お知らせ", "Announcements"),
    "other": ("其他", "その他", "Other"),
}

NEWS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS news_articles (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  page_name TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  published_at TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  tags_json TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '',
  body_markdown TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  source_file TEXT NOT NULL DEFAULT '',
  source_hash TEXT NOT NULL DEFAULT '',
  manual_fields_json TEXT NOT NULL DEFAULT '[]',
  last_seen_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_articles_date ON news_articles(published_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_news_articles_source ON news_articles(source, published_at DESC);
CREATE TABLE IF NOT EXISTS news_tags (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  labels_json TEXT NOT NULL DEFAULT '{}',
  sort_order INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_tags_active ON news_tags(active, sort_order, id);
CREATE TABLE IF NOT EXISTS news_article_tags (
  news_id TEXT NOT NULL,
  tag_id TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(news_id, tag_id),
  FOREIGN KEY(news_id) REFERENCES news_articles(id) ON DELETE CASCADE,
  FOREIGN KEY(tag_id) REFERENCES news_tags(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_news_article_tags_tag ON news_article_tags(tag_id, news_id);
CREATE TABLE IF NOT EXISTS news_images (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  news_id TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'remote',
  local_path TEXT NOT NULL DEFAULT '',
  public_url TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  alt_text TEXT NOT NULL DEFAULT '',
  width INTEGER NOT NULL DEFAULT 0,
  height INTEGER NOT NULL DEFAULT 0,
  bytes INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY(news_id) REFERENCES news_articles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_news_images_article ON news_images(news_id, position, id);
CREATE TABLE IF NOT EXISTS news_image_suppressions (
  news_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  local_path TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY(news_id, kind, local_path, source_url),
  FOREIGN KEY(news_id) REFERENCES news_articles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_news_image_suppressions_article ON news_image_suppressions(news_id);
CREATE TABLE IF NOT EXISTS news_fetch_state (
  news_id TEXT PRIMARY KEY,
  source_url TEXT NOT NULL DEFAULT '',
  etag TEXT NOT NULL DEFAULT '',
  last_modified TEXT NOT NULL DEFAULT '',
  checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  checked_at TEXT NOT NULL,
  discovered_count INTEGER NOT NULL DEFAULT 0,
  changed_count INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS news_refresh_queue (
  news_id TEXT PRIMARY KEY,
  source_url TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  risk INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT NOT NULL DEFAULT '',
  last_success_at TEXT NOT NULL DEFAULT '',
  next_attempt_at TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(news_id) REFERENCES news_articles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_news_refresh_queue_status
  ON news_refresh_queue(status, next_attempt_at, updated_at);
"""


def _is_news_chrome_markdown_line(line: str) -> bool:
    stripped = line.strip()
    if stripped in NEWS_CHROME_LINES:
        return True
    match = re.fullmatch(r"(?:[-*+]\s*)?\[([^]]+)\]\(([^)]+)\)", stripped)
    if not match or match.group(1).strip() not in NEWS_CHROME_LINES:
        return False
    return urlparse(match.group(2)).path.rstrip("/").endswith("topics.php")


def clean_news_markdown(value: Any) -> str:
    """Remove official-site navigation and image-conversion artifacts."""
    lines: list[str] = []
    has_content = False
    for line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if _is_news_chrome_markdown_line(line):
            continue
        if not has_content and not line.strip():
            continue
        if line.strip():
            has_content = True
        lines.append(line)
    return strip_news_image_markup("\n".join(lines).strip())


def truncate_news_summary(value: Any) -> str:
    return clean_news_markdown(value)[:NEWS_SUMMARY_MAX_LENGTH].rstrip()


def ensure_news_schema(conn) -> None:
    conn.executescript(NEWS_SCHEMA_SQL)
    image_columns = {row["name"] for row in conn.execute("PRAGMA table_info(news_images)")}
    if "public_url" not in image_columns:
        conn.execute("ALTER TABLE news_images ADD COLUMN public_url TEXT NOT NULL DEFAULT ''")
    for row in conn.execute("SELECT id, body_markdown, summary, manual_fields_json FROM news_articles").fetchall():
        manual_fields = decode_json(row["manual_fields_json"], [])
        body = row["body_markdown"] if "body_markdown" in manual_fields else clean_news_markdown(row["body_markdown"])
        # Summaries have a hard storage limit, including administrator-edited values.
        summary = truncate_news_summary(row["summary"])
        if body != row["body_markdown"] or summary != row["summary"]:
            conn.execute(
                "UPDATE news_articles SET body_markdown = ?, summary = ? WHERE id = ?", (body, summary, row["id"])
            )
    tag_columns = {row["name"] for row in conn.execute("PRAGMA table_info(news_tags)")}
    if "aliases_json" not in tag_columns:
        conn.execute("ALTER TABLE news_tags ADD COLUMN aliases_json TEXT NOT NULL DEFAULT '[]'")
    ensure_news_tag_definitions(conn)
    migrate_news_article_tags(conn)


def decode_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


NEWS_TAG_KEY_RE = re.compile(r"^[\w][\w:.-]{0,79}$", re.UNICODE)


def normalized_news_tag_key(value: Any) -> str:
    slug = str(value or "").strip()
    if not NEWS_TAG_KEY_RE.fullmatch(slug):
        raise ValueError("标签 ID 只能使用字母、数字、下划线、冒号、点或短横线，长度为 1–80 个字符")
    return slug


def clean_tag_values(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("[") and raw.endswith("]"):
            parsed = decode_json(raw, None)
            value = parsed if isinstance(parsed, list) else raw[1:-1]
        else:
            value = raw
    if not isinstance(value, (list, tuple, set)):
        value = re.split(r"[,，、\n]+", str(value or ""))
    result: list[str] = []
    for item in value:
        tag = str(item or "").strip().strip("'\"")
        if tag and tag not in result:
            result.append(tag)
    return result


def normalized_tags(value: Any) -> list[str]:
    return [tag for tag in clean_tag_values(value) if tag in NEWS_ALLOWED_TAGS]


def news_tag_id_for_slug(slug: str) -> str:
    return f"news-tag-{hashlib.sha256(f'news-tag:{slug}'.encode()).hexdigest()[:20]}"


def default_news_tag_labels(slug: str) -> dict[str, str]:
    labels = NEWS_TAG_LABELS.get(slug, (slug, slug, slug))
    return {"zh-CN": labels[0], "ja": labels[1], "en": labels[2]}


def normalized_news_tag_labels(value: Any, slug: str) -> dict[str, str]:
    labels = default_news_tag_labels(slug)
    if isinstance(value, dict):
        for language in labels:
            candidate = str(value.get(language) or "").strip()
            if candidate:
                labels[language] = candidate[:200]
    elif isinstance(value, str) and value.strip():
        labels = {language: value.strip()[:200] for language in labels}
    return labels


def _insert_news_tag(
    conn,
    slug: str,
    labels: Any = None,
    sort_order: int = 0,
    active: int = 1,
    now: str | None = None,
):
    timestamp = now or datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO news_tags
        (id, slug, labels_json, sort_order, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            news_tag_id_for_slug(slug),
            slug,
            json.dumps(normalized_news_tag_labels(labels, slug), ensure_ascii=False),
            sort_order,
            active,
            timestamp,
            timestamp,
        ),
    )
    return conn.execute("SELECT * FROM news_tags WHERE slug = ?", (slug,)).fetchone()


def news_tag_row_for_value(conn, value: Any):
    token = str(value or "").strip()
    if not token:
        return None
    row = conn.execute("SELECT * FROM news_tags WHERE id = ? OR slug = ? LIMIT 1", (token, token)).fetchone()
    if row is not None:
        return row
    for candidate in conn.execute("SELECT * FROM news_tags").fetchall():
        if token in clean_tag_values(decode_json(candidate["aliases_json"], [])):
            return candidate
    return None


def ensure_news_tag_definitions(conn) -> None:
    now = datetime.now(UTC).isoformat()
    for position, slug in enumerate(NEWS_TAG_CATALOG_ORDER):
        row = news_tag_row_for_value(conn, slug)
        if row is None:
            _insert_news_tag(conn, slug, default_news_tag_labels(slug), position, now=now)
            continue
        if not isinstance(decode_json(row["labels_json"], None), dict) or not decode_json(row["labels_json"], {}):
            conn.execute(
                "UPDATE news_tags SET labels_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(default_news_tag_labels(slug), ensure_ascii=False), now, row["id"]),
            )


def migrate_news_article_tags(conn) -> None:
    now = datetime.now(UTC).isoformat()
    next_position = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM news_tags").fetchone()[0]
    for article in conn.execute("SELECT id, tags_json FROM news_articles").fetchall():
        raw_tags = clean_tag_values(decode_json(article["tags_json"], article["tags_json"]))
        active_tags: list[tuple[str, str]] = []
        for slug in raw_tags:
            row = news_tag_row_for_value(conn, slug)
            if row is None:
                row = _insert_news_tag(conn, slug, sort_order=next_position, now=now)
                next_position += 1
            if row["active"]:
                active_tags.append((row["slug"], row["id"]))
        conn.execute("DELETE FROM news_article_tags WHERE news_id = ?", (article["id"],))
        conn.executemany(
            "INSERT INTO news_article_tags(news_id, tag_id, position) VALUES (?, ?, ?)",
            [(article["id"], tag_id, position) for position, (_, tag_id) in enumerate(active_tags)],
        )
        normalized = [slug for slug, _ in active_tags]
        if normalized != raw_tags:
            conn.execute(
                "UPDATE news_articles SET tags_json = ? WHERE id = ?",
                (json.dumps(normalized, ensure_ascii=False), article["id"]),
            )


def resolve_news_tag_values(conn, value: Any, include_inactive: bool = False) -> tuple[list[str], list[str], list[str]]:
    keys: list[str] = []
    ids: list[str] = []
    invalid: list[str] = []
    for token in clean_tag_values(value):
        row = news_tag_row_for_value(conn, token)
        if not row or (not include_inactive and not row["active"]):
            invalid.append(token)
            continue
        if row["id"] in ids:
            continue
        ids.append(row["id"])
        keys.append(row["slug"])
    return keys, ids, invalid


def sync_news_article_tags(conn, news_id: str, value: Any) -> tuple[list[str], list[str]]:
    keys, ids, _ = resolve_news_tag_values(conn, value)
    conn.execute("DELETE FROM news_article_tags WHERE news_id = ?", (news_id,))
    conn.executemany(
        "INSERT INTO news_article_tags(news_id, tag_id, position) VALUES (?, ?, ?)",
        [(news_id, tag_id, position) for position, tag_id in enumerate(ids)],
    )
    return keys, ids


def news_article_tag_ids(conn, news_id: str) -> list[str]:
    return [
        row["tag_id"]
        for row in conn.execute(
            "SELECT tag_id FROM news_article_tags WHERE news_id = ? ORDER BY position, tag_id", (news_id,)
        ).fetchall()
    ]


def news_tag_catalog(conn, counts: dict[str, int] | None = None, include_inactive: bool = False) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM news_tags WHERE active = 1 OR ? ORDER BY sort_order, id", (1 if include_inactive else 0,)
    ).fetchall()
    if counts is None:
        counts = {
            row["slug"]: row["count"]
            for row in conn.execute(
                """SELECT t.slug, COUNT(a.news_id) AS count
                   FROM news_tags t
                   LEFT JOIN news_article_tags a ON a.tag_id = t.id
                   WHERE t.active = 1
                   GROUP BY t.id, t.slug"""
            ).fetchall()
        }
    result = []
    for row in rows:
        labels = normalized_news_tag_labels(decode_json(row["labels_json"], {}), row["slug"])
        result.append(
            {
                "id": row["id"],
                "name": row["slug"],
                "labels": labels,
                "label": labels["zh-CN"],
                "count": counts.get(row["slug"], 0),
                "sort_order": row["sort_order"],
                "active": bool(row["active"]),
            }
        )
    return result


def normalize_news_date(value: Any, fallback: str = "") -> str:
    raw = str(value or "").strip()
    match = NEWS_DATE_RE.search(raw)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    fallback_match = NEWS_DATE_RE.search(str(fallback or ""))
    if fallback_match:
        year, month, day = fallback_match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    fallback_digits = re.sub(r"\D", "", str(fallback or ""))
    return f"{fallback_digits[:4]}-{fallback_digits[4:6]}-{fallback_digits[6:8]}" if len(fallback_digits) == 8 else ""


def metadata_field(text: str, name: str) -> str:
    match = re.search(rf"^- {re.escape(name)}[：:](.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def markdown_section(text: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _is_image_srcset_artifact(value: str) -> bool:
    return bool(
        re.match(r"^\s*(?:https?:)?//", value, re.IGNORECASE)
        and re.search(r"\b\d+(?:\.\d+)?w\b", value, re.IGNORECASE)
    )


def strip_news_image_markup(text: str) -> str:
    lines: list[str] = []
    cleaned_text = NEWS_HTML_IMAGE_RE.sub("", str(text or ""))
    for raw_line in cleaned_text.splitlines():
        line = raw_line
        for image_match in NEWS_IMAGE_RE.finditer(line):
            if _is_image_srcset_artifact(line[image_match.end() :]):
                line = line[: image_match.end()]
                break
        if _is_image_srcset_artifact(line):
            continue
        lines.append(NEWS_IMAGE_RE.sub("", line).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def strip_markdown_images(text: str) -> str:
    return strip_news_image_markup(text)


def news_image_tag_source(image) -> str:
    fallback = ""
    for attribute in ("data-src", "data-original", "data-lazy-src", "src"):
        value = str(image.get(attribute) or "").strip()
        if not value:
            continue
        fallback = fallback or value
        if not value.lower().startswith("data:"):
            return value
    srcset = str(image.get("srcset") or image.get("data-srcset") or "").strip()
    if srcset:
        return srcset.split(",", 1)[0].strip().split(" ", 1)[0]
    return fallback


def image_references(text: str, base_url: str = "") -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_reference(raw: Any) -> None:
        raw_value = unquote(str(raw or "").strip()).removeprefix("./")
        if raw_value.startswith("pic/"):
            normalized = str(Path(raw_value).as_posix())
            if normalized.startswith("pic/") and ".." not in Path(normalized).parts:
                key = f"local:{normalized}"
                if key not in seen:
                    references.append({"kind": "archive", "local_path": normalized, "source_url": ""})
                    seen.add(key)
            return
        parsed = urlparse(raw_value)
        if not parsed.scheme and base_url and raw_value.startswith(("/", "//")):
            raw_value = urljoin(base_url, raw_value)
            parsed = urlparse(raw_value)
        if parsed.scheme in {"http", "https"}:
            source_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))
            key = f"remote:{source_url}"
            if key not in seen:
                references.append({"kind": "remote", "local_path": "", "source_url": source_url})
                seen.add(key)

    for match in NEWS_IMAGE_RE.finditer(text):
        add_reference(match.group(1) or match.group(2))
    for image in BeautifulSoup(text, "html.parser").find_all("img"):
        add_reference(news_image_tag_source(image))
    return references


def news_id(source: str, page_name: str, source_url: str = "") -> str:
    identity = f"{source}:{page_name or source_url}".encode()
    return hashlib.sha256(identity).hexdigest()[:16]


def source_from_markdown(path: Path) -> str:
    match = NEWS_FILENAME_RE.match(path.name)
    if match:
        return match.group(2)
    if "topics" in path.name:
        return "niji_topics"
    if "as_news" in path.name:
        return "as_news"
    return "niji_news"


def parse_local_markdown(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    source = source_from_markdown(path)
    filename_match = NEWS_FILENAME_RE.match(path.name)
    filename_date = filename_match.group(1) if filename_match else ""
    title = ""
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    page_name = metadata_field(text, "页面名称")
    source_url = metadata_field(text, "来源")
    category = metadata_field(text, "分类")
    summary = truncate_news_summary(markdown_section(text, "摘要"))
    body_heading = re.search(r"^##\s+页面内容\s*$", text, re.MULTILINE)
    body = text[body_heading.end() :].strip() if body_heading else ""
    if re.search(r"^# ニュース\s*$", body, re.MULTILINE):
        date_heading = re.search(r"^######\s+20\d{2}[./-]\d{1,2}[./-]\d{1,2}.*$", body, re.MULTILINE)
        if date_heading:
            body = body[date_heading.start() :]
    footer = re.search(r"^\*\s+(?:\[)?最新のニュース一覧へ", body, re.MULTILINE)
    if footer:
        body = body[: footer.start()]
    body = strip_markdown_images(body)
    if not body:
        body = summary
    refs = image_references(text, source_url)
    published_at = normalize_news_date(metadata_field(text, "发布日期"), filename_date)
    tags_line = re.search(r"^- tags[：:]\s*\[([^\]]*)\]", text, re.MULTILINE | re.IGNORECASE)
    tags = normalized_tags(tags_line.group(1) if tags_line else [])
    return {
        "id": news_id(source, page_name, source_url),
        "source": source if source in NEWS_SOURCES else "niji_news",
        "page_name": page_name or path.stem,
        "title": title,
        "published_at": published_at,
        "category": category,
        "tags": tags,
        "summary": summary,
        "body_markdown": body.strip(),
        "source_url": source_url,
        "source_file": path.name,
        "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "images": refs,
    }


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def inferred_topic_tags(title: str, category: str, body: str = "") -> list[str]:
    text = f"{title}\n{category}\n{body}"
    tags: set[str] = set()
    category_tag = NEWS_CATEGORY_TAGS.get(category)
    if category_tag:
        tags.add(category_tag)
    if category == "ゲーム" or re.search(r"スクスタ|スクフェス|ゲーム|ビジュアルノベル", text, re.IGNORECASE):
        tags.add("game")
    if category in {"アニメ放送/配信", "アニメ映像商品", "劇場"} or re.search(
        r"TVアニメ|アニメーション|劇場版|映画|完結編|OVA|NEXT SKY|にじよん", text, re.IGNORECASE
    ):
        tags.add("anime")
    if re.search(r"コラボ(?:レーション)?|タイアップ|共同企画|×", text, re.IGNORECASE):
        tags.add("collaboration")
    if re.search(r"お詫び|おわび|訂正|誤植|不具合", text):
        tags.add("apology")
    if category == "キャスト配信/ラジオ" or re.search(
        r"生放送|生配信|ラジオ|AuDee|キャスト|声優|舞台挨拶|公開収録", title, re.IGNORECASE
    ):
        tags.add("voice-activity")
    if "voice-activity" in tags and re.search(
        r"生放送|生配信|アーカイブ配信|オンライン|YouTube|ニコニコ|ABEMA|配信", title, re.IGNORECASE
    ):
        tags.add("voice:online")
    if "voice-activity" in tags and re.search(r"ラジオ|AuDee|がさらじ|Webラジオ", title, re.IGNORECASE):
        tags.add("voice:radio")
    if "voice-activity" in tags and re.search(
        r"舞台挨拶|公開収録|(?<!ラブ)ライブ|(?<!Love)Live|公演|ステージ|トークショー|お渡し会|イベント",
        title,
        re.IGNORECASE,
    ):
        tags.add("voice:offline")
    return [tag for tag in NEWS_TAG_ORDER if tag in tags]


def _parse_image_dimension(value: Any) -> int | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*", str(value or ""), re.IGNORECASE)
    return int(float(match.group(1))) if match else None


def news_image_tag_dimensions(image) -> tuple[int | None, int | None]:
    dimensions: dict[str, int | None] = {
        "width": _parse_image_dimension(image.get("width")),
        "height": _parse_image_dimension(image.get("height")),
    }
    for name, raw_value in NEWS_IMAGE_STYLE_RE.findall(str(image.get("style") or "")):
        dimensions[name.lower()] = _parse_image_dimension(raw_value)
    return dimensions["width"], dimensions["height"]


def news_image_dimensions_allowed(width: int | None, height: int | None) -> bool:
    bounds = (
        (width, NEWS_IMAGE_MIN_WIDTH, NEWS_IMAGE_MAX_WIDTH),
        (height, NEWS_IMAGE_MIN_HEIGHT, NEWS_IMAGE_MAX_HEIGHT),
    )
    return all(value is None or lower <= value <= upper for value, lower, upper in bounds)


def _canonical_page_url(url: str, base_url: str = NEWS_TOPICS_URL) -> str:
    absolute = urljoin(base_url, url)
    parsed = urlparse(absolute)
    query = parse_qs(parsed.query)
    page_key = "p" if query.get("p") else "id" if query.get("id") else ""
    page = (query.get(page_key) or [""])[0] if page_key else ""
    if page:
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", f"{page_key}={page}", ""))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def page_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    value = str((query.get("p") or query.get("id") or [""])[0]).strip()
    if value:
        return value
    match = re.search(r"/(\d+_[^/?.#]+)\.html$", parsed.path)
    return match.group(1) if match else ""


def _is_topic_detail_url(url: str, base_url: str = NEWS_TOPICS_URL) -> bool:
    parsed = urlparse(urljoin(base_url, url))
    if parsed.hostname not in {"www.lovelive-anime.jp", "lovelive-anime.jp"}:
        return False
    page = page_name_from_url(url)
    return bool(page and (NEWS_PAGE_ID_RE.match(page) or re.match(r"^\d+$", page)))


def _listing_container(anchor):
    current = anchor
    for _ in range(6):
        current = current.parent
        if current is None:
            break
        classes = " ".join(current.get("class") or []).lower()
        if current.name in {"li", "article"} or any(token in classes for token in ("news", "topic", "list")):
            return current
    return anchor.parent or anchor


def _category_from_container(container) -> str:
    for node in container.find_all(["span", "p", "small", "div"]):
        text = _clean_text(node.get_text(" ", strip=True))
        if text in NEWS_CATEGORIES:
            return text
    text = _clean_text(container.get_text(" ", strip=True))
    for category in sorted(NEWS_CATEGORIES, key=len, reverse=True):
        if category in text:
            return category
    return ""


def _date_from_text(text: str) -> str:
    match = NEWS_DATE_RE.search(text)
    return normalize_news_date(match.group(0) if match else "")


def _listing_title(anchor, container) -> str:
    title = _clean_text(anchor.get_text(" ", strip=True))
    if not title or len(title) < 2:
        title = _clean_text(container.get_text(" ", strip=True))
    title = NEWS_DATE_RE.sub("", title)
    for category in sorted(NEWS_CATEGORIES, key=len, reverse=True):
        title = re.sub(rf"^\s*{re.escape(category)}\s*", "", title)
    return title.strip(" ·|｜-–—")


def parse_topic_listing(html: str, base_url: str = NEWS_TOPICS_URL) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        raw_url = str(anchor.get("href") or "").strip()
        if not _is_topic_detail_url(raw_url, base_url):
            continue
        source_url = _canonical_page_url(raw_url, base_url)
        if source_url in seen:
            continue
        container = _listing_container(anchor)
        title = _listing_title(anchor, container)
        date_value = _date_from_text(_clean_text(container.get_text(" ", strip=True)))
        if not title or not date_value:
            continue
        seen.add(source_url)
        entries.append(
            {
                "source_url": source_url,
                "page_name": page_name_from_url(source_url),
                "title": title,
                "published_at": date_value,
                "category": _category_from_container(container),
            }
        )
    return entries


def topic_next_offset(html: str, current_offset: int) -> int | None:
    soup = BeautifulSoup(html, "html.parser")
    offsets = []
    for anchor in soup.find_all("a", href=True):
        parsed = urlparse(urljoin(NEWS_TOPICS_URL, str(anchor.get("href") or "")))
        if not parsed.path.endswith("/topics.php"):
            continue
        try:
            offset = int((parse_qs(parsed.query).get("offset") or [""])[0])
        except ValueError:
            continue
        if offset > current_offset:
            offsets.append(offset)
    return min(offsets) if offsets else None


def _detail_root(soup: BeautifulSoup):
    for selector in (
        ".news-detail",
        ".news_detail",
        ".topics-detail",
        ".detail",
        ".newsbox",
        ".news",
        ".p-page__detail",
        "#main",
        "main",
        ".main",
        "#contents",
        ".contents",
        "article",
    ):
        root = soup.select_one(selector)
        if root and len(_clean_text(root.get_text(" ", strip=True))) > 40:
            return root
    return soup.body or soup


def _html_markdown(root, source_url: str) -> str:
    clone = BeautifulSoup(str(root), "html.parser")
    for node in clone.select(
        "script, style, noscript, nav, header, footer, .breadcrumb, .pankuzu, .share, .pager, .pagination, #contentsmenu, #contentstitle, #social, .navi, p.cat, #bnr2, img"
    ):
        node.decompose()
    for anchor in clone.select("a[href]"):
        anchor["href"] = urljoin(source_url, anchor["href"])
    return markdownify(str(clone), heading_style="ATX", bullets="-").strip()


def _html_text(root) -> str:
    clone = BeautifulSoup(str(root), "html.parser")
    for node in clone.select(
        "script, style, noscript, nav, header, footer, .breadcrumb, .pankuzu, .share, .pager, .pagination"
    ):
        node.decompose()
    lines: list[str] = []
    for raw in clone.get_text("\n", strip=True).splitlines():
        line = _clean_text(raw)
        if not line or line in NEWS_CHROME_LINES or line in lines:
            continue
        lines.append(line)
    return "\n\n".join(lines)


def parse_topic_detail(
    html: str,
    source_url: str,
    listing: dict[str, str] | None = None,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    root = _detail_root(soup)
    title = str((listing or {}).get("title") or "").strip()
    if not title:
        heading_nodes = root.select("h1, h2, h3, h4, h5, h6")
        if not heading_nodes:
            heading_nodes = soup.select("h1, h2, h3, h4, h5, h6")
        for heading in heading_nodes:
            candidate = _clean_text(heading.get_text(" ", strip=True))
            if (
                candidate
                and candidate not in NEWS_CHROME_LINES
                and candidate not in NEWS_CATEGORIES
                and not NEWS_DATE_RE.fullmatch(candidate)
                and len(candidate) > 2
            ):
                title = candidate
                break
    page_name = page_name_from_url(source_url)
    text = _html_text(root)
    if (
        not title
        or not text
        or any(marker in text.lower() for marker in ("just a moment", "access denied", "verify you are human"))
    ):
        raise ValueError("官网详情未包含有效新闻内容")
    published_at = str((listing or {}).get("published_at") or "") or _date_from_text(text)
    category = str((listing or {}).get("category") or "").strip() or _category_from_container(root)
    body_lines = text.splitlines()
    # Remove the repeated page chrome at the beginning while retaining a plain,
    # readable article when the official HTML uses a non-semantic layout.
    while body_lines and (
        body_lines[0] in {"ニュース", "NEWS"}
        or body_lines[0] == category
        or body_lines[0] == title
        or bool(NEWS_DATE_RE.fullmatch(body_lines[0]))
    ):
        body_lines.pop(0)
    footer_index = next(
        (index for index, line in enumerate(body_lines) if line in {"最新のニュース一覧へ", "ニュース一覧へ"}),
        None,
    )
    if footer_index is not None:
        body_lines = body_lines[:footer_index]
    body = "\n\n".join(
        line
        for line in body_lines
        if line != title
        and line not in {"ニュース", "NEWS"}
        and line not in NEWS_CHROME_LINES
        and not NEWS_DATE_RE.fullmatch(line)
    )
    summary = truncate_news_summary(" ".join(body.split()))
    images: list[dict[str, Any]] = []
    seen: set[str] = set()
    for image in root.find_all("img"):
        raw = news_image_tag_source(image)
        if not raw or raw.startswith("data:"):
            continue
        width, height = news_image_tag_dimensions(image)
        if not news_image_dimensions_allowed(width, height):
            continue
        absolute = urljoin(source_url, raw)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or "/nijigasaki/img/" in parsed.path:
            continue
        absolute = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))
        if absolute in seen:
            continue
        seen.add(absolute)
        images.append(
            {
                "kind": "remote",
                "local_path": "",
                "source_url": absolute,
                "alt_text": _clean_text(image.get("alt") or ""),
                "width": width or 0,
                "height": height or 0,
            }
        )
    return {
        "id": news_id("niji_topics", page_name, source_url),
        "source": "niji_topics",
        "page_name": page_name,
        "title": title,
        "published_at": normalize_news_date(published_at),
        "category": category,
        "tags": inferred_topic_tags(title, category, body),
        "summary": truncate_news_summary(summary),
        "body_markdown": clean_news_markdown(_html_markdown(root, source_url)) or body.strip() or summary,
        "source_url": _canonical_page_url(source_url),
        "source_file": "",
        "source_hash": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "images": images,
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def upsert_news_record(conn, record: dict[str, Any], now: str | None = None) -> tuple[bool, bool]:
    """Store one source record and return (changed, created).

    Values explicitly edited by an administrator are kept when a later local
    import or topics crawl supplies a new source version.
    """
    timestamp = now or _now()
    source = str(record.get("source") or "niji_news")
    if source not in NEWS_SOURCES:
        source = "niji_news"
    record_id = str(
        record.get("id") or news_id(source, str(record.get("page_name") or ""), str(record.get("source_url") or ""))
    )
    tags = normalized_tags(record.get("tags", []))
    incoming = {
        "id": record_id,
        "source": source,
        "page_name": str(record.get("page_name") or "").strip(),
        "title": str(record.get("title") or "").strip(),
        "published_at": normalize_news_date(record.get("published_at")),
        "category": str(record.get("category") or "").strip(),
        "tags_json": json.dumps(tags, ensure_ascii=False),
        "summary": truncate_news_summary(record.get("summary")),
        "body_markdown": clean_news_markdown(record.get("body_markdown")),
        "source_url": str(record.get("source_url") or "").strip(),
        "source_file": str(record.get("source_file") or "").strip(),
        "source_hash": str(record.get("source_hash") or "").strip(),
    }
    old = conn.execute("SELECT * FROM news_articles WHERE id = ?", (record_id,)).fetchone()
    manual_fields = set(decode_json(old["manual_fields_json"], []) if old else [])
    values = dict(incoming)
    if old:
        for field in NEWS_EDITABLE_FIELDS:
            if field in manual_fields:
                values[field] = old[field]
        # Keep the richer tags generated by the local Markdown classifier when
        # a live Topics refresh touches the same imported article.
        if (
            not incoming["source_file"]
            and old["source_file"]
            and old["tags_json"]
            or not tags
            and not incoming["source_file"]
            and old["tags_json"]
        ):
            values["tags_json"] = old["tags_json"]
        if not values["source_file"]:
            values["source_file"] = old["source_file"]
    values["summary"] = truncate_news_summary(values["summary"])
    changed = not old or any(
        str(old[field] or "") != str(values[field] or "")
        for field in values
        if field not in {"id", "source_hash", "source_file"}
    )
    if not old:
        conn.execute(
            """INSERT INTO news_articles
            (id, source, page_name, title, published_at, category, tags_json, summary, body_markdown,
             source_url, source_file, source_hash, manual_fields_json, last_seen_at, created_at, updated_at)
            VALUES (:id, :source, :page_name, :title, :published_at, :category, :tags_json, :summary, :body_markdown,
                    :source_url, :source_file, :source_hash, :manual_fields_json, :last_seen_at, :created_at, :updated_at)""",
            {
                **values,
                "manual_fields_json": "[]",
                "last_seen_at": timestamp,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
    else:
        conn.execute(
            """UPDATE news_articles SET source=:source, page_name=:page_name, title=:title,
            published_at=:published_at, category=:category, tags_json=:tags_json, summary=:summary,
            body_markdown=:body_markdown, source_url=:source_url, source_file=:source_file,
            source_hash=:source_hash, last_seen_at=:last_seen_at, updated_at=:updated_at WHERE id=:id""",
            {**values, "last_seen_at": timestamp, "updated_at": timestamp if changed else old["updated_at"]},
        )

    tag_keys, _ = sync_news_article_tags(conn, record_id, values["tags_json"])
    tag_json = json.dumps(tag_keys, ensure_ascii=False)
    if tag_json != values["tags_json"]:
        conn.execute("UPDATE news_articles SET tags_json = ? WHERE id = ?", (tag_json, record_id))
        changed = True

    # Reconcile by identity, not DELETE+INSERT: stable image IDs keep existing
    # /api/news/images/{id} links valid across polls and local reimports.
    if "images" in record:
        images = record.get("images") or []
        managed_kinds = {"archive", "remote"} if record.get("source_file") else {"remote"}
        old_images = conn.execute("SELECT * FROM news_images WHERE news_id = ?", (record_id,)).fetchall()
        suppressed = {
            (row["kind"], row["local_path"] or row["source_url"])
            for row in conn.execute(
                "SELECT kind, local_path, source_url FROM news_image_suppressions WHERE news_id = ?", (record_id,)
            ).fetchall()
        }
        remaining = {row["id"]: row for row in old_images if row["kind"] in managed_kinds}
        seen = set()
        for position, image in enumerate(images):
            if not isinstance(image, dict):
                continue
            kind = str(image.get("kind") or "remote")
            if kind not in managed_kinds:
                continue
            local_path = str(image.get("local_path") or "").strip()
            source_url = str(image.get("source_url") or "").strip()
            identity = (kind, local_path or source_url)
            if not identity[1] or identity in seen or identity in suppressed:
                continue
            seen.add(identity)
            values = {
                "position": position,
                "local_path": local_path,
                "source_url": source_url,
                "alt_text": str(image.get("alt_text") or "").strip(),
                "width": int(image.get("width") or 0),
                "height": int(image.get("height") or 0),
                "bytes": int(image.get("bytes") or 0),
                "sha256": str(image.get("sha256") or ""),
            }
            duplicate = next(
                (row for row in old_images if (row["kind"], row["local_path"] or row["source_url"]) == identity), None
            )
            if duplicate:
                remaining.pop(duplicate["id"], None)
                if any(duplicate[key] != value for key, value in values.items()):
                    assignments = ", ".join(f"{key} = ?" for key in values)
                    conn.execute(
                        f"UPDATE news_images SET {assignments} WHERE id = ?", [*values.values(), duplicate["id"]]
                    )
                    changed = True
            else:
                keys = ", ".join(values)
                placeholders = ", ".join("?" for _ in values)
                conn.execute(
                    f"INSERT INTO news_images (news_id, kind, {keys}, created_at) VALUES (?, ?, {placeholders}, ?)",
                    [record_id, kind, *values.values(), timestamp],
                )
                changed = True
        if remaining:
            conn.executemany("DELETE FROM news_images WHERE id = ?", [(image_id,) for image_id in remaining])
            changed = True
    if changed:
        conn.execute("UPDATE news_articles SET updated_at = ? WHERE id = ?", (timestamp, record_id))
    return changed, old is None


__all__ = [
    "NEWS_ALLOWED_TAGS",
    "NEWS_CATEGORIES",
    "NEWS_EDITABLE_FIELDS",
    "NEWS_SCHEMA_SQL",
    "NEWS_SOURCES",
    "NEWS_TAG_CATALOG_ORDER",
    "NEWS_TAG_LABELS",
    "NEWS_SOURCE_LABELS",
    "NEWS_TOPICS_URL",
    "clean_tag_values",
    "ensure_news_schema",
    "image_references",
    "inferred_topic_tags",
    "metadata_field",
    "news_id",
    "normalize_news_date",
    "normalized_tags",
    "news_article_tag_ids",
    "news_tag_catalog",
    "news_tag_id_for_slug",
    "normalized_news_tag_labels",
    "normalized_news_tag_key",
    "resolve_news_tag_values",
    "sync_news_article_tags",
    "news_image_dimensions_allowed",
    "news_image_tag_dimensions",
    "news_image_tag_source",
    "parse_local_markdown",
    "parse_topic_detail",
    "NEWS_SUMMARY_MAX_LENGTH",
    "truncate_news_summary",
    "parse_topic_listing",
    "topic_next_offset",
    "upsert_news_record",
]
