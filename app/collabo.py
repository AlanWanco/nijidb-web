"""SQLite-backed collaboration illustration records and migration helpers."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

COLLABO_REVIEW_STATUSES = {"pending", "approved"}
COLLABO_COLLECTION_STATUSES = {"complete", "partial", "unavailable"}
COLLABO_CHARACTER_TAGS = (
    ("ayumu", ("上原歩夢", "上原步梦", "歩夢", "步梦", "Ayumu Uehara")),
    ("kasumi", ("中須かすみ", "中须霞", "かすみ", "霞", "Kasumi Nakasu")),
    ("shizuku", ("桜坂しずく", "樱坂雫", "しずく", "雫", "Shizuku Osaka")),
    ("karin", ("朝香果林", "果林", "Karin Asaka")),
    ("ai", ("宮下愛", "宫下爱", "愛", "爱", "Ai Miyashita")),
    ("kanata", ("近江彼方", "彼方", "Kanata Konoe")),
    ("setsuna", ("優木せつ菜", "优木雪菜", "中川菜々", "中川菜菜", "せつ菜", "雪菜", "Setsuna Yuki")),
    ("emma", ("エマ・ヴェルデ", "艾玛", "エマ", "Emma Verde")),
    ("rina", ("天王寺璃奈", "璃奈", "Rina Tennoji")),
    ("shioriko", ("三船栞子", "栞子", "Shioriko Mifune")),
    ("mia", ("ミア・テイラー", "米娅", "ミア", "Mia Taylor")),
    ("lanzhu", ("鐘嵐珠", "钟岚珠", "ランジュ", "嵐珠", "岚珠", "Lanzhu Zhong")),
    ("yu", ("高咲侑", "侑", "Yuu Takasaki", "Yuu")),
)
COLLABO_CHARACTER_TAG_IDS = tuple(tag_id for tag_id, _ in COLLABO_CHARACTER_TAGS)
COLLABO_IDOL_TAG_IDS = tuple(tag_id for tag_id in COLLABO_CHARACTER_TAG_IDS if tag_id != "yu")
COLLABO_INITIAL_NINE_TAG_IDS = (
    "ayumu",
    "kasumi",
    "shizuku",
    "karin",
    "ai",
    "kanata",
    "setsuna",
    "emma",
    "rina",
)
COLLABO_COMBINATION_GROUPS = (
    ("all", COLLABO_CHARACTER_TAG_IDS, ()),
    ("idol12", COLLABO_IDOL_TAG_IDS, ()),
    ("grade1", ("kasumi", "shizuku", "rina", "shioriko"), ()),
    ("grade2", ("ayumu", "ai", "setsuna", "lanzhu"), ()),
    ("grade3", ("karin", "kanata", "emma", "mia"), ()),
    ("movie1", ("ayumu", "shizuku", "kanata", "emma", "lanzhu"), ("kasumi", "yu")),
    ("movie2", ("ai", "rina", "setsuna", "shioriko", "mia"), ("karin",)),
    ("initial9", COLLABO_INITIAL_NINE_TAG_IDS, ()),
    ("anime10", COLLABO_INITIAL_NINE_TAG_IDS + ("yu",), ()),
    ("shioriko10", COLLABO_INITIAL_NINE_TAG_IDS + ("shioriko",), ()),
)
COLLABO_COMBINATION_GROUP_MAP = {group_id: (required, optional) for group_id, required, optional in COLLABO_COMBINATION_GROUPS}
COLLABO_CHARACTER_TAG_ALIASES = {
    alias.casefold(): tag_id
    for tag_id, aliases in COLLABO_CHARACTER_TAGS
    for alias in (tag_id, *aliases)
}
# collaboration_images.review_status remains in the schema for old databases only;
# image-level review is no longer part of the API or publishing workflow.
COLLABO_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS collaboration_items (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL DEFAULT '',
  date TEXT NOT NULL DEFAULT '',
  date_kind TEXT NOT NULL DEFAULT 'first_seen',
  partners_json TEXT NOT NULL DEFAULT '[]',
  credit TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  links_json TEXT NOT NULL DEFAULT '[]',
  tags_json TEXT NOT NULL DEFAULT '[]',
  periods_json TEXT NOT NULL DEFAULT '[]',
  collection_status TEXT NOT NULL DEFAULT 'unavailable',
  review_status TEXT NOT NULL DEFAULT 'pending',
  cover_image_id TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  source_hash TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_collaboration_items_date ON collaboration_items(date DESC, id);
CREATE INDEX IF NOT EXISTS idx_collaboration_items_review ON collaboration_items(review_status, date DESC);
CREATE TABLE IF NOT EXISTS collaboration_images (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  path TEXT NOT NULL DEFAULT '',
  public_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  source_page TEXT NOT NULL DEFAULT '',
  source_title TEXT NOT NULL DEFAULT '',
  caption TEXT NOT NULL DEFAULT '',
  alt TEXT NOT NULL DEFAULT '',
  width INTEGER NOT NULL DEFAULT 0,
  height INTEGER NOT NULL DEFAULT 0,
  bytes INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  score REAL NOT NULL DEFAULT 0,
  context TEXT NOT NULL DEFAULT '',
  review_status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(item_id) REFERENCES collaboration_items(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_collaboration_images_item ON collaboration_images(item_id, position, id);
CREATE TABLE IF NOT EXISTS collaboration_meta (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_collaboration_schema(conn) -> None:
    conn.executescript(COLLABO_SCHEMA_SQL)
    item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(collaboration_items)")}
    for column, definition in (
        ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("periods_json", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        if column not in item_columns:
            conn.execute(f"ALTER TABLE collaboration_items ADD COLUMN {column} {definition}")
    image_columns = {row["name"] for row in conn.execute("PRAGMA table_info(collaboration_images)")}
    if "public_url" not in image_columns:
        conn.execute("ALTER TABLE collaboration_images ADD COLUMN public_url TEXT NOT NULL DEFAULT ''")


def decode_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed


def json_dict(value: Any) -> dict[str, Any]:
    parsed = decode_json(value, value)
    return dict(parsed) if isinstance(parsed, dict) else {}


def text_list(value: Any) -> list[str]:
    parsed = decode_json(value, value)
    if isinstance(parsed, str):
        parsed = re.split(r"[,，、\n]+", parsed)
    if not isinstance(parsed, (list, tuple, set)):
        return []
    result: list[str] = []
    for entry in parsed:
        text = str(entry or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def collaboration_tags(value: Any) -> list[str]:
    parsed = decode_json(value, value)
    if isinstance(parsed, str):
        parsed = re.split(r"[,，、\n]+", parsed)
    if not isinstance(parsed, (list, tuple, set)):
        return []
    values = [part for entry in parsed for part in re.split(r"[,，、\n]+", str(entry or ""))]
    selected: set[str] = set()
    for entry in values:
        token = str(entry or "").strip()
        if not token:
            continue
        if token.casefold() in {"all", "全部", "全员"}:
            selected.update(COLLABO_CHARACTER_TAG_IDS)
            continue
        tag_id = COLLABO_CHARACTER_TAG_ALIASES.get(token.casefold())
        if tag_id:
            selected.add(tag_id)
    return [tag_id for tag_id in COLLABO_CHARACTER_TAG_IDS if tag_id in selected]


def collaboration_combination_group(value: Any) -> str:
    group_id = str(value or "").strip().casefold()
    return group_id if group_id in COLLABO_COMBINATION_GROUP_MAP else ""


def collaboration_combination_matches(value: Any, group: Any) -> bool:
    group_id = collaboration_combination_group(group)
    if not group_id:
        return False
    required_values, optional_values = COLLABO_COMBINATION_GROUP_MAP[group_id]
    selected = set(collaboration_tags(value))
    required = set(required_values)
    allowed = required | set(optional_values)
    return bool(selected) and required <= selected <= allowed


def collaboration_tag_search_text(value: Any) -> str:
    selected = set(collaboration_tags(value))
    parts: list[str] = []
    for tag_id, aliases in COLLABO_CHARACTER_TAGS:
        if tag_id in selected:
            parts.extend((tag_id, *aliases))
    return " ".join(parts)


def normalize_collaboration_periods(value: Any) -> list[dict[str, str]]:
    parsed = decode_json(value, value)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, (list, tuple)):
        return []
    periods: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        start_raw = str(entry.get("start_date") or entry.get("start") or "").strip()
        end_raw = str(entry.get("end_date") or entry.get("end") or "").strip()
        title = str(entry.get("title") or entry.get("label") or "").strip()
        description = str(entry.get("description") or entry.get("note") or "").strip()
        if not any((start_raw, end_raw, title, description)):
            continue
        if len(title) > 200 or len(description) > 2000:
            raise ValueError("时间段标题或描述过长")
        start = normalize_date(start_raw) if start_raw else ""
        end = normalize_date(end_raw) if end_raw else ""
        if start_raw and not start:
            raise ValueError("时间段开始日期格式无效")
        if end_raw and not end:
            raise ValueError("时间段结束日期格式无效")
        if not start or not end:
            raise ValueError("时间段需要填写开始和结束日期")
        if start > end:
            raise ValueError("时间段结束日期不能早于开始日期")
        if not title and not description:
            raise ValueError("时间段需要填写标题或描述")
        key = (start, end, title, description)
        if key not in seen:
            seen.add(key)
            periods.append({"start_date": start, "end_date": end, "title": title, "description": description})
    if len(periods) > 50:
        raise ValueError("最多只能添加 50 个时间段")
    return periods


def normalize_links(value: Any, *, strict: bool = False) -> list[dict[str, str]]:
    parsed = decode_json(value, value)
    if not isinstance(parsed, (list, tuple)):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in parsed:
        if isinstance(entry, str):
            url = entry.strip()
            title = ""
        elif isinstance(entry, dict):
            url = str(entry.get("url") or "").strip()
            title = str(entry.get("title") or entry.get("name") or "").strip()
        else:
            continue
        if not url:
            if strict:
                raise ValueError("相关页面需要填写有效的 HTTP/HTTPS 地址")
            continue
        if url in seen:
            continue
        if not valid_external_url(url):
            if strict:
                raise ValueError("相关页面需要填写有效的 HTTP/HTTPS 地址")
            # Legacy manifests are local input; never expose an invalid link
            # through the public API even if one was imported in the past.
            continue
        seen.add(url)
        result.append({"title": title, "url": url})
    return result


def normalize_date(value: Any) -> str:
    raw = str(value or "").strip()
    match = re.search(r"(?<!\d)(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})日?", raw)
    if match:
        year, month, day = match.groups()
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            return ""
    if re.fullmatch(r"20\d{6}", raw):
        try:
            return datetime.strptime(raw, "%Y%m%d").date().isoformat()
        except ValueError:
            return ""
    return ""


def valid_external_url(value: Any) -> bool:
    raw = str(value or "").strip()
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw):
        return False
    try:
        parsed = urlparse(raw)
        port = parsed.port
        decoded_path = unquote(parsed.path)
    except (TypeError, ValueError):
        return False
    hostname = parsed.hostname or ""
    return (
        parsed.scheme in {"http", "https"}
        and bool(hostname)
        and not any(character.isspace() for character in hostname)
        and (port is None or 1 <= port <= 65535)
        and not parsed.netloc.endswith(":")
        and not parsed.fragment
        and "#" not in raw
        and not ("?" in raw and not parsed.query)
        and not parsed.username
        and not parsed.password
        and "\\" not in raw
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded_path)
        and "?" not in decoded_path
        and "#" not in decoded_path
        and "\\" not in decoded_path
        and all(part not in {".", ".."} for part in decoded_path.split("/") if part)
        and all(part for part in decoded_path.split("/")[1:-1])
    )


def valid_slug(value: Any) -> bool:
    return bool(re.fullmatch(r"\d{8}-[a-f0-9]{6}", str(value or "").strip()))


def nonnegative_integer(value: Any, *, strict: bool = False) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool) or (
        isinstance(value, float) and (not math.isfinite(value) or not value.is_integer())
    ):
        if strict:
            raise ValueError("图片数值格式无效")
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        if strict:
            raise ValueError("图片数值格式无效") from exc
        return 0
    if number < 0 or number > 2**63 - 1:
        if strict:
            raise ValueError("图片数值超出范围")
        return 0
    return number


def finite_number(value: Any, *, strict: bool = False) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, bool):
        if strict:
            raise ValueError("图片评分格式无效")
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        if strict:
            raise ValueError("图片评分格式无效") from exc
        return 0.0
    if not math.isfinite(number):
        if strict:
            raise ValueError("图片评分格式无效")
        return 0.0
    return number


def image_asset_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    local_prefixes = (
        "/api/collabo/assets/",
        "/api/collaboration-illustrations/assets/",
        "/media/illustrations/",
        "api/collabo/assets/",
        "api/collaboration-illustrations/assets/",
        "media/illustrations/",
        "runtime:",
        "archive:",
    )
    try:
        parsed = urlparse(raw)
    except (TypeError, ValueError):
        return ""
    local_uri = any(
        raw.startswith(prefix) and not raw.startswith(f"{prefix}/")
        for prefix in ("runtime:", "archive:")
    )
    if (parsed.scheme or parsed.netloc) and not local_uri:
        return ""
    if parsed.query or parsed.fragment or "?" in raw or "#" in raw or "\\" in raw:
        return ""
    raw = unquote(parsed.path or raw)
    if "?" in raw or "#" in raw or "\\" in raw:
        return ""
    for prefix in local_prefixes:
        if raw.startswith(prefix):
            raw = raw.removeprefix(prefix)
            break
    raw = raw.lstrip("/")
    parts = raw.split("/") if raw else []
    if (
        not raw
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw)
    ):
        return ""
    return Path(raw).as_posix()


def image_id(item_id: str, path: str, source_url: str) -> str:
    identity = f"{item_id}\n{path}\n{source_url}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:32]


def item_slug(item_date: str, item_id: str, used: set[str] | None = None) -> str:
    del item_id  # The suffix is intentionally random and is not derived from source IDs.
    used = used or set()
    date_part = re.sub(r"\D", "", item_date)[:8] or "00000000"
    for _ in range(32):
        candidate = f"{date_part}-{secrets.token_hex(3)}"
        if candidate not in used:
            return candidate
    raise RuntimeError("无法生成唯一的联动永久链接")


def source_url_from_image(image: dict[str, Any], path: str) -> str:
    for key in ("source_url", "url"):
        value = str(image.get(key) or "").strip()
        if value and valid_external_url(value):
            return value
    return ""


def manifest_image_record(item_id: str, image: dict[str, Any], position: int, timestamp: str) -> dict[str, Any] | None:
    path = image_asset_path(image.get("path") or image.get("url"))
    source_url = source_url_from_image(image, path)
    if not path and not source_url:
        return None
    return {
        "id": image_id(item_id, path, source_url),
        "item_id": item_id,
        "position": position,
        "path": path,
        "public_url": "",
        "thumbnail_path": image_asset_path(image.get("thumbnail_path") or image.get("thumbnail_url")),
        "source_url": source_url,
        "source_page": (
            str(image.get("source_page") or "").strip()
            if valid_external_url(image.get("source_page"))
            else ""
        ),
        "source_title": str(image.get("source_title") or "").strip(),
        "caption": str(image.get("caption") or "").strip(),
        "alt": str(image.get("alt") or image.get("alt_text") or "").strip(),
        "width": nonnegative_integer(image.get("width")),
        "height": nonnegative_integer(image.get("height")),
        "bytes": nonnegative_integer(image.get("bytes")),
        "sha256": str(image.get("sha256") or "").strip(),
        "kind": str(image.get("kind") or "").strip(),
        "score": finite_number(image.get("score")),
        "context": str(image.get("context") or "").strip(),
        # Keep the legacy column consistent while treating every image as usable.
        "review_status": "approved",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _load_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _index_items(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for year in index.get("years", []):
        if not isinstance(year, dict):
            continue
        for item in year.get("items", []):
            if isinstance(item, dict) and item.get("id"):
                result[str(item["id"])] = item
    return result


def _meta_get(conn, key: str, fallback: Any = None) -> Any:
    row = conn.execute("SELECT value_json FROM collaboration_meta WHERE key = ?", (key,)).fetchone()
    return decode_json(row["value_json"], fallback) if row else fallback


def _meta_set_if_missing(conn, key: str, value: Any, timestamp: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO collaboration_meta(key, value_json, updated_at) VALUES (?, ?, ?)",
        (key, json.dumps(value, ensure_ascii=False), timestamp),
    )


def migrate_collaboration_sources(conn, index_path: Path | None = None, manifest_path: Path | None = None) -> dict[str, int]:
    """Import missing records from the local index/manifest without overwriting edits."""
    ensure_collaboration_schema(conn)
    index = _load_json(index_path)
    manifest = _load_json(manifest_path)
    manifest_items = manifest.get("items") if isinstance(manifest.get("items"), dict) else {}
    index_items = _index_items(index)
    item_ids = list(index_items)
    item_ids.extend(item_id for item_id in manifest_items if item_id not in index_items)
    if not item_ids:
        return {"items": 0, "images": 0}

    timestamp = now_iso()
    source = index.get("source") if isinstance(index.get("source"), dict) else {}
    if source:
        _meta_set_if_missing(conn, "source", source, timestamp)
    if manifest.get("generated_at"):
        _meta_set_if_missing(conn, "manifest_generated_at", manifest["generated_at"], timestamp)
    used_slugs = {row["slug"] for row in conn.execute("SELECT slug FROM collaboration_items")}
    imported_items = imported_images = 0
    for item_id in item_ids:
        raw_item = dict(index_items.get(item_id) or {})
        raw_manifest = manifest_items.get(item_id) if isinstance(manifest_items.get(item_id), dict) else {}
        manifest_images = raw_manifest.get("images") if isinstance(raw_manifest.get("images"), list) else []
        existing = conn.execute("SELECT * FROM collaboration_items WHERE id = ?", (item_id,)).fetchone()
        item_date = normalize_date(raw_item.get("date") or raw_item.get("first_seen") or raw_manifest.get("first_seen"))
        if not item_date:
            item_date = "0000-00-00"
        if not existing:
            slug = item_slug(item_date, item_id, used_slugs)
            used_slugs.add(slug)
            collection_status = str(raw_manifest.get("status") or "").strip()
            if collection_status not in COLLABO_COLLECTION_STATUSES:
                collection_status = "complete" if manifest_images else "unavailable"
            links = normalize_links(raw_item.get("links") or raw_item.get("official_links"))
            partners = text_list(raw_item.get("partners") or raw_item.get("collaboration"))
            raw_tags = raw_item["tags"] if "tags" in raw_item else raw_item.get("character_tags", [])
            raw_periods = raw_item["periods"] if "periods" in raw_item else raw_item.get("time_periods", [])
            try:
                periods = normalize_collaboration_periods(raw_periods)
            except ValueError:
                periods = []
            tags = collaboration_tags(raw_tags)
            conn.execute(
                """INSERT INTO collaboration_items
                (id, slug, title, date, date_kind, partners_json, credit, note, links_json, tags_json, periods_json,
                 collection_status, review_status, cover_image_id, metadata_json, source_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)""",
                (
                    item_id,
                    slug,
                    str(raw_item.get("title") or raw_manifest.get("title") or "").strip(),
                    item_date,
                    str(raw_item.get("date_kind") or "first_seen").strip(),
                    json.dumps(partners, ensure_ascii=False),
                    str(raw_item.get("credit") or "").strip(),
                    str(raw_item.get("note") or "").strip(),
                    json.dumps(links, ensure_ascii=False),
                    json.dumps(tags, ensure_ascii=False),
                    json.dumps(periods, ensure_ascii=False),
                    collection_status,
                    str(raw_item.get("review_status") or "").strip() if str(raw_item.get("review_status") or "").strip() in COLLABO_REVIEW_STATUSES else "pending",
                    json.dumps(
                        {
                            "source_index": raw_item,
                            "source_manifest": {key: value for key, value in raw_manifest.items() if key != "images"},
                        },
                        ensure_ascii=False,
                    ),
                    hashlib.sha256(json.dumps({"item": raw_item, "manifest": raw_manifest}, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                    timestamp,
                    timestamp,
                ),
            )
            imported_items += 1
        elif raw_item:
            # A container may boot once with only manifest.json available. If the
            # richer index appears later, fill only fields that were still empty;
            # never overwrite an editor's non-empty values.
            stored_metadata = json_dict(existing["metadata_json"])
            source_index = stored_metadata.get("source_index")
            if not isinstance(source_index, dict) or not source_index:
                source_links = normalize_links(raw_item.get("links") or raw_item.get("official_links"))
                source_partners = text_list(raw_item.get("partners") or raw_item.get("collaboration"))
                raw_tags = raw_item["tags"] if "tags" in raw_item else raw_item.get("character_tags", [])
                raw_periods = raw_item["periods"] if "periods" in raw_item else raw_item.get("time_periods", [])
                source_tags = collaboration_tags(raw_tags)
                try:
                    source_periods = normalize_collaboration_periods(raw_periods)
                except ValueError:
                    source_periods = []
                assignments: list[str] = []
                values: list[Any] = []
                if not existing["partners_json"] or not text_list(existing["partners_json"]):
                    if source_partners:
                        assignments.append("partners_json = ?")
                        values.append(json.dumps(source_partners, ensure_ascii=False))
                if not existing["credit"] and raw_item.get("credit"):
                    assignments.append("credit = ?")
                    values.append(str(raw_item["credit"]).strip())
                if not existing["note"] and raw_item.get("note"):
                    assignments.append("note = ?")
                    values.append(str(raw_item["note"]).strip())
                if not normalize_links(existing["links_json"]) and source_links:
                    assignments.append("links_json = ?")
                    values.append(json.dumps(source_links, ensure_ascii=False))
                if not collaboration_tags(existing["tags_json"]) and source_tags:
                    assignments.append("tags_json = ?")
                    values.append(json.dumps(source_tags, ensure_ascii=False))
                if not normalize_collaboration_periods(existing["periods_json"]) and source_periods:
                    assignments.append("periods_json = ?")
                    values.append(json.dumps(source_periods, ensure_ascii=False))
                if existing["date_kind"] == "first_seen" and raw_item.get("date_kind") in {"announced", "starts", "first_seen"}:
                    assignments.append("date_kind = ?")
                    values.append(str(raw_item["date_kind"]))
                stored_metadata = {
                    "source_index": raw_item,
                    "source_manifest": {key: value for key, value in raw_manifest.items() if key != "images"},
                }
                assignments.extend(["metadata_json = ?", "source_hash = ?", "updated_at = ?"])
                values.extend([
                    json.dumps(stored_metadata, ensure_ascii=False),
                    hashlib.sha256(json.dumps({"item": raw_item, "manifest": raw_manifest}, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                    timestamp,
                ])
                conn.execute(
                    f"UPDATE collaboration_items SET {', '.join(assignments)} WHERE id = ?",
                    [*values, item_id],
                )
        image_ids = []
        for position, raw_image in enumerate(manifest_images):
            if not isinstance(raw_image, dict):
                continue
            image = manifest_image_record(item_id, raw_image, position, timestamp)
            if not image:
                continue
            image_ids.append(image["id"])
            duplicate = conn.execute(
                """SELECT id FROM collaboration_images
                WHERE item_id = ? AND ((? != '' AND path = ?) OR (? != '' AND path = '' AND source_url = ?))
                LIMIT 1""",
                (item_id, image["path"], image["path"], image["source_url"], image["source_url"]),
            ).fetchone()
            if duplicate:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO collaboration_images
                (id, item_id, position, path, public_url, thumbnail_path, source_url, source_page, source_title,
                 caption, alt, width, height, bytes, sha256, kind, score, context, review_status, created_at, updated_at)
                VALUES (:id, :item_id, :position, :path, :public_url, :thumbnail_path, :source_url, :source_page, :source_title,
                        :caption, :alt, :width, :height, :bytes, :sha256, :kind, :score, :context, :review_status,
                        :created_at, :updated_at)""",
                image,
            )
            imported_images += 1
        if not existing and image_ids:
            conn.execute("UPDATE collaboration_items SET cover_image_id = ? WHERE id = ?", (image_ids[0], item_id))
    return {"items": imported_items, "images": imported_images}


def collaboration_years(conn) -> list[str]:
    return [
        row["year"]
        for row in conn.execute(
            "SELECT DISTINCT substr(date, 1, 4) AS year FROM collaboration_items WHERE date GLOB '20??-??-??' ORDER BY year DESC"
        ).fetchall()
        if row["year"]
    ]


def collaboration_year_values(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for entry in values:
        for year in str(entry or "").split(","):
            year = year.strip()
            if re.fullmatch(r"20\d{2}", year) and year not in result:
                result.append(year)
    return result


def collaboration_rows(conn, q: str = "", year: Any = "", tags: Any = "", group: Any = "") -> list[Any]:
    rows = conn.execute("SELECT * FROM collaboration_items ORDER BY CASE WHEN date = '' THEN 1 ELSE 0 END, date DESC, id DESC").fetchall()
    years = set(collaboration_year_values(year))
    group_id = collaboration_combination_group(group)
    normalized_tags = collaboration_tags(tags)
    selected_tags = set(normalized_tags) if len(normalized_tags) < len(COLLABO_CHARACTER_TAG_IDS) else set()
    keyword = str(q or "").strip().casefold()
    image_search: dict[str, str] = {}
    if keyword:
        image_rows = conn.execute(
            "SELECT item_id, caption, alt, source_page, source_title, source_url, context FROM collaboration_images"
        ).fetchall()
        for image in image_rows:
            item_id = str(image["item_id"])
            image_search[item_id] = " ".join(
                [
                    image_search.get(item_id, ""),
                    str(image["caption"] or ""),
                    str(image["alt"] or ""),
                    str(image["source_page"] or ""),
                    str(image["source_title"] or ""),
                    str(image["source_url"] or ""),
                    str(image["context"] or ""),
                ]
            )
    result = []
    for row in rows:
        if years and row["date"][:4] not in years:
            continue
        if selected_tags and not selected_tags.intersection(collaboration_tags(row["tags_json"])):
            continue
        if group_id and not collaboration_combination_matches(row["tags_json"], group_id):
            continue
        if keyword:
            searchable = " ".join(
                [
                    str(row["title"] or ""),
                    str(row["credit"] or ""),
                    str(row["note"] or ""),
                    " ".join(text_list(row["partners_json"])),
                    json.dumps(normalize_links(row["links_json"]), ensure_ascii=False),
                    collaboration_tag_search_text(row["tags_json"]),
                    json.dumps(normalize_collaboration_periods(row["periods_json"]), ensure_ascii=False),
                    str(row["metadata_json"] or ""),
                    image_search.get(str(row["id"]), ""),
                ]
            ).casefold()
            if keyword not in searchable:
                continue
        result.append(row)
    return result


def find_collaboration(conn, identifier: str, admin: bool = False):
    if admin:
        return conn.execute("SELECT * FROM collaboration_items WHERE id = ? OR slug = ? LIMIT 1", (identifier, identifier)).fetchone()
    return conn.execute("SELECT * FROM collaboration_items WHERE slug = ? LIMIT 1", (identifier,)).fetchone()


def collaboration_source(conn) -> dict[str, Any]:
    source = _meta_get(conn, "source", {})
    return source if isinstance(source, dict) else {}


def normalized_item_payload(payload: dict[str, Any], existing: Any = None) -> dict[str, Any]:
    title = str(payload.get("title") or (existing["title"] if existing else "")).strip()
    item_date = normalize_date(payload.get("date") or (existing["date"] if existing else ""))
    if not title:
        raise ValueError("标题不能为空")
    if not item_date:
        raise ValueError("日期格式无效")
    date_kind = str(payload.get("date_kind") or (existing["date_kind"] if existing else "first_seen")).strip()
    if date_kind not in {"announced", "starts", "first_seen"}:
        date_kind = "first_seen"
    review_status = str(payload.get("review_status") or (existing["review_status"] if existing else "pending")).strip()
    if review_status not in COLLABO_REVIEW_STATUSES:
        review_status = "pending"
    collection_status = str(payload.get("collection_status") or (existing["collection_status"] if existing else "unavailable")).strip()
    if collection_status not in COLLABO_COLLECTION_STATUSES:
        collection_status = "unavailable"
    links = normalize_links(
        payload.get("links") if "links" in payload else (existing["links_json"] if existing else []),
        strict="links" in payload,
    )
    for link in links:
        if len(link["title"]) > 500 or not valid_external_url(link["url"]):
            raise ValueError("相关页面需要填写有效的 HTTP/HTTPS 地址")
    partners = text_list(payload.get("partners") if "partners" in payload else (existing["partners_json"] if existing else []))
    tags_value = payload["tags"] if "tags" in payload else (
        payload.get("character_tags") if "character_tags" in payload else (existing["tags_json"] if existing else [])
    )
    tags = collaboration_tags(tags_value)
    if "periods" in payload:
        periods_value = payload["periods"]
    elif "start_date" in payload or "end_date" in payload:
        periods_value = [{"start_date": payload.get("start_date"), "end_date": payload.get("end_date")}]
    else:
        periods_value = existing["periods_json"] if existing else []
    periods = normalize_collaboration_periods(periods_value)
    credit_value = payload["credit"] if "credit" in payload else (existing["credit"] if existing else "")
    note_value = payload["note"] if "note" in payload else (existing["note"] if existing else "")
    if len(title) > 500 or len(partners) > 100 or any(len(value) > 200 for value in partners):
        raise ValueError("联动资料内容过长")
    return {
        "title": title,
        "date": item_date,
        "date_kind": date_kind,
        "partners": partners,
        "tags": tags,
        "periods": periods,
        "credit": str(credit_value or "").strip(),
        "note": str(note_value or "").strip(),
        "links": links,
        "collection_status": collection_status,
        "review_status": review_status,
    }


def upsert_collaboration_item(conn, payload: dict[str, Any]) -> str:
    ensure_collaboration_schema(conn)
    requested_id = str(payload.get("id") or "").strip()
    existing = conn.execute("SELECT * FROM collaboration_items WHERE id = ?", (requested_id,)).fetchone() if requested_id else None
    item_id = str(existing["id"] if existing else requested_id or secrets.token_hex(8))
    if not re.fullmatch(r"[a-f0-9]{16,64}", item_id):
        if existing:
            raise ValueError("联动记录 ID 无效")
        item_id = secrets.token_hex(8)
    normalized = normalized_item_payload(payload, existing)
    if existing:
        slug = existing["slug"]
    else:
        used = {row["slug"] for row in conn.execute("SELECT slug FROM collaboration_items WHERE id != ?", (item_id,))}
        requested_slug = str(payload.get("slug") or "").strip()
        slug = requested_slug if valid_slug(requested_slug) and requested_slug not in used else item_slug(normalized["date"], item_id, used)
    timestamp = now_iso()
    image_inputs = payload.get("images") if "images" in payload else None
    if image_inputs is None:
        image_inputs = [dict(row) for row in conn.execute("SELECT * FROM collaboration_images WHERE item_id = ? ORDER BY position, id", (item_id,)).fetchall()]
    if not isinstance(image_inputs, list):
        raise ValueError("图片数据格式无效")
    existing_images = {
        row["id"]: row
        for row in conn.execute("SELECT * FROM collaboration_images WHERE item_id = ?", (item_id,)).fetchall()
    }
    prepared: list[dict[str, Any]] = []
    id_mapping: dict[str, str] = {}
    seen_keys: set[str] = set()
    for position, raw_image in enumerate(image_inputs):
        if not isinstance(raw_image, dict):
            continue
        requested_image_id = str(raw_image.get("id") or "").strip()
        existing_image = existing_images.get(requested_image_id)
        raw_url = str(raw_image.get("url") or raw_image.get("path") or "").strip()
        asset_path = image_asset_path(raw_image.get("asset_path"))
        path = image_asset_path(raw_url)
        stored_source_url = existing_image["source_url"] if existing_image else ""
        stored_public_url = existing_image["public_url"] if existing_image else ""
        source_url = str(raw_image.get("source_url") or "").strip()
        if (
            not source_url
            and existing_image
            and (
                raw_url in {stored_public_url, stored_source_url}
                or path and path == str(existing_image["path"] or "")
            )
        ):
            source_url = stored_source_url
        public_url_value = str(
            raw_image["public_url"] if "public_url" in raw_image else stored_public_url or ""
        ).strip()
        if not path and asset_path and (
            raw_url.startswith("/api/collabo/assets/")
            or raw_url.startswith("/api/collaboration-illustrations/assets/")
            or (public_url_value and raw_url == public_url_value)
            or (existing_image and raw_url in {stored_public_url, stored_source_url})
        ):
            path = asset_path
        # A display URL may intentionally differ from source_url. Only discard
        # a cached public URL when the actual source identity changed, or when
        # the caller explicitly supplied a replacement/empty public_url.
        identity_url = source_url or raw_url
        if (
            existing_image
            and stored_public_url
            and "public_url" not in raw_image
            and identity_url not in {stored_public_url, stored_source_url}
        ):
            public_url_value = ""
        if public_url_value and not valid_external_url(public_url_value):
            raise ValueError("图片公开地址无效")
        if not path and not source_url and valid_external_url(raw_url):
            source_url = raw_url
        source_page = str(raw_image.get("source_page") or "").strip()
        if source_url and not valid_external_url(source_url):
            raise ValueError("图片来源地址无效")
        if source_page and not valid_external_url(source_page):
            raise ValueError("图片来源页面地址无效")
        if not path and not source_url:
            raise ValueError("图片需要填写本地路径或 HTTP/HTTPS 地址")
        key = f"path:{path}" if path else f"url:{source_url}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        actual_id = requested_image_id if requested_image_id in existing_images else image_id(item_id, path, source_url)
        id_mapping[requested_image_id] = actual_id
        existing_image = existing_images.get(actual_id)
        prepared.append(
            {
                "id": actual_id,
                "item_id": item_id,
                "position": len(prepared),
                "path": path,
                "public_url": public_url_value,
                "thumbnail_path": image_asset_path(raw_image.get("thumbnail_path") or raw_image.get("thumbnail_url")),
                "source_url": source_url,
                "source_page": source_page,
                "source_title": str(raw_image.get("source_title") or "").strip(),
                "caption": str(raw_image.get("caption") or "").strip(),
                "alt": str(raw_image.get("alt") or "").strip(),
                "width": nonnegative_integer(raw_image.get("width"), strict=True),
                "height": nonnegative_integer(raw_image.get("height"), strict=True),
                "bytes": nonnegative_integer(raw_image.get("bytes"), strict=True),
                "sha256": str(raw_image.get("sha256") or "").strip(),
                "kind": str(raw_image.get("kind") or "manual").strip(),
                "score": finite_number(raw_image.get("score"), strict=True),
                "context": str(raw_image.get("context") or "").strip(),
                # Kept only for compatibility with the existing SQLite schema.
                "review_status": "approved",
                "created_at": existing_images[actual_id]["created_at"] if actual_id in existing_images else timestamp,
                "updated_at": timestamp,
            }
        )
    requested_cover = str(payload.get("cover_image_id") or "").strip()
    cover_image_id = id_mapping.get(requested_cover, requested_cover)
    if cover_image_id not in {image["id"] for image in prepared}:
        cover_image_id = prepared[0]["id"] if prepared else ""
    metadata = json_dict(existing["metadata_json"] if existing else payload.get("metadata_json", {}))
    conn.execute(
        """INSERT INTO collaboration_items
        (id, slug, title, date, date_kind, partners_json, credit, note, links_json, tags_json, periods_json,
         collection_status, review_status, cover_image_id, metadata_json, source_hash, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET slug=excluded.slug, title=excluded.title, date=excluded.date,
          date_kind=excluded.date_kind, partners_json=excluded.partners_json, credit=excluded.credit,
          note=excluded.note, links_json=excluded.links_json, tags_json=excluded.tags_json,
          periods_json=excluded.periods_json, collection_status=excluded.collection_status,
          review_status=excluded.review_status, cover_image_id=excluded.cover_image_id,
          metadata_json=excluded.metadata_json, updated_at=excluded.updated_at""",
        (
            item_id,
            slug,
            normalized["title"],
            normalized["date"],
            normalized["date_kind"],
            json.dumps(normalized["partners"], ensure_ascii=False),
            normalized["credit"],
            normalized["note"],
            json.dumps(normalized["links"], ensure_ascii=False),
            json.dumps(normalized["tags"], ensure_ascii=False),
            json.dumps(normalized["periods"], ensure_ascii=False),
            normalized["collection_status"],
            normalized["review_status"],
            cover_image_id,
            json.dumps(metadata, ensure_ascii=False),
            existing["source_hash"] if existing else "",
            existing["created_at"] if existing else timestamp,
            timestamp,
        ),
    )
    keep_ids = {image["id"] for image in prepared}
    if keep_ids:
        placeholders = ",".join("?" for _ in keep_ids)
        conn.execute(f"DELETE FROM collaboration_images WHERE item_id = ? AND id NOT IN ({placeholders})", [item_id, *keep_ids])
    else:
        conn.execute("DELETE FROM collaboration_images WHERE item_id = ?", (item_id,))
    for image in prepared:
        conn.execute(
            """INSERT INTO collaboration_images
            (id, item_id, position, path, public_url, thumbnail_path, source_url, source_page, source_title,
             caption, alt, width, height, bytes, sha256, kind, score, context, review_status, created_at, updated_at)
            VALUES (:id, :item_id, :position, :path, :public_url, :thumbnail_path, :source_url, :source_page, :source_title,
                    :caption, :alt, :width, :height, :bytes, :sha256, :kind, :score, :context, :review_status,
                    :created_at, :updated_at)
            ON CONFLICT(id) DO UPDATE SET position=excluded.position, path=excluded.path,
              public_url=excluded.public_url, thumbnail_path=excluded.thumbnail_path, source_url=excluded.source_url, source_page=excluded.source_page,
              source_title=excluded.source_title, caption=excluded.caption, alt=excluded.alt, width=excluded.width,
              height=excluded.height, bytes=excluded.bytes, sha256=excluded.sha256, kind=excluded.kind,
              score=excluded.score, context=excluded.context, review_status=excluded.review_status,
              updated_at=excluded.updated_at""",
            image,
        )
    return item_id


__all__ = [
    "COLLABO_CHARACTER_TAG_IDS",
    "COLLABO_CHARACTER_TAGS",
    "COLLABO_COMBINATION_GROUPS",
    "COLLABO_IDOL_TAG_IDS",
    "COLLABO_INITIAL_NINE_TAG_IDS",
    "COLLABO_COLLECTION_STATUSES",
    "COLLABO_REVIEW_STATUSES",
    "COLLABO_SCHEMA_SQL",
    "collaboration_combination_group",
    "collaboration_combination_matches",
    "collaboration_rows",
    "collaboration_source",
    "collaboration_tag_search_text",
    "collaboration_tags",
    "collaboration_year_values",
    "collaboration_years",
    "ensure_collaboration_schema",
    "find_collaboration",
    "image_asset_path",
    "migrate_collaboration_sources",
    "normalize_collaboration_periods",
    "normalize_links",
    "normalized_item_payload",
    "text_list",
    "upsert_collaboration_item",
    "valid_external_url",
]
