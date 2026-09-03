from __future__ import annotations

import asyncio
import calendar
import hashlib
import hmac
import json
import mimetypes
import re
import os
import secrets
import shutil
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import boto3
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

SOURCE_URL = "https://www.lovelive-anime.jp/nijigasaki/cd.php"
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("DATA_DIR", "/data")) / "nijidb.sqlite3"
MEDIA_DIR = DB_PATH.parent / "images"
BACKUP_DIR = Path(os.getenv("DATABASE_BACKUP_DIR", str(DB_PATH.parent / "backups")))
COVER_CACHE_VERSION = "source-url-refresh-v1"
COVER_CACHE_VERSION_PATH = DB_PATH.parent / ".cover-cache-version"
FRONTEND_DIST = ROOT / "frontend" / "dist"
PASSWORD_ITERATIONS = 310_000
BACKUP_MAX_BYTES = 32 * 1024 * 1024
PROGRAM_JSON_FORMAT = "nijidb-program"
PROGRAM_JSON_VERSION = 2
PROGRAM_IMPORT_MAX_OCCURRENCES = 2000
R2_ENDPOINT = os.getenv("R2_ENDPOINT", "").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "nijidb").strip()
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL", "").strip().rstrip("/")
R2_IMAGE_PREFIX = os.getenv("R2_IMAGE_PREFIX", "images").strip("/")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(BACKUP_DIR, 0o700)
stop_event = asyncio.Event()
sync_lock = asyncio.Lock()
_r2_client: Any | None = None


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def backup_database_to(destination: Path) -> None:
    source = db()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source.close()


def create_persistent_database_backup(reason: str) -> Path:
    normalized_reason = re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-") or "manual"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = BACKUP_DIR / f"nijidb-backup-{timestamp}-{normalized_reason}.sqlite3"
    temporary_path = BACKUP_DIR / f".nijidb-backup-{secrets.token_hex(6)}.sqlite3"
    try:
        backup_database_to(temporary_path)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, backup_path)
    except Exception:
        remove_file(temporary_path)
        raise
    return backup_path


def backup_reason(filename: str) -> str:
    match = re.match(r"^nijidb-backup-\d{8}T\d{12}Z-(.+)\.sqlite3$", filename)
    return match.group(1) if match else "manual"


def list_database_backups() -> list[dict[str, Any]]:
    backups = []
    for path in BACKUP_DIR.glob("nijidb-backup-*.sqlite3"):
        try:
            stat = path.stat()
        except OSError:
            continue
        backups.append({
            "filename": path.name,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "reason": backup_reason(path.name),
        })
    return sorted(backups, key=lambda item: item["created_at"], reverse=True)


def resolve_database_backup(filename: str) -> Path:
    if Path(filename).name != filename or not filename.endswith(".sqlite3"):
        raise ValueError("数据库备份文件名无效")
    path = (BACKUP_DIR / filename).resolve()
    try:
        path.relative_to(BACKUP_DIR.resolve())
    except ValueError as exc:
        raise ValueError("数据库备份文件路径无效") from exc
    if not path.is_file():
        raise FileNotFoundError(filename)
    return path


def validate_database_backup(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()
    required_tables = {"settings", "releases", "sync_log"}
    if integrity != "ok":
        raise ValueError("数据库完整性校验失败")
    if not required_tables.issubset(tables):
        raise ValueError("备份文件不是 Nijidb 数据库")


def remove_database_sidecars() -> None:
    remove_file(Path(f"{DB_PATH}-wal"))
    remove_file(Path(f"{DB_PATH}-shm"))


def restore_database_file(source_path: Path) -> None:
    handle, raw_path = tempfile.mkstemp(prefix=".nijidb-restore-", suffix=".sqlite3", dir=DB_PATH.parent)
    os.close(handle)
    restore_path = Path(raw_path)
    try:
        shutil.copyfile(source_path, restore_path)
        os.chmod(restore_path, 0o600)
        remove_database_sidecars()
        os.replace(restore_path, DB_PATH)
    finally:
        remove_file(restore_path)


async def save_backup_upload(request: Request) -> Path:
    content_length = request.headers.get("content-length", "")
    try:
        if content_length and int(content_length) > BACKUP_MAX_BYTES:
            raise HTTPException(413, "备份文件不能超过 32 MB")
    except ValueError:
        pass

    handle, raw_path = tempfile.mkstemp(prefix=".nijidb-upload-", suffix=".sqlite3", dir=DB_PATH.parent)
    os.close(handle)
    upload_path = Path(raw_path)
    total = 0
    try:
        with upload_path.open("wb") as target:
            async for chunk in request.stream():
                total += len(chunk)
                if total > BACKUP_MAX_BYTES:
                    raise HTTPException(413, "备份文件不能超过 32 MB")
                target.write(chunk)
        if total == 0:
            raise HTTPException(400, "请选择有效的数据库备份文件")
        return upload_path
    except BaseException:
        remove_file(upload_path)
        raise


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (TypeError, ValueError):
        return False


def init_db() -> None:
    if os.getenv("ADMIN_SECRET", "change-me") == "change-me":
        print("WARNING: ADMIN_SECRET is using the insecure default")
    admin_password = os.getenv("ADMIN_PASSWORD")
    if not admin_password:
        admin_password = "admin"
        print("WARNING: ADMIN_PASSWORD is using the insecure default", flush=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS releases (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, subtitle TEXT, artist TEXT,
          release_date TEXT, price TEXT, cover_url TEXT, detail_html TEXT NOT NULL,
          source_url TEXT NOT NULL, fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL,
          position INTEGER NOT NULL DEFAULT 999999, tracks_json TEXT NOT NULL DEFAULT '[]',
          spec_json TEXT NOT NULL DEFAULT '{}', extras_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS sync_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TEXT NOT NULL,
          changed_count INTEGER NOT NULL, error TEXT
        );
        CREATE TABLE IF NOT EXISTS database_activity_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
          category TEXT NOT NULL, summary TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS programs (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'ongoing',
          category TEXT NOT NULL DEFAULT 'personal',
           format TEXT NOT NULL DEFAULT 'video',
           platform TEXT NOT NULL DEFAULT 'network',
           delivery TEXT NOT NULL DEFAULT 'recorded',
          auto_generate INTEGER NOT NULL DEFAULT 1,
          people TEXT NOT NULL DEFAULT '',
           official_url TEXT NOT NULL DEFAULT '',
           description TEXT NOT NULL DEFAULT '',
          frequency TEXT NOT NULL DEFAULT 'weekly',
          week_interval INTEGER NOT NULL DEFAULT 1,
          monthly_mode TEXT NOT NULL DEFAULT 'week',
          week_index INTEGER NOT NULL DEFAULT 0,
          weekday INTEGER NOT NULL DEFAULT 0,
           schedule_time TEXT NOT NULL DEFAULT '',
           start_date TEXT NOT NULL DEFAULT '',
           end_date TEXT NOT NULL DEFAULT '',
           parent_id TEXT NOT NULL DEFAULT '',
           subprogram_name TEXT NOT NULL DEFAULT '主节目',
           episode_start INTEGER NOT NULL DEFAULT 1,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL
         );
         CREATE TABLE IF NOT EXISTS program_periods (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           program_id TEXT NOT NULL,
           start_date TEXT NOT NULL,
           end_date TEXT NOT NULL DEFAULT '',
           frequency TEXT NOT NULL DEFAULT 'weekly',
           week_interval INTEGER NOT NULL DEFAULT 1,
           week_index INTEGER NOT NULL DEFAULT 0,
           weekday INTEGER NOT NULL DEFAULT 0,
           schedule_time TEXT NOT NULL DEFAULT '',
           timezone TEXT NOT NULL DEFAULT 'Asia/Tokyo',
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL,
           UNIQUE(program_id, start_date)
         );
         CREATE INDEX IF NOT EXISTS idx_program_periods_program ON program_periods(program_id, start_date);
         CREATE TABLE IF NOT EXISTS program_occurrences (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           program_id TEXT NOT NULL,
            original_date TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
             generated_date TEXT NOT NULL DEFAULT '',
             original_time TEXT NOT NULL DEFAULT '',
             delivery TEXT NOT NULL DEFAULT '',
             shift_following_days INTEGER NOT NULL DEFAULT 0,
             source_url TEXT NOT NULL DEFAULT '',
           mirror_url TEXT NOT NULL DEFAULT '',
           subtitle_url TEXT NOT NULL DEFAULT '',
           status TEXT NOT NULL DEFAULT 'scheduled',
           adjusted_date TEXT NOT NULL DEFAULT '',
           adjusted_time TEXT NOT NULL DEFAULT '',
           note TEXT NOT NULL DEFAULT '',
           guests TEXT NOT NULL DEFAULT '[]',
           special TEXT NOT NULL DEFAULT '',
           materialized INTEGER NOT NULL DEFAULT 0,
           created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(program_id, original_date)
        );
        CREATE INDEX IF NOT EXISTS idx_program_occurrences_program ON program_occurrences(program_id, original_date);
        """)
        program_columns = {row["name"] for row in conn.execute("PRAGMA table_info(programs)")}
        program_migrations = {
            "status": "TEXT NOT NULL DEFAULT 'ongoing'",
            "week_interval": "INTEGER NOT NULL DEFAULT 1",
            "monthly_mode": "TEXT NOT NULL DEFAULT 'week'",
            "auto_generate": "INTEGER NOT NULL DEFAULT 1",
            "parent_id": "TEXT NOT NULL DEFAULT ''",
            "subprogram_name": "TEXT NOT NULL DEFAULT '主节目'",
            "episode_start": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, definition in program_migrations.items():
            if name not in program_columns:
                conn.execute(f"ALTER TABLE programs ADD COLUMN {name} {definition}")
        conn.execute("UPDATE programs SET parent_id = '' WHERE parent_id IS NULL")
        conn.execute("UPDATE programs SET subprogram_name = '主节目' WHERE trim(coalesce(subprogram_name, '')) = ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_programs_parent ON programs(parent_id, title)")
        period_columns = {row["name"] for row in conn.execute("PRAGMA table_info(program_periods)")}
        if "timezone" not in period_columns:
            conn.execute("ALTER TABLE program_periods ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Asia/Tokyo'")
        occurrence_columns = {row["name"] for row in conn.execute("PRAGMA table_info(program_occurrences)")}
        if "title" not in occurrence_columns:
            conn.execute("ALTER TABLE program_occurrences ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        if "generated_date" not in occurrence_columns:
            conn.execute("ALTER TABLE program_occurrences ADD COLUMN generated_date TEXT NOT NULL DEFAULT ''")
        if "guests" not in occurrence_columns:
            conn.execute("ALTER TABLE program_occurrences ADD COLUMN guests TEXT NOT NULL DEFAULT '[]'")
        if "materialized" not in occurrence_columns:
            conn.execute("ALTER TABLE program_occurrences ADD COLUMN materialized INTEGER NOT NULL DEFAULT 0")
        if "special" not in occurrence_columns:
            conn.execute("ALTER TABLE program_occurrences ADD COLUMN special TEXT NOT NULL DEFAULT ''")
        if "source_url" not in occurrence_columns:
            conn.execute("ALTER TABLE program_occurrences ADD COLUMN source_url TEXT NOT NULL DEFAULT ''")
        if "mirror_url" not in occurrence_columns:
            conn.execute("ALTER TABLE program_occurrences ADD COLUMN mirror_url TEXT NOT NULL DEFAULT ''")
        if "subtitle_url" not in occurrence_columns:
            conn.execute("ALTER TABLE program_occurrences ADD COLUMN subtitle_url TEXT NOT NULL DEFAULT ''")
        if "delivery" not in occurrence_columns:
            conn.execute("ALTER TABLE program_occurrences ADD COLUMN delivery TEXT NOT NULL DEFAULT ''")
        if "shift_following_days" not in occurrence_columns:
            conn.execute("ALTER TABLE program_occurrences ADD COLUMN shift_following_days INTEGER NOT NULL DEFAULT 0")
        seed_program_periods(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(releases)")}
        if "position" not in columns:
            conn.execute("ALTER TABLE releases ADD COLUMN position INTEGER NOT NULL DEFAULT 999999")
        if "tracks_json" not in columns:
            conn.execute("ALTER TABLE releases ADD COLUMN tracks_json TEXT NOT NULL DEFAULT '[]'")
        if "spec_json" not in columns:
            conn.execute("ALTER TABLE releases ADD COLUMN spec_json TEXT NOT NULL DEFAULT '{}'")
        if "extras_json" not in columns:
            conn.execute("ALTER TABLE releases ADD COLUMN extras_json TEXT NOT NULL DEFAULT '[]'")
        defaults = {
            "interval_minutes": os.getenv("CHECK_INTERVAL_MINUTES", "10"),
            "detail_interval_minutes": os.getenv("DETAIL_CHECK_INTERVAL_MINUTES", "5"),
            "onebot_url": os.getenv("ONEBOT_URL", ""),
            "onebot_token": os.getenv("ONEBOT_TOKEN", ""),
            "onebot_target": os.getenv("ONEBOT_TARGET", ""),
            "onebot_profile": os.getenv("ONEBOT_PROFILE", "bot"),
            "admin_password_hash": hash_password(admin_password),
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (key, value))


def settings() -> dict[str, str]:
    with db() as conn:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def public_settings() -> dict[str, str]:
    values = settings()
    values.pop("admin_password_hash", None)
    return values


def log_database_activity(category: str, summary: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO database_activity_log(created_at, category, summary) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), category, summary),
        )


def recent_database_logs(limit: int = 20) -> list[dict[str, Any]]:
    with db() as conn:
        sync_rows = conn.execute(
            "SELECT id, checked_at, changed_count, error FROM sync_log WHERE changed_count > 0 OR (error IS NOT NULL AND error != '') ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        activity_rows = conn.execute(
            "SELECT id, created_at, category, summary FROM database_activity_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    logs = [
        {
            "id": f"music-{row['id']}",
            "checked_at": row["checked_at"],
            "category": "music",
            "summary": f"发现 {row['changed_count']} 项变化",
            "changed_count": row["changed_count"],
            "error": row["error"],
        }
        for row in sync_rows
    ]
    logs.extend(
        {
            "id": f"program-{row['id']}",
            "checked_at": row["created_at"],
            "category": row["category"],
            "summary": row["summary"],
            "changed_count": 1,
            "error": None,
        }
        for row in activity_rows
    )
    logs.sort(key=lambda row: row["checked_at"], reverse=True)
    return logs[:limit]


def admin_cookie_value(password_hash: str) -> str:
    secret = os.getenv("ADMIN_SECRET", "change-me")
    return hmac.new(secret.encode(), f"admin:{password_hash}".encode(), hashlib.sha256).hexdigest()


def decode_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def release_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    payload["tracks"] = decode_json(payload.pop("tracks_json", ""), [])
    payload["specs"] = decode_json(payload.pop("spec_json", ""), {})
    payload["extras"] = decode_json(payload.pop("extras_json", ""), [])
    return payload


def release_summary(row: sqlite3.Row) -> dict[str, Any]:
    payload = release_payload(row)
    payload.pop("detail_html", None)
    payload.pop("fingerprint", None)
    payload.pop("tracks", None)
    payload.pop("specs", None)
    payload.pop("extras", None)
    return payload


JAPAN_TZ = ZoneInfo("Asia/Tokyo")
PROGRAM_CATEGORIES = {"official", "personal"}
PROGRAM_FORMATS = {"video", "radio"}
PROGRAM_PLATFORMS = {"tv", "network"}
PROGRAM_DELIVERIES = {"live", "recorded"}
PROGRAM_FREQUENCIES = {"weekly", "monthly", "individual", "single"}
OCCURRENCE_STATUSES = {"scheduled", "rescheduled", "cancelled", "deleted"}
PROGRAM_FORECAST_DAYS = 183
PROGRAM_TIMEZONES = {
    "Asia/Tokyo": "东京时间",
    "Asia/Shanghai": "中国标准时间",
    "Asia/Seoul": "韩国时间",
    "UTC": "协调世界时",
    "America/Los_Angeles": "美国太平洋时间",
    "America/New_York": "美国东部时间",
}
BILIBILI_BV_PATTERN = re.compile(r"(?<![A-Za-z0-9])(BV[0-9A-Za-z]{10})(?![A-Za-z0-9])", re.IGNORECASE)


def boolean_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def integer_value(value: Any, label: str, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{label}范围为 {minimum}–{maximum}")
    return result


def occurrence_shift_days(value: Any) -> int:
    try:
        value = int(value or 0)
        return value if value in {-7, 7} else 0
    except (TypeError, ValueError):
        return 0


def normalized_people(value: Any) -> str:
    source = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            source = parsed if isinstance(parsed, list) else value
        except json.JSONDecodeError:
            pass
    if not isinstance(source, list):
        source = re.split(r"[,，、\n]+", str(source or ""))
    people: list[str] = []
    for item in source:
        name = str(item or "").strip()
        if name and name not in people:
            people.append(name)
    return json.dumps(people, ensure_ascii=False)


def normalized_program_link(value: Any, label: str, allow_bilibili_id: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if allow_bilibili_id and BILIBILI_BV_PATTERN.fullmatch(raw):
        return f"BV{raw[2:]}"
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label}需要填写完整的 HTTP/HTTPS 地址或有效的 BV 号")
    return raw


def program_people(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]") if isinstance(value, str) else value
    except json.JSONDecodeError:
        parsed = value
    if not isinstance(parsed, list):
        parsed = re.split(r"[,，、\n]+", str(parsed or ""))
    return [str(item).strip() for item in parsed if str(item).strip()]


def occurrence_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["title"] = str(payload.get("title") or "").strip()
    payload["guests"] = program_people(payload.get("guests", "[]"))
    payload["special"] = str(payload.get("special") or "").strip().upper()
    delivery = str(payload.get("delivery") or "").strip()
    payload["delivery"] = delivery if delivery in PROGRAM_DELIVERIES else ""
    payload["shift_following_days"] = occurrence_shift_days(payload.get("shift_following_days"))
    for key in ("source_url", "mirror_url", "subtitle_url"):
        payload[key] = str(payload.get(key) or "").strip()
    payload["materialized"] = boolean_value(payload.get("materialized"), False)
    if payload.get("status") == "scheduled" and payload.get("adjusted_date"):
        payload["status"] = "rescheduled"
    return payload


def period_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["week_interval"] = int(payload.get("week_interval") or 1)
    payload["week_index"] = int(payload.get("week_index") or 0)
    payload["weekday"] = int(payload.get("weekday") or 0)
    payload["timezone"] = payload.get("timezone") or "Asia/Tokyo"
    return payload


def occurrence_timezone(value: Any) -> ZoneInfo:
    timezone = str(value or "Asia/Tokyo")
    return ZoneInfo(timezone if timezone in PROGRAM_TIMEZONES else "Asia/Tokyo")


def legacy_period(values: dict[str, Any]) -> dict[str, Any]:
    frequency = str(values.get("frequency") or "weekly").strip()
    if frequency == "irregular" or (frequency == "monthly" and values.get("monthly_mode") == "irregular"):
        frequency = "single"
    week_index = int(values.get("week_index") or 0)
    start_date = str(values.get("start_date") or "").strip()
    end_date = str(values.get("end_date") or "").strip()
    if frequency == "single":
        end_date = start_date
        week_index = 0
    return {
        "start_date": start_date,
        "end_date": end_date,
        "frequency": frequency,
        "week_interval": int(values.get("week_interval") or 1),
        "week_index": week_index,
        "weekday": int(values.get("weekday") or 0),
        "schedule_time": str(values.get("schedule_time") or "").strip(),
        "timezone": str(values.get("timezone") or "Asia/Tokyo").strip(),
    }


def program_payload(
    row: sqlite3.Row | dict[str, Any],
    occurrences: list[dict[str, Any]] | None = None,
    periods: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = dict(row)
    payload.pop("duration_minutes", None)
    payload.pop("episode_count", None)
    payload.pop("mirror_url", None)
    payload.pop("subtitle_url", None)
    payload["people"] = program_people(payload.get("people", ""))
    payload["auto_generate"] = boolean_value(payload.get("auto_generate"), True)
    payload["week_interval"] = int(payload.get("week_interval") or 1)
    payload["week_index"] = int(payload.get("week_index") or 0)
    payload["weekday"] = int(payload.get("weekday") or 0)
    payload["parent_id"] = str(payload.get("parent_id") or "").strip()
    payload["subprogram_name"] = str(payload.get("subprogram_name") or "主节目").strip() or "主节目"
    payload["episode_start"] = 0 if str(payload.get("episode_start", 1)).strip() == "0" else 1
    payload["occurrences"] = occurrences or []
    payload["periods"] = periods if periods else ([legacy_period(payload)] if payload.get("start_date") else [])
    payload["timezone"] = payload["periods"][0].get("timezone", "Asia/Tokyo") if payload["periods"] else "Asia/Tokyo"
    payload["status"] = inferred_program_status(payload)
    payload["episode_count"] = program_episode_count(payload)
    payload["update_status"] = program_update_status(payload)
    return payload


def inferred_program_status(program: dict[str, Any]) -> str:
    if program.get("end_date"):
        return "completed"
    periods = program.get("periods", [])
    single_periods = [period for period in periods if period.get("frequency") == "single" and period.get("start_date")]
    if periods and len(single_periods) == len(periods):
        latest_period = max(single_periods, key=lambda period: period["start_date"])
        latest = date.fromisoformat(latest_period["start_date"])
        return "completed" if latest < datetime.now(occurrence_timezone(latest_period.get("timezone"))).date() else "ongoing"
    return "ongoing"


def program_display_name(program: dict[str, Any]) -> str:
    if not program.get("parent_id"):
        return program["title"]
    subprogram_name = str(program.get("subprogram_name") or "").strip()
    return f"{program['title']} · {subprogram_name}" if subprogram_name else program["title"]


def normalized_period(values: dict[str, Any], program_start: date, program_end: date | None) -> dict[str, Any]:
    start_date = str(values.get("start_date") or "").strip()
    if not start_date:
        raise ValueError("每个时期都需要填写开始日期")
    frequency = str(values.get("frequency") or "weekly").strip()
    if frequency == "irregular":
        frequency = "single"
    if frequency not in PROGRAM_FREQUENCIES:
        raise ValueError("时期更新方式无效")
    try:
        parsed_start = date.fromisoformat(start_date)
        parsed_end = date.fromisoformat(str(values.get("end_date") or "").strip()) if values.get("end_date") else None
    except ValueError as exc:
        raise ValueError("时期日期格式无效") from exc
    if parsed_start < program_start:
        raise ValueError("时期开始日期不能早于节目开始日期")
    if program_end and parsed_start > program_end:
        raise ValueError("时期开始日期不能晚于节目结束日期")
    if frequency == "single":
        parsed_end = parsed_start
    if parsed_end and parsed_start > parsed_end:
        raise ValueError("时期开始日期不能晚于时期结束日期")
    if program_end and parsed_end and parsed_end > program_end:
        raise ValueError("时期结束日期不能晚于节目结束日期")
    weekday = integer_value(values.get("weekday"), "星期", 0, 6)
    week_interval = integer_value(values.get("week_interval"), "每隔几周", 1, 52)
    week_index = integer_value(values.get("week_index"), "第几周", -5, 5)
    if frequency == "monthly" and week_index == 0:
        raise ValueError("固定月更需要填写第几周，支持 1–5 或 -1–-5")
    if frequency != "weekly":
        week_interval = 1
    if frequency != "monthly":
        week_index = 0
    schedule_time = str(values.get("schedule_time") or "").strip()
    if schedule_time and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule_time):
        raise ValueError("播出时间格式应为 HH:MM")
    timezone = str(values.get("timezone") or "Asia/Tokyo").strip()
    if timezone not in PROGRAM_TIMEZONES:
        raise ValueError("更新时间时区无效")
    return {
        "start_date": parsed_start.isoformat(),
        "end_date": parsed_end.isoformat() if parsed_end else "",
        "frequency": frequency,
        "week_interval": week_interval,
        "week_index": week_index,
        "weekday": weekday,
        "schedule_time": schedule_time,
        "timezone": timezone,
    }


def normalized_periods(values: dict[str, Any], program_start: date, program_end: date | None) -> list[dict[str, Any]]:
    raw_periods = values.get("periods")
    if not isinstance(raw_periods, list) or not raw_periods:
        raw_periods = [legacy_period(values)]
    if not all(isinstance(item, dict) for item in raw_periods):
        raise ValueError("时期格式无效")
    periods = [normalized_period(item, program_start, program_end) for item in raw_periods]
    periods.sort(key=lambda item: item["start_date"])
    if len({item["start_date"] for item in periods}) != len(periods):
        raise ValueError("时期开始日期不能相同")
    for index, period in enumerate(periods):
        next_period = periods[index + 1] if index + 1 < len(periods) else None
        if next_period:
            next_start = date.fromisoformat(next_period["start_date"])
            if not period["end_date"]:
                period["end_date"] = (next_start - timedelta(days=1)).isoformat()
            if date.fromisoformat(period["end_date"]) >= next_start:
                raise ValueError("时期之间不能重叠")
        elif not period["end_date"] and program_end:
            period["end_date"] = program_end.isoformat()
    return periods


def normalized_program(values: dict[str, Any]) -> dict[str, Any]:
    title = str(values.get("title") or "").strip()
    if not title:
        raise ValueError("节目名称不能为空")
    category = str(values.get("category") or "personal").strip()
    if category not in PROGRAM_CATEGORIES:
        raise ValueError("节目类型无效")
    program_format = str(values.get("format") or "video").strip()
    if program_format not in PROGRAM_FORMATS:
        raise ValueError("节目形式无效")
    platform = str(values.get("platform") or "network").strip()
    if platform not in PROGRAM_PLATFORMS:
        raise ValueError("播出平台无效")
    delivery = str(values.get("delivery") or "recorded").strip()
    if delivery not in PROGRAM_DELIVERIES:
        raise ValueError("播放方式无效")
    auto_generate = boolean_value(values.get("auto_generate"), True)
    start_date = str(values.get("start_date") or "").strip()
    end_date = str(values.get("end_date") or "").strip()
    parent_id = str(values.get("parent_id") or "").strip()
    subprogram_name = str(values.get("subprogram_name") or "").strip()
    raw_episode_start = values.get("episode_start", 1)
    episode_start = 1 if raw_episode_start in (None, "") else integer_value(raw_episode_start, "首集编号", 0, 1)
    if not parent_id:
        subprogram_name = "主节目"
    elif not subprogram_name or subprogram_name == "主节目":
        raise ValueError("子节目名称不能为空且不能使用“主节目”")
    raw_periods = values.get("periods")
    if not start_date and isinstance(raw_periods, list) and raw_periods and isinstance(raw_periods[0], dict):
        start_date = str(raw_periods[0].get("start_date") or "").strip()
    if not start_date:
        raise ValueError("开始日期不能为空")
    try:
        parsed_start = date.fromisoformat(start_date)
        parsed_end = date.fromisoformat(end_date) if end_date else None
    except ValueError as exc:
        raise ValueError("开始日期或结束日期格式无效") from exc
    single_period_set = isinstance(raw_periods, list) and bool(raw_periods) and all(
        isinstance(item, dict) and str(item.get("frequency") or "") in {"single", "irregular"}
        for item in raw_periods
    )
    if single_period_set:
        end_date = ""
        parsed_end = None
    elif not end_date and isinstance(raw_periods, list) and raw_periods and isinstance(raw_periods[-1], dict):
        candidate_end = str(raw_periods[-1].get("end_date") or "").strip()
        if candidate_end:
            try:
                parsed_end = date.fromisoformat(candidate_end)
            except ValueError as exc:
                raise ValueError("时期日期格式无效") from exc
            end_date = candidate_end
    if parsed_end and parsed_start > parsed_end:
        raise ValueError("开始日期不能晚于结束日期")
    periods = normalized_periods(values, parsed_start, parsed_end)
    first_period = periods[0]
    all_single = all(period["frequency"] == "single" for period in periods)
    normalized_end = "" if all_single else periods[-1]["end_date"]
    status = inferred_program_status({"end_date": normalized_end, "periods": periods})
    return {
        "title": title,
        "status": status,
        "category": category,
        "format": program_format,
        "platform": platform,
        "delivery": delivery,
        "auto_generate": auto_generate,
        "people": normalized_people(values.get("people")),
        "official_url": normalized_program_link(values.get("official_url"), "相关链接"),
        "description": str(values.get("description") or "").strip(),
        "frequency": first_period["frequency"],
        "week_interval": first_period["week_interval"],
        "monthly_mode": "week",
        "week_index": first_period["week_index"],
        "weekday": first_period["weekday"],
        "schedule_time": first_period["schedule_time"],
        "start_date": periods[0]["start_date"],
        "end_date": normalized_end,
        "parent_id": parent_id,
        "subprogram_name": subprogram_name,
        "episode_start": episode_start,
        "periods": periods,
    }


def import_choice(value: Any, aliases: dict[str, str]) -> str:
    raw = str(value or "").strip()
    return aliases.get(raw.lower(), aliases.get(raw, raw))


def import_date(value: Any, label: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = re.split(r"[T ]", raw, maxsplit=1)[0]
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label}格式无效：{raw}") from exc


def import_payload_options(payload: dict[str, Any]) -> dict[str, str]:
    raw_program = payload.get("program") if isinstance(payload.get("program"), dict) else payload
    raw_options = payload.get("import_options")
    if raw_options is None:
        raw_options = {}
    if not isinstance(raw_options, dict):
        raise ValueError("import_options 必须是对象")

    try:
        version = int(payload.get("_version") or 1)
    except (TypeError, ValueError):
        version = 1
    schedule_mode = import_choice(raw_options.get("schedule_mode"), {
        "逐期准确": "individual", "逐期数据": "individual", "individual": "individual",
        "自动生成": "generated", "规则生成": "generated", "generated": "generated",
    })
    if not schedule_mode:
        schedule_mode = "generated" if version < 2 else "individual"
    if schedule_mode not in {"individual", "generated"}:
        raise ValueError("import_options.schedule_mode 必须是 individual 或 generated")

    target_mode = import_choice(raw_options.get("target_mode"), {
        "新建": "new", "new": "new", "覆盖": "overwrite", "覆盖已有": "overwrite", "overwrite": "overwrite",
    }) or "new"
    if target_mode not in {"new", "overwrite"}:
        raise ValueError("import_options.target_mode 必须是 new 或 overwrite")
    source_program_id = str(raw_program.get("id") or "").strip() if isinstance(raw_program, dict) else ""
    target_program_id = str(raw_options.get("target_program_id") or "").strip()
    return {
        "schedule_mode": schedule_mode,
        "target_mode": target_mode,
        "target_program_id": target_program_id,
        "source_program_id": source_program_id,
    }


def program_json_metadata() -> dict[str, Any]:
    return {
        "_format": PROGRAM_JSON_FORMAT,
        "_version": PROGRAM_JSON_VERSION,
        "_description": "Nijidb 单独节目导入导出格式；一个 JSON 文件只描述一个节目及其单集资料。",
        "_field_notes": {
            "import_options.schedule_mode": "individual（默认）：以 occurrences 为最终逐期数据，不自动生成；generated：按 periods 自动生成，occurrences 只作为已保存的覆盖和例外。",
            "import_options.target_mode": "new（默认）：新建节目；overwrite：覆盖 target_program_id 指定的已有节目。覆盖前必须在网页预览中再次确认。",
            "program.id": "导出时保留的节目 ID，仅用于预览匹配覆盖目标；新建导入时会忽略。",
            "program": "节目基本资料和排期时期；periods 可以有多个，按 start_date 分段。",
            "program.delivery": "默认播出方式：live 或 recorded。",
            "program.periods[].frequency": "weekly、monthly、individual 或 single。",
            "program.periods[].week_interval": "周更间隔；填写 2 表示隔周。",
            "program.periods[].week_index": "固定月更的第几周，1–5 表示顺数，-1–-5 表示倒数。",
            "occurrences[].original_date": "单集原定日期，必填；支持 YYYY-MM-DD。",
            "occurrences[].title": "单集标题，可选；有值时会显示在日历和单集详情中。",
            "occurrences[].adjusted_date": "改期后的实际日期；status 为 rescheduled 时必填。",
            "occurrences[].adjusted_time": "改期后的时间；留空表示沿用原定时间。",
            "occurrences[].shift_following_days": "改期后后续隔周排期的偏移，只能填写 -7、0 或 7；只有 rescheduled 单集可以填写。individual 模式不会再次级联。",
            "occurrences[].generated": "导出标记；true 表示本期由 periods 自动生成，false 表示已有保存记录。generated 模式导入时，未补充内容的 generated=true 记录不会重复保存，仍由 periods 生成；补充内容后会保存为覆盖。",
            "occurrences[].materialized": "true 表示自动生成单集曾被保存为数据库记录；仅作来源说明。",
            "occurrences[].delivery": "本期播出方式：live、recorded 或空字符串表示跟随节目默认。",
            "occurrences[].special": "普通单集使用空字符串，EX 单集使用 EX。",
            "occurrences[].status": "scheduled、rescheduled、cancelled 或 deleted。deleted 会保留为不显示的删除记录。",
            "occurrences[].guests": "本期临时嘉宾数组，不会修改节目固定成员。",
        },
        "_import_notes": [
            "schedule_mode 缺省为 individual：导入的 occurrences 是准确的最终逐期数据，program.auto_generate 会被关闭，不会凭 periods 生成额外单集。",
            "需要继续按排期规则生成时，将 schedule_mode 设置为 generated；此时 program.auto_generate 会开启，occurrences 作为已保存覆盖和例外。",
            "导出默认是 individual 完整逐期快照，适合交给 AI 优化内容后覆盖导回；也可以选择 generated 规则加当前自动生成结果和例外导出。自动生成结果按系统现有约半年的生成窗口写入。",
            "target_mode 缺省为 new；覆盖导入必须指定 target_program_id，并在网页导入预览中明确选择覆盖目标。",
            "JSON 可以保留这些说明字段；导入器也兼容 // 和 /* */ 注释。",
        ],
    }


def program_json_template() -> dict[str, Any]:
    payload = program_json_metadata()
    payload["import_options"] = {
        "schedule_mode": "individual",
        "target_mode": "new",
        "target_program_id": "",
    }
    payload["program"] = {
        "title": "示例节目",
        "category": "personal",
        "format": "video",
        "platform": "network",
        "delivery": "recorded",
        "auto_generate": False,
        "episode_start": 1,
        "people": ["成员姓名"],
        "official_url": "https://example.com/program",
        "description": "节目简介。",
        "periods": [
            {
                "start_date": "2026-01-01",
                "end_date": "2026-06-30",
                "frequency": "weekly",
                "week_interval": 2,
                "week_index": 0,
                "weekday": 2,
                "schedule_time": "20:00",
                "timezone": "Asia/Tokyo",
            },
            {
                "start_date": "2026-07-01",
                "end_date": "",
                "frequency": "monthly",
                "week_interval": 1,
                "week_index": 1,
                "weekday": 2,
                "schedule_time": "20:00",
                "timezone": "Asia/Tokyo",
            },
        ],
    }
    payload["occurrences"] = [
        {
            "original_date": "2026-01-07",
            "title": "普通单集示例标题",
            "original_time": "20:00",
            "delivery": "live",
            "status": "scheduled",
            "special": "",
            "generated": False,
            "materialized": False,
            "source_url": "https://example.com/episode-1",
            "mirror_url": "",
            "subtitle_url": "",
            "note": "普通单集示例。",
            "guests": ["本期嘉宾"],
        },
        {
            "original_date": "2026-02-04",
            "title": "顺延单集示例标题",
            "original_time": "20:00",
            "delivery": "recorded",
            "status": "rescheduled",
            "adjusted_date": "2026-02-11",
            "adjusted_time": "20:00",
            "shift_following_days": 7,
            "special": "",
            "generated": False,
            "materialized": False,
            "source_url": "https://example.com/episode-2",
            "mirror_url": "",
            "subtitle_url": "",
            "note": "本期顺延一周，后续隔周排期同步顺延。",
            "guests": [],
        },
        {
            "original_date": "2026-02-18",
            "title": "取消单集示例标题",
            "original_time": "20:00",
            "delivery": "",
            "status": "cancelled",
            "special": "",
            "generated": False,
            "materialized": False,
            "source_url": "",
            "mirror_url": "",
            "subtitle_url": "",
            "note": "因故取消，保留记录。",
            "guests": [],
        },
        {
            "original_date": "2026-02-25",
            "title": "EX 特别单集示例标题",
            "original_time": "20:00",
            "delivery": "",
            "status": "deleted",
            "special": "EX",
            "generated": False,
            "materialized": False,
            "source_url": "https://example.com/episode-ex",
            "mirror_url": "",
            "subtitle_url": "",
            "note": "EX 特别单集示例。",
            "guests": [],
        },
    ]
    return payload


def normalize_import_periods(values: dict[str, Any]) -> dict[str, Any]:
    source = dict(values)
    source["frequency"] = import_choice(source.get("frequency"), {
        "每周": "weekly", "周更": "weekly", "weekly": "weekly",
        "每月": "monthly", "月更": "monthly", "monthly": "monthly",
        "逐期设置": "individual", "individual": "individual",
        "单次": "single", "single": "single", "irregular": "single",
    })
    if isinstance(source.get("weekday"), str):
        source["weekday"] = import_choice(source["weekday"], {
            "周一": "0", "周二": "1", "周三": "2", "周四": "3",
            "周五": "4", "周六": "5", "周日": "6",
        })
    if source["frequency"] == "monthly" and "week_index" not in source:
        number = int(source.get("week_number") or 1)
        source["week_index"] = -number if str(source.get("week_direction") or "first") in {"last", "倒数"} else number
    return source


def normalize_import_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    if not isinstance(payload, dict):
        raise ValueError("JSON 根对象格式无效")
    options = import_payload_options(payload)
    raw_program = payload.get("program") if isinstance(payload.get("program"), dict) else payload
    if not isinstance(raw_program, dict):
        raise ValueError("JSON 中缺少 program 对象")
    program_source = dict(raw_program)
    raw_occurrences = payload.get("occurrences")
    if raw_occurrences is None:
        raw_occurrences = payload.get("episodes", program_source.pop("occurrences", []))
    if not isinstance(raw_occurrences, list):
        raise ValueError("occurrences 必须是数组")
    if len(raw_occurrences) > PROGRAM_IMPORT_MAX_OCCURRENCES:
        raise ValueError(f"单次导入最多支持 {PROGRAM_IMPORT_MAX_OCCURRENCES} 期单集")

    program_source.pop("id", None)
    program_source["auto_generate"] = options["schedule_mode"] == "generated"
    program_source["parent_id"] = ""
    program_source["subprogram_name"] = "主节目"
    program_source["category"] = import_choice(program_source.get("category"), {
        "官方": "official", "官方节目": "official", "official": "official",
        "个人": "personal", "个人节目": "personal", "personal": "personal",
    })
    program_source["format"] = import_choice(program_source.get("format"), {
        "视频": "video", "有画面": "video", "video": "video",
        "广播": "radio", "无画面": "radio", "radio": "radio",
    })
    program_source["platform"] = import_choice(program_source.get("platform"), {
        "电视": "tv", "电视台": "tv", "tv": "tv",
        "网络": "network", "network": "network",
    })
    program_source["delivery"] = import_choice(program_source.get("delivery"), {
        "直播": "live", "live": "live", "录播": "recorded", "recorded": "recorded",
    })
    if "periods" in program_source:
        if not isinstance(program_source["periods"], list) or not all(isinstance(item, dict) for item in program_source["periods"]):
            raise ValueError("periods 必须是对象数组")
        program_source["periods"] = [normalize_import_periods(item) for item in program_source["periods"]]
    program_values = normalized_program(program_source)

    delivery_aliases = {"直播": "live", "live": "live", "录播": "recorded", "recorded": "recorded", "跟随默认": ""}
    status_aliases = {"正常": "scheduled", "正常播出": "scheduled", "scheduled": "scheduled", "取消": "cancelled", "因故取消": "cancelled", "cancelled": "cancelled", "改期": "rescheduled", "已改期": "rescheduled", "rescheduled": "rescheduled", "删除": "deleted", "已删除": "deleted", "deleted": "deleted"}
    special_aliases = {"普通": "", "普通单集": "", "": "", "ex": "EX", "EX": "EX", "EX单集": "EX", "EX 单集": "EX", "EX 特别节目": "EX", "特别节目": "EX"}
    weekday_periods = program_values["periods"]
    occurrences: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_dates: set[str] = set()
    skipped_generated = 0
    for index, raw_occurrence in enumerate(raw_occurrences, start=1):
        if not isinstance(raw_occurrence, dict):
            raise ValueError(f"第 {index} 期单集格式无效")
        item = dict(raw_occurrence)
        original_date = import_date(item.get("original_date") or item.get("date"), f"第 {index} 期原定日期")
        if not original_date:
            raise ValueError(f"第 {index} 期原定日期不能为空")
        if original_date in seen_dates:
            raise ValueError(f"第 {index} 期与其他单集重复使用原定日期：{original_date}")
        seen_dates.add(original_date)
        generated_date = import_date(item.get("generated_date"), f"第 {index} 期生成日期")
        matching_period = next(
            (
                period for period in weekday_periods
                if date.fromisoformat(period["start_date"]) <= date.fromisoformat(original_date)
                and (not period.get("end_date") or date.fromisoformat(original_date) <= date.fromisoformat(period["end_date"]))
            ),
            None,
        )
        if matching_period and matching_period["frequency"] == "individual" and not generated_date:
            original = date.fromisoformat(original_date)
            generated_date = date(original.year, original.month, 1).isoformat()
        status = import_choice(item.get("status") or "scheduled", status_aliases)
        special_value = item.get("special") or item.get("type")
        if item.get("is_ex") is True:
            special_value = "EX"
        generated_marker = boolean_value(item.get("generated"), False)
        has_override_content = any((
            item.get("title"),
            item.get("delivery") not in (None, "", "跟随默认"),
            item.get("source_url"),
            item.get("mirror_url"),
            item.get("subtitle_url"),
            item.get("status") not in (None, "", "scheduled", "正常", "正常播出"),
            item.get("special"),
            item.get("type"),
            item.get("is_ex") is True,
            item.get("adjusted_date"),
            item.get("adjusted_time"),
            item.get("shift_following_days") not in (None, "", 0),
            item.get("note"),
            item.get("guests"),
        ))
        if options["schedule_mode"] == "generated" and generated_marker and not has_override_content:
            skipped_generated += 1
            continue
        occurrence_values = {
            "original_date": original_date,
            "title": str(item.get("title") or "").strip(),
            "generated_date": generated_date,
            "original_time": str(item.get("original_time") or item.get("time") or "").strip(),
            "delivery": import_choice(item.get("delivery"), delivery_aliases),
            "shift_following_days": item.get("shift_following_days", 0),
            "source_url": item.get("source_url", ""),
            "mirror_url": item.get("mirror_url", ""),
            "subtitle_url": item.get("subtitle_url", ""),
            "status": status,
            "special": import_choice(special_value, special_aliases),
            "adjusted_date": import_date(item.get("adjusted_date"), f"第 {index} 期调整日期"),
            "adjusted_time": str(item.get("adjusted_time") or "").strip(),
            "note": item.get("note", ""),
            "guests": item.get("guests", []),
            "materialized": item.get("materialized", False),
        }
        occurrences.append(normalized_occurrence(occurrence_values))
    if skipped_generated:
        warnings.append(f"已跳过 {skipped_generated} 条未补充内容的自动生成单集，导入后仍由 periods 自动生成。")
    if options["schedule_mode"] == "individual":
        warnings.append("已按逐期准确模式导入：不会根据 periods 自动生成额外单集，也不会再次级联提前或顺延。")
    else:
        warnings.append("已按自动生成模式导入：periods 会生成排期，occurrences 中的记录作为覆盖或例外。")
    return program_values, occurrences, warnings


def import_preview_payload(
    program: dict[str, Any],
    occurrences: list[dict[str, Any]],
    warnings: list[str],
    options: dict[str, str],
    matches: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = program_json_metadata()
    payload["import_options"] = {
        "schedule_mode": options["schedule_mode"],
        "target_mode": "new",
        "target_program_id": "",
    }
    payload["source_program_id"] = options["source_program_id"]
    payload["matches"] = matches
    payload["program"] = {
        key: program[key]
        for key in ("title", "category", "format", "platform", "delivery", "auto_generate", "episode_start", "official_url", "description", "periods")
    }
    payload["program"]["people"] = program_people(program.get("people", []))
    payload["occurrences"] = []
    for occurrence in occurrences:
        item = {
            key: occurrence.get(key, "")
            for key in ("original_date", "title", "generated_date", "original_time", "delivery", "status", "special", "adjusted_date", "adjusted_time", "shift_following_days", "source_url", "mirror_url", "subtitle_url", "note")
        }
        item["guests"] = program_people(occurrence.get("guests", []))
        payload["occurrences"].append(item)
    payload["warnings"] = warnings
    payload["counts"] = {"periods": len(program["periods"]), "occurrences": len(occurrences)}
    return payload


def program_import_matches(source_program_id: str, title: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """SELECT id, title, subprogram_name, parent_id
               FROM programs
               WHERE (? != '' AND id = ?) OR title = ?
               ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, title COLLATE NOCASE, subprogram_name COLLATE NOCASE""",
            (source_program_id, source_program_id, title, source_program_id),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "subprogram_name": row["subprogram_name"] or "主节目",
            "display_name": f"{row['title']} · {row['subprogram_name']}" if row["parent_id"] else row["title"],
            "match": "id" if source_program_id and row["id"] == source_program_id else "title",
        }
        for row in rows
    ]


def validate_program_group(conn: sqlite3.Connection, values: dict[str, Any], current_id: str = "") -> dict[str, Any]:
    parent_id = values["parent_id"]
    if current_id:
        current = conn.execute("SELECT parent_id FROM programs WHERE id = ?", (current_id,)).fetchone()
        if current and (current["parent_id"] or "") != parent_id:
            raise ValueError("不能修改节目所属关系，请新建子节目")
    if not parent_id:
        values["subprogram_name"] = "主节目"
        return values
    if parent_id == current_id:
        raise ValueError("子节目不能挂在自己下面")
    parent = conn.execute("SELECT id, title, parent_id FROM programs WHERE id = ?", (parent_id,)).fetchone()
    if not parent:
        raise ValueError("所属主节目不存在")
    if parent["parent_id"]:
        raise ValueError("子节目只能挂在主节目下")
    values["title"] = parent["title"]
    sibling = conn.execute(
        "SELECT id FROM programs WHERE parent_id = ? AND subprogram_name = ? AND id != ?",
        (parent_id, values["subprogram_name"], current_id),
    ).fetchone()
    if sibling:
        raise ValueError("同一个主节目下不能重复使用子节目名称")
    return values


def seed_program_periods(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM programs").fetchall()
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        if not row["start_date"]:
            continue
        if conn.execute("SELECT 1 FROM program_periods WHERE program_id = ? LIMIT 1", (row["id"],)).fetchone():
            continue
        period = legacy_period(dict(row))
        if row["frequency"] == "monthly" and row["monthly_mode"] == "irregular":
            period["frequency"] = "single"
            period["end_date"] = period["start_date"]
            period["week_index"] = 0
        if period["frequency"] not in PROGRAM_FREQUENCIES:
            period["frequency"] = "weekly"
        conn.execute("""INSERT INTO program_periods (
            program_id, start_date, end_date, frequency, week_interval, week_index, weekday, schedule_time, timezone, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            row["id"], period["start_date"], period["end_date"], period["frequency"], period["week_interval"],
            period["week_index"], period["weekday"], period["schedule_time"], period["timezone"], row["created_at"] or now, row["updated_at"] or now,
        ))


def normalized_occurrence(values: dict[str, Any]) -> dict[str, Any]:
    original_date = str(values.get("original_date") or "").strip()
    if not original_date:
        raise ValueError("原定日期不能为空")
    generated_date = str(values.get("generated_date") or "").strip()
    adjusted_date = str(values.get("adjusted_date") or "").strip()
    for label, value in (("原定日期", original_date), ("生成日期", generated_date), ("调整日期", str(values.get("adjusted_date") or "").strip())):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{label}格式无效") from exc
    status = str(values.get("status") or "scheduled").strip()
    if status not in OCCURRENCE_STATUSES:
        raise ValueError("排期状态无效")
    special = str(values.get("special") or "").strip().upper()
    if special not in {"", "EX"}:
        raise ValueError("特殊节目类型无效")
    if status == "scheduled" and adjusted_date:
        status = "rescheduled"
    if status == "rescheduled" and not adjusted_date:
        raise ValueError("已改期单集需要填写调整日期")
    delivery = str(values.get("delivery") or "").strip()
    if delivery and delivery not in PROGRAM_DELIVERIES:
        raise ValueError("单集播出方式无效")
    shift_following_days = integer_value(values.get("shift_following_days"), "后续排期偏移天数", -7, 7)
    if shift_following_days not in {-7, 0, 7}:
        raise ValueError("后续排期偏移只能为 -7、0 或 7 天")
    if shift_following_days and status != "rescheduled":
        raise ValueError("只有已改期单集可以顺延后续排期")
    original_time = str(values.get("original_time") or "").strip()
    adjusted_time = str(values.get("adjusted_time") or "").strip()
    for label, value in (("原定时间", original_time), ("调整时间", adjusted_time)):
        if value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"{label}格式应为 HH:MM")
    return {
        "original_date": original_date,
        "title": str(values.get("title") or "").strip(),
        "generated_date": generated_date,
        "original_time": original_time,
        "delivery": delivery,
        "shift_following_days": shift_following_days,
        "source_url": normalized_program_link(values.get("source_url"), "源地址"),
        "mirror_url": normalized_program_link(values.get("mirror_url"), "搬运地址", True),
        "subtitle_url": normalized_program_link(values.get("subtitle_url"), "字幕地址", True),
        "status": status,
        "special": special,
        "adjusted_date": adjusted_date if status == "rescheduled" else "",
        "adjusted_time": adjusted_time if status == "rescheduled" else "",
        "note": str(values.get("note") or "").strip(),
        "guests": normalized_people(values.get("guests")),
        "materialized": boolean_value(values.get("materialized"), False),
    }


def insert_occurrence_row(conn: sqlite3.Connection, values: dict[str, Any]) -> sqlite3.Cursor:
    values = {"materialized": 0, **values}
    return conn.execute("""INSERT INTO program_occurrences (
        program_id, original_date, title, generated_date, original_time, delivery, shift_following_days, source_url, mirror_url, subtitle_url, status,
        adjusted_date, adjusted_time, note, guests, special, materialized, created_at, updated_at
    ) VALUES (
        :program_id, :original_date, :title, :generated_date, :original_time, :delivery, :shift_following_days, :source_url, :mirror_url, :subtitle_url, :status,
        :adjusted_date, :adjusted_time, :note, :guests, :special, :materialized, :created_at, :updated_at
    )""", values)


def calendar_date(value: str, fallback: date) -> date:
    try:
        return date.fromisoformat(value.split("T", 1)[0]) if value else fallback
    except ValueError as exc:
        raise HTTPException(400, "日历日期格式无效") from exc


def monthly_weekday(year: int, month: int, week_index: int, weekday: int) -> date | None:
    last_day = calendar.monthrange(year, month)[1]
    if week_index > 0:
        first = date(year, month, 1)
        day = 1 + (weekday - first.weekday()) % 7 + (week_index - 1) * 7
        return date(year, month, day) if day <= last_day else None
    last = date(year, month, last_day)
    day = last_day - (last.weekday() - weekday) % 7 + (week_index + 1) * 7
    return date(year, month, day) if day >= 1 else None


def period_recurring_dates(period: dict[str, Any], range_end: date) -> list[date]:
    if not period.get("start_date"):
        return []
    period_start = date.fromisoformat(period["start_date"])
    if period["frequency"] == "single":
        return [period_start] if period_start <= range_end else []
    if period["frequency"] == "weekly":
        current = period_start + timedelta(days=(period["weekday"] - period_start.weekday()) % 7)
        step = timedelta(days=7 * period["week_interval"])
        dates: list[date] = []
        while current <= range_end:
            dates.append(current)
            current += step
        return dates
    if period["frequency"] == "individual":
        dates = []
        current = period_start
        while current <= range_end:
            dates.append(current)
            current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
        return dates
    if period["frequency"] != "monthly" or not period.get("week_index"):
        return []
    dates = []
    current = date(period_start.year, period_start.month, 1)
    while current <= range_end:
        occurrence = monthly_weekday(current.year, current.month, period["week_index"], period["weekday"])
        if occurrence and period_start <= occurrence <= range_end:
            dates.append(occurrence)
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return dates


def occurrence_record(
    program: dict[str, Any],
    original: date,
    override: dict[str, Any] | None = None,
    schedule_time: str = "",
    timezone: str = "Asia/Tokyo",
    frequency: str = "",
    manual: bool = False,
    schedule_shift_days: int = 0,
) -> dict[str, Any]:
    has_override = bool(override)
    override = override or {}
    individual = frequency == "individual"
    generated_date = str(override.get("generated_date") or "").strip() if individual else ""
    base_original_date = original.isoformat()
    effective_original_date = str(override.get("original_date") or base_original_date).strip() if has_override else base_original_date
    default_time = schedule_time or program.get("schedule_time", "")
    effective_original_time = str(override.get("original_time") or default_time) if has_override else default_time
    delivery_override = str(override.get("delivery") or "").strip() if has_override else ""
    if delivery_override not in PROGRAM_DELIVERIES:
        delivery_override = ""
    delivery = delivery_override or str(program.get("delivery") or "recorded")
    shift_following_days = occurrence_shift_days(override.get("shift_following_days")) if has_override else 0
    explicit_adjusted_date = str(override.get("adjusted_date") or "").strip()
    event_date = explicit_adjusted_date or effective_original_date
    shifted_by_reschedule = False
    if not explicit_adjusted_date and schedule_shift_days:
        event_date = (date.fromisoformat(event_date) + timedelta(days=schedule_shift_days)).isoformat()
        shifted_by_reschedule = True
    event_time = override.get("adjusted_time") or effective_original_time
    status = override.get("status", "scheduled")
    if status == "scheduled" and override.get("adjusted_date"):
        status = "rescheduled"
    special = str(override.get("special") or "").strip().upper()
    record = {
        "id": override.get("id"),
        "generated": not has_override,
        "individual": individual,
        "original_date": effective_original_date,
        "title": str(override.get("title") or "").strip() if has_override else "",
        "generated_date": generated_date or (base_original_date if individual else ""),
        "original_time": effective_original_time,
        "delivery": delivery,
        "delivery_override": delivery_override,
        "shift_following_days": shift_following_days,
        "schedule_shift_days": schedule_shift_days,
        "shifted_by_reschedule": shifted_by_reschedule,
        "source_url": str(override.get("source_url") or "").strip(),
        "mirror_url": str(override.get("mirror_url") or "").strip(),
        "subtitle_url": str(override.get("subtitle_url") or "").strip(),
        "date": date.fromisoformat(event_date),
        "time": event_time,
        "timezone": timezone or program.get("timezone", "Asia/Tokyo"),
        "status": status,
        "special": special,
        "adjusted_date": override.get("adjusted_date", ""),
        "adjusted_time": override.get("adjusted_time", ""),
        "note": override.get("note", ""),
        "guests": program_people(override.get("guests", [])),
        "materialized": boolean_value(override.get("materialized"), False),
        "manual": manual,
    }
    record["aired"] = record["status"] not in {"cancelled", "deleted"} and occurrence_has_passed(record)
    return record


def program_occurrence_records(program: dict[str, Any], range_start: date, range_end: date) -> list[dict[str, Any]]:
    program_start = date.fromisoformat(program["start_date"]) if program.get("start_date") else range_start
    program_end = date.fromisoformat(program["end_date"]) if program.get("end_date") else None
    forecast_end = datetime.now(occurrence_timezone(program.get("timezone"))).date() + timedelta(days=PROGRAM_FORECAST_DAYS)
    generation_end = min(range_end, program_end) if program_end else min(range_end, forecast_end)
    override_rows = program.get("occurrences", [])
    overrides = {str(row.get("generated_date") or row["original_date"]): row for row in override_rows}
    periods = program.get("periods") or ([legacy_period(program)] if program.get("start_date") else [])
    records: list[dict[str, Any]] = []
    base_date_keys: set[str] = set()
    if boolean_value(program.get("auto_generate"), True):
        for period in periods:
            period_start = date.fromisoformat(period["start_date"])
            period_end = date.fromisoformat(period["end_date"]) if period.get("end_date") else program_end
            period_generation_end = min(generation_end, period_end) if period_end else generation_end
            if period_generation_end < period_start:
                continue
            base_dates = period_recurring_dates(period, period_generation_end)
            base_date_keys.update(item.isoformat() for item in base_dates)
            schedule_shift_days = 0
            can_shift_following = period.get("frequency") == "weekly" and int(period.get("week_interval") or 1) == 2
            for original in base_dates:
                record = occurrence_record(
                    program,
                    original,
                    overrides.get(original.isoformat()),
                    period.get("schedule_time", ""),
                    period.get("timezone", "Asia/Tokyo"),
                    period.get("frequency", "weekly"),
                    schedule_shift_days=schedule_shift_days,
                )
                records.append(record)
                if can_shift_following and record["status"] == "rescheduled":
                    schedule_shift_days += record.get("shift_following_days", 0)
    for row in override_rows:
        stored_generated_date = str(row.get("generated_date") or "").strip()
        generated_date = stored_generated_date or row["original_date"]
        if generated_date in base_date_keys:
            continue
        original = date.fromisoformat(row["original_date"])
        anchor = date.fromisoformat(generated_date)
        period = next(
            (
                item
                for item in periods
                if date.fromisoformat(item["start_date"]) <= anchor
                and (not item.get("end_date") or anchor <= date.fromisoformat(item["end_date"]))
            ),
            periods[0] if periods else {},
        )
        records.append(occurrence_record(
            program,
            original,
            row,
            period.get("schedule_time", ""),
            period.get("timezone", "Asia/Tokyo"),
            period.get("frequency", "weekly"),
            manual=not stored_generated_date and not boolean_value(row.get("materialized"), False),
        ))
    return records


def effective_program_occurrences(program: dict[str, Any], range_start: date, range_end: date) -> list[dict[str, Any]]:
    records = program_occurrence_records(program, range_start, range_end)
    return sorted(
        [record for record in records if record["status"] not in {"cancelled", "deleted"} and range_start <= record["date"] <= range_end],
        key=lambda record: (record["date"], record["time"], record["original_date"]),
    )


def occurrence_episode_numbers(records: list[dict[str, Any]], episode_start: int = 1) -> list[int]:
    episode = episode_start
    numbers = []
    for record in records:
        if record["status"] in {"cancelled", "deleted"}:
            numbers.append(episode)
            continue
        numbers.append(episode)
        if record.get("special") != "EX":
            episode += 1
    return numbers


def program_episode_start(program: dict[str, Any]) -> int:
    return 0 if str(program.get("episode_start", 1)).strip() == "0" else 1


def program_update_status(program: dict[str, Any]) -> str:
    if program.get("status") == "completed" or not program.get("start_date"):
        return "completed" if program.get("status") == "completed" else "not_updated"
    today = datetime.now(occurrence_timezone(program.get("timezone"))).date()
    start = date.fromisoformat(program["start_date"])
    if today < start:
        return "not_updated"
    occurrences = effective_program_occurrences(program, start, today)
    if not occurrences:
        return "not_updated"
    latest = max(occurrences, key=lambda item: item["date"])
    original_date = date.fromisoformat(latest.get("generated_date") or latest["original_date"])
    periods = program.get("periods") or []
    period = next(
        (
            item
            for item in periods
            if date.fromisoformat(item["start_date"]) <= original_date
            and (not item.get("end_date") or original_date <= date.fromisoformat(item["end_date"]))
        ),
        {},
    )
    if period.get("frequency") == "weekly":
        cadence_days = 7 * max(int(period.get("week_interval") or 1), 1)
    elif period.get("frequency") in {"monthly", "individual"}:
        cadence_days = 31
    else:
        cadence_days = 1
    return "not_updated" if today > latest["date"] + timedelta(days=cadence_days * 2) else "updated"


def occurrence_has_passed(occurrence: dict[str, Any]) -> bool:
    current = datetime.now(occurrence_timezone(occurrence["timezone"]))
    return occurrence["date"] < current.date() or (
        occurrence["date"] == current.date() and (not occurrence["time"] or occurrence["time"] <= current.strftime("%H:%M"))
    )


def program_episode_count(program: dict[str, Any]) -> int:
    if not program.get("start_date"):
        return 0
    program_start = date.fromisoformat(program["start_date"])
    today = datetime.now(occurrence_timezone(program.get("timezone"))).date()
    program_end = date.fromisoformat(program["end_date"]) if program.get("end_date") else None
    limit = program_end if program.get("status") == "completed" and program_end else today
    if program_end:
        limit = min(limit, program_end)
    if limit < program_start:
        return 0
    occurrences = effective_program_occurrences(program, program_start, limit)
    if program.get("status") == "completed":
        return sum(1 for item in occurrences if item.get("special") != "EX")
    return sum(
        1
        for item in occurrences
        if item.get("special") != "EX" and occurrence_has_passed(item)
    )


def materialize_generated_occurrences(conn: sqlite3.Connection, program: dict[str, Any]) -> int:
    if not program.get("start_date"):
        return 0
    program_start = date.fromisoformat(program["start_date"])
    program_end = date.fromisoformat(program["end_date"]) if program.get("end_date") else None
    range_end = program_end or datetime.now(occurrence_timezone(program.get("timezone"))).date() + timedelta(days=PROGRAM_FORECAST_DAYS)
    if range_end < program_start:
        return 0

    records = program_occurrence_records({**program, "auto_generate": True}, program_start, range_end)
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            program["id"],
            record["original_date"],
            record.get("title", ""),
            record.get("generated_date", ""),
            record.get("original_time", ""),
            record.get("delivery_override", ""),
            0,
            record.get("source_url", ""),
            record.get("mirror_url", ""),
            record.get("subtitle_url", ""),
            "rescheduled" if record["date"].isoformat() != record["original_date"] else record.get("status", "scheduled"),
            record["date"].isoformat() if record["date"].isoformat() != record["original_date"] else "",
            "",
            "",
            json.dumps([], ensure_ascii=False),
            record.get("special", ""),
            1,
            now,
            now,
        )
        for record in records
        if record["generated"] and record["aired"]
    ]
    if not rows:
        return 0

    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO program_occurrences (
            program_id, original_date, title, generated_date, original_time, delivery, shift_following_days, source_url, mirror_url, subtitle_url, status,
            adjusted_date, adjusted_time, note, guests, special, materialized, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    return conn.total_changes - before


def backfill_individual_occurrence_anchors(
    conn: sqlite3.Connection,
    program_id: str,
    old_periods: list[dict[str, Any]],
    new_periods: list[dict[str, Any]],
) -> None:
    converted_periods = {
        period["start_date"]: period
        for period in new_periods
        if period.get("frequency") == "individual"
    }
    monthly_periods = [
        period
        for period in old_periods
        if period.get("frequency") == "monthly" and period.get("start_date") in converted_periods
    ]
    if not monthly_periods:
        return

    rows = conn.execute(
        "SELECT id, original_date, generated_date FROM program_occurrences WHERE program_id = ? ORDER BY original_date, id",
        (program_id,),
    ).fetchall()
    used_anchors = {
        str(row["generated_date"]).strip()
        for row in rows
        if str(row["generated_date"] or "").strip()
    }
    for row in rows:
        if str(row["generated_date"] or "").strip():
            continue
        original = date.fromisoformat(row["original_date"])
        old_period = next(
            (
                period
                for period in monthly_periods
                if date.fromisoformat(period["start_date"]) <= original
                and (not period.get("end_date") or original <= date.fromisoformat(period["end_date"]))
            ),
            None,
        )
        if not old_period:
            continue
        anchor = date(original.year, original.month, 1)
        period_start = date.fromisoformat(old_period["start_date"])
        period_end = date.fromisoformat(old_period["end_date"]) if old_period.get("end_date") else None
        anchor_value = anchor.isoformat()
        if anchor < period_start or (period_end and anchor > period_end) or anchor_value in used_anchors:
            anchor_value = original.isoformat()
        if anchor_value in used_anchors:
            continue
        conn.execute(
            "UPDATE program_occurrences SET generated_date = ? WHERE id = ? AND program_id = ?",
            (anchor_value, row["id"], program_id),
        )
        used_anchors.add(anchor_value)


def replace_program_periods(conn: sqlite3.Connection, program_id: str, periods: list[dict[str, Any]], timestamp: str) -> None:
    conn.execute("DELETE FROM program_periods WHERE program_id = ?", (program_id,))
    conn.executemany("""INSERT INTO program_periods (
        program_id, start_date, end_date, frequency, week_interval, week_index, weekday, schedule_time, timezone, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", [
        (
            program_id,
            period["start_date"],
            period["end_date"],
            period["frequency"],
            period["week_interval"],
            period["week_index"],
            period["weekday"],
            period["schedule_time"],
            period["timezone"],
            timestamp,
            timestamp,
        )
        for period in periods
    ])


def insert_program_row(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    conn.execute("""INSERT INTO programs (
        id, title, status, category, format, platform, delivery, auto_generate, people, official_url, description,
        frequency, week_interval, monthly_mode, week_index, weekday, schedule_time,
        start_date, end_date, parent_id, subprogram_name, episode_start, created_at, updated_at
    ) VALUES (
        :id, :title, :status, :category, :format, :platform, :delivery, :auto_generate, :people, :official_url, :description,
        :frequency, :week_interval, :monthly_mode, :week_index, :weekday, :schedule_time,
        :start_date, :end_date, :parent_id, :subprogram_name, :episode_start, :created_at, :updated_at
    )""", values)
    replace_program_periods(conn, values["id"], values["periods"], values["updated_at"])


def replace_imported_program_row(
    conn: sqlite3.Connection,
    values: dict[str, Any],
    target_id: str,
    created_at: str,
    updated_at: str,
) -> None:
    values = {**values, "id": target_id, "created_at": created_at, "updated_at": updated_at}
    conn.execute("""UPDATE programs SET
        title=:title, status=:status, category=:category, format=:format, platform=:platform, delivery=:delivery, auto_generate=:auto_generate,
        people=:people, official_url=:official_url, description=:description,
        frequency=:frequency, week_interval=:week_interval, monthly_mode=:monthly_mode,
        week_index=:week_index, weekday=:weekday, schedule_time=:schedule_time,
        start_date=:start_date, end_date=:end_date, parent_id=:parent_id, subprogram_name=:subprogram_name, episode_start=:episode_start, updated_at=:updated_at
        WHERE id=:id""", values)
    if not values["parent_id"]:
        conn.execute(
            "UPDATE programs SET title = ?, updated_at = ? WHERE parent_id = ?",
            (values["title"], updated_at, target_id),
        )
    conn.execute("DELETE FROM program_occurrences WHERE program_id = ?", (target_id,))
    replace_program_periods(conn, target_id, values["periods"], updated_at)


def program_rows() -> list[dict[str, Any]]:
    with db() as conn:
        program_rows = conn.execute("SELECT * FROM programs ORDER BY category, title COLLATE NOCASE, CASE WHEN parent_id = '' THEN 0 ELSE 1 END, subprogram_name COLLATE NOCASE").fetchall()
        period_rows = conn.execute("SELECT * FROM program_periods ORDER BY start_date, id").fetchall()
        occurrence_rows = conn.execute("SELECT * FROM program_occurrences ORDER BY original_date, id").fetchall()
    periods_grouped: dict[str, list[dict[str, Any]]] = {}
    for row in period_rows:
        periods_grouped.setdefault(row["program_id"], []).append(period_payload(row))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in occurrence_rows:
        grouped.setdefault(row["program_id"], []).append(occurrence_payload(row))
    return [program_payload(row, grouped.get(row["id"], []), periods_grouped.get(row["id"])) for row in program_rows]


def program_json_occurrence_item(occurrence: dict[str, Any], freeze_effective_date: bool) -> dict[str, Any]:
    original_date = occurrence.get("original_date", "")
    effective_date = occurrence.get("date")
    effective_date = effective_date.isoformat() if isinstance(effective_date, date) else str(effective_date or original_date)
    status = occurrence.get("status", "scheduled")
    adjusted_date = occurrence.get("adjusted_date", "")
    adjusted_time = occurrence.get("adjusted_time", "")
    if freeze_effective_date and status not in {"cancelled", "deleted"} and effective_date != original_date:
        status = "rescheduled"
        adjusted_date = effective_date
        adjusted_time = occurrence.get("time", "") if occurrence.get("time", "") != occurrence.get("original_time", "") else ""
    item = {
        "original_date": original_date,
        "title": str(occurrence.get("title") or "").strip(),
        "original_time": occurrence.get("original_time", ""),
        "delivery": occurrence.get("delivery_override", occurrence.get("delivery", "")),
        "status": status,
        "special": occurrence.get("special", ""),
        "generated": boolean_value(occurrence.get("generated"), False),
        "materialized": boolean_value(occurrence.get("materialized"), False),
        "source_url": occurrence.get("source_url", ""),
        "mirror_url": occurrence.get("mirror_url", ""),
        "subtitle_url": occurrence.get("subtitle_url", ""),
        "note": occurrence.get("note", ""),
        "guests": occurrence.get("guests", []),
    }
    if occurrence.get("generated_date"):
        item["generated_date"] = occurrence["generated_date"]
    if status == "rescheduled":
        item["adjusted_date"] = adjusted_date
        item["adjusted_time"] = adjusted_time
        item["shift_following_days"] = 0 if freeze_effective_date else occurrence.get("shift_following_days", 0)
    elif not freeze_effective_date and occurrence.get("shift_following_days"):
        item["shift_following_days"] = occurrence["shift_following_days"]
    return item


def program_json_export(program: dict[str, Any], mode: str = "individual") -> dict[str, Any]:
    if mode not in {"individual", "generated"}:
        raise ValueError("导出模式必须是 individual 或 generated")
    payload = program_json_metadata()
    payload["_exported_at"] = datetime.now(timezone.utc).isoformat()
    payload["import_options"] = {
        "schedule_mode": mode,
        "target_mode": "new",
        "target_program_id": "",
    }
    payload["program"] = {
        key: program.get(key)
        for key in ("title", "category", "format", "platform", "delivery", "auto_generate", "episode_start", "people", "official_url", "description", "periods")
    }
    payload["program"]["id"] = program.get("id", "")
    if mode == "individual":
        payload["program"]["auto_generate"] = False
        payload["_export_notes"] = [
            "这是完整逐期快照；自动生成的单集也会展开写入 occurrences。",
            "快照按当前实际播出日期冻结；由前期改期级联产生的日期会转换为本期独立的 rescheduled 记录。",
            "导入此文件后默认关闭自动生成，不会因 periods 重新生成或再次级联提前/顺延。",
        ]
        today = datetime.now(JAPAN_TZ).date()
        start = calendar_date(program.get("start_date", ""), today)
        end = calendar_date(program.get("end_date", ""), today + timedelta(days=PROGRAM_FORECAST_DAYS))
        records = program_occurrence_list(program, start.isoformat(), end.isoformat())["occurrences"]
        payload["occurrences"] = [program_json_occurrence_item(record, True) for record in records]
    else:
        payload["program"]["auto_generate"] = True
        payload["_export_notes"] = [
            "这是排期规则加当前自动生成结果和已保存例外；自动生成单集会写入 occurrences，并标记 generated=true。",
            "导入此文件后会按 periods 自动生成；没有补充内容的 generated=true 记录不会重复保存，有内容的记录会作为覆盖保留。",
            "当前自动生成结果按系统现有约半年的生成窗口导出。",
        ]
        today = datetime.now(JAPAN_TZ).date()
        start = calendar_date(program.get("start_date", ""), today)
        end = calendar_date(program.get("end_date", ""), today + timedelta(days=PROGRAM_FORECAST_DAYS))
        records = program_occurrence_list({**program, "auto_generate": True}, start.isoformat(), end.isoformat())["occurrences"]
        payload["occurrences"] = [
            program_json_occurrence_item(occurrence, False)
            for occurrence in records
        ]
    return payload


def occurrence_start_value(value_date: date, value_time: str, timezone_value: str) -> str:
    if not value_time:
        return value_date.isoformat()
    start = datetime.combine(value_date, time.fromisoformat(value_time), tzinfo=occurrence_timezone(timezone_value))
    return start.isoformat(timespec="minutes")


def program_calendar_events(program: dict[str, Any], range_start: date, range_end: date) -> list[dict[str, Any]]:
    program_start = date.fromisoformat(program["start_date"]) if program.get("start_date") else range_start
    records = sorted(program_occurrence_records(program, program_start, range_end), key=lambda record: record["original_date"])
    episode_numbers = occurrence_episode_numbers(records, program_episode_start(program))
    events = []
    for index, record in enumerate(records):
        if record["status"] == "deleted":
            continue
        if not range_start <= record["date"] <= range_end:
            continue
        episode_number = episode_numbers[index]
        update_suffix = " · 未更新" if program.get("update_status") == "not_updated" else ""
        if record.get("special") == "EX":
            episode_label = "EX 特别节目"
        elif record["status"] == "cancelled":
            episode_label = f"原定第{episode_number}期"
        else:
            episode_label = f"第{episode_number}期"
        occurrence_title = str(record.get("title") or "").strip()
        event_title = f"{program_display_name(program)} · {episode_label}"
        if occurrence_title:
            event_title += f" · {occurrence_title}"
        event_title += " · 已取消" if record["status"] == "cancelled" else update_suffix
        event: dict[str, Any] = {
            "id": f"{program['id']}-{record['original_date']}",
            "title": event_title,
            "start": occurrence_start_value(record["date"], record["time"], record["timezone"]),
            "allDay": not bool(record["time"]),
            "extendedProps": {
                "programId": program["id"],
                "programTitle": program["title"],
                "subprogramName": program.get("subprogram_name") or "主节目",
                "episode": episode_number,
                "occurrenceTitle": occurrence_title,
                "special": record.get("special", ""),
                "source_url": record.get("source_url", ""),
                "mirror_url": record.get("mirror_url", ""),
                "subtitle_url": record.get("subtitle_url", ""),
                "category": program["category"],
                "status": program["status"],
                "format": program["format"],
                "delivery": record.get("delivery", program.get("delivery", "recorded")),
                "occurrenceId": record["id"],
                "originalDate": record["original_date"],
                "originalTime": record["original_time"],
                "originalStart": occurrence_start_value(
                    date.fromisoformat(record["original_date"]),
                    record["original_time"],
                    record["timezone"],
                ),
                "adjustedDate": record["adjusted_date"],
                "adjustedTime": record["adjusted_time"],
                "adjustedStart": occurrence_start_value(
                    date.fromisoformat(record["adjusted_date"]),
                    record["adjusted_time"] or record["original_time"],
                    record["timezone"],
                ) if record["adjusted_date"] else "",
                "timezone": record["timezone"],
                "occurrenceStatus": record["status"],
                "aired": record["aired"],
                "updateStatus": program.get("update_status", "updated"),
                "note": record["note"],
                "guests": record["guests"],
                "people": program_people(program.get("people", [])),
            },
        }
        events.append(event)
    return events


def admin_cookie(request: Request) -> bool:
    raw = request.cookies.get("nijidb_admin", "")
    expected = admin_cookie_value(settings().get("admin_password_hash", ""))
    return bool(raw and hmac.compare_digest(raw, expected))


def require_api_admin(request: Request) -> None:
    if not admin_cookie(request):
        raise HTTPException(401, "需要管理员登录")


def set_admin_cookie(response: JSONResponse) -> None:
    response.set_cookie("nijidb_admin", admin_cookie_value(settings().get("admin_password_hash", "")), httponly=True, samesite="strict")


def normalized_settings(values: dict[str, Any]) -> dict[str, str]:
    try:
        interval = max(5, min(60, int(str(values.get("interval_minutes", "10")))))
    except ValueError:
        interval = 10
    try:
        detail_interval = max(1, min(30, int(str(values.get("detail_interval_minutes", "5")))))
    except ValueError:
        detail_interval = 5
    return {
        "interval_minutes": str(interval),
        "detail_interval_minutes": str(detail_interval),
        "onebot_url": str(values.get("onebot_url", "") or "").strip(),
        "onebot_token": str(values.get("onebot_token", "") or "").strip(),
        "onebot_target": str(values.get("onebot_target", "") or "").strip(),
        "onebot_profile": str(values.get("onebot_profile", "bot") or "").strip() or "bot",
    }


def save_settings(values: dict[str, str]) -> None:
    with db() as conn:
        conn.executemany("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", values.items())


def clean_text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def parse_plain_tracks(spec) -> list[dict[str, object]]:
    tracks: list[dict[str, object]] = []
    blocks = spec.select("p.list")
    if not blocks and getattr(spec, "name", "") == "dd":
        blocks = [spec]
    for block in blocks:
        current: dict[str, object] | None = None
        for raw_line in block.get_text("\n").splitlines():
            line = re.sub(r"[ \t]+", " ", raw_line).strip(" \u3000")
            if not line:
                continue
            track_match = re.match(r"^(\d{1,3})(?:[.．、]\s*|\s+)(.+)$", line)
            if track_match:
                if current:
                    tracks.append(current)
                current = {"disc": "", "number": int(track_match.group(1)), "title": track_match.group(2).strip(), "credits": {}}
                continue
            if current and (":" in line or "：" in line):
                key, value = re.split(r"[：:]", line, maxsplit=1)
                key = re.sub(r"\s*・\s*", "・", key).replace(" ", "").strip()
                add_credit(current["credits"], key, value.strip())
        if current:
            tracks.append(current)
    return tracks


def add_credit(credits: dict[str, str], key: str, value: str) -> None:
    normalized = re.sub(r"\s+", "", key).strip("：:")
    roles = [part for part in re.split(r"[・/／]", normalized) if part]
    if len(roles) > 1 and all(role in {"作詞", "作曲", "編曲"} for role in roles):
        for role in roles:
            credits[role] = value
    else:
        credits[normalized] = value


def parse_extras(content) -> list[dict[str, object]]:
    extras: list[dict[str, object]] = []
    for panel in content.select(".tokutenbox, .kokuchibox"):
        heading = panel.select_one(".tokutentitle")
        items = [clean_text(item) for item in panel.select("li") if clean_text(item)]
        body = clean_text(panel)
        if not heading and not items and panel.select_one("a[href]"):
            continue
        if heading:
            body = body.removeprefix(clean_text(heading)).strip()
        extras.append({"type": "bonus" if "tokutenbox" in (panel.get("class") or []) else "notice", "title": clean_text(heading) or "相关说明", "entries": items or ([body] if body else [])})
    for link in content.select("a[href]"):
        url = urljoin(SOURCE_URL, link["href"])
        label = clean_text(link)
        if not label or url.startswith("#") or any(extra.get("url") == url for extra in extras):
            continue
        extras.append({"type": "link", "title": label, "url": url})
    return extras


def parse_release(release_id: str, content, entry_image=None) -> dict[str, str]:
    title = content.select_one(".title") if content else None
    subtitle = title.find("span") if title else None
    entry_title = clean_text(title) or (entry_image.get("alt", "").strip() if entry_image else "") or release_id
    specs = content.select(".spec") if content else []
    fields: dict[str, str] = {}
    tracks: list[dict[str, object]] = []
    plain_track_sources = []
    for spec in specs:
        for dt in spec.find_all("dt", recursive=False):
            dd = dt.find_next_sibling("dd")
            if dd:
                field_name = clean_text(dt).strip("【】")
                fields[field_name] = clean_text(dd)
                if field_name in {"収録内容", "仕様", "収録曲"}:
                    plain_track_sources.append(dd)
        for track_list in spec.select("ul.track"):
            for number, track in enumerate(track_list.find_all("li", recursive=False), 1):
                track_copy = BeautifulSoup(str(track), "html.parser").find("li")
                credits: dict[str, str] = {}
                credit_list = track_copy.find("dl", recursive=False) if track_copy else None
                if credit_list:
                    for credit_name in credit_list.find_all("dt", recursive=False):
                        credit_value = credit_name.find_next_sibling("dd")
                        if credit_value:
                            add_credit(credits, clean_text(credit_name), clean_text(credit_value))
                    credit_list.decompose()
                media_label = track_list.find_previous("p", class_="media")
                track_title = re.sub(r"^\d{1,3}(?:[.．、]|\s)\s*", "", clean_text(track_copy))
                tracks.append({"disc": media_label.get_text(strip=True) if media_label else "", "number": number, "title": track_title, "credits": credits})
        if not tracks:
            for track_source in plain_track_sources:
                tracks.extend(parse_plain_tracks(track_source))
    image = (content.select_one(".cover img") if content else None) or entry_image
    detail_copy = BeautifulSoup(str(content or ""), "html.parser") if content else BeautifulSoup("", "html.parser")
    for detail_image in detail_copy.select("img[src]"):
        detail_image["src"] = urljoin(SOURCE_URL, detail_image["src"])
        detail_image["loading"] = "lazy"
    detail_html = str(detail_copy) if content else ""
    extras = parse_extras(content) if content else []
    record = {
        "id": release_id,
        "title": entry_title,
        "subtitle": clean_text(subtitle),
        "artist": fields.get("アーティスト", ""),
        "release_date": fields.get("発売日") or fields.get("一般発売日") or fields.get("劇場先行発売日", ""),
        "price": fields.get("価格", ""),
        "cover_url": urljoin(SOURCE_URL, image.get("src", "")) if image else "",
        "detail_html": detail_html,
        "source_url": f"{SOURCE_URL}#{release_id}",
        "tracks_json": json.dumps(tracks, ensure_ascii=False),
        "spec_json": json.dumps(fields, ensure_ascii=False),
        "extras_json": json.dumps(extras, ensure_ascii=False),
    }
    record["fingerprint"] = hashlib.sha256((record["title"] + record["detail_html"] + record["cover_url"] + record["tracks_json"] + record["extras_json"]).encode()).hexdigest()
    return record


async def scrape() -> list[dict[str, str]]:
    print(f"[sync] fetching {SOURCE_URL}", flush=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://www.lovelive-anime.jp/nijigasaki/",
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        response = await client.get(SOURCE_URL)
        response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    result = []
    boxes = {box.get("id"): box for box in soup.select(".box[id]")}
    source_list = soup.select_one("ul.list")
    if not source_list:
        return result
    missing_detail_ids = []
    for link in source_list.select("a[href^='#']"):
        release_id = link["href"][1:]
        box = boxes.get(release_id)
        if not box or not box.select_one(".title"):
            missing_detail_ids.append(release_id)
    dynamic_details: dict[str, BeautifulSoup] = {}
    if missing_detail_ids:
        detail_headers = {**headers, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=detail_headers) as detail_client:
            for release_id in missing_detail_ids:
                try:
                    detail_response = await detail_client.post(
                        urljoin(SOURCE_URL, "cd_detail.php"),
                        json=release_id.removeprefix("cd"),
                    )
                    detail_response.raise_for_status()
                    dynamic_details[release_id] = BeautifulSoup(detail_response.json(), "html.parser")
                except Exception as exc:
                    print(f"[sync] detail unavailable {release_id}: {exc}", flush=True)
        print(f"[sync] loaded {len(dynamic_details)}/{len(missing_detail_ids)} deferred details", flush=True)
    for entry in source_list.find_all("li", recursive=False):
        link = entry.find("a", href=True)
        if not link or not link["href"].startswith("#"):
            continue
        release_id = link["href"][1:]
        box = boxes.get(release_id)
        content = dynamic_details.get(release_id) or box
        entry_image = entry.find("img", src=True)
        result.append(parse_release(release_id, content, entry_image))
    print(f"[sync] parsed {len(result)} releases", flush=True)
    return result


async def send_onebot(message: str, config: dict[str, str]) -> None:
    if not config.get("onebot_url") or not config.get("onebot_target"):
        raise ValueError("OneBot 地址或接收目标未设置")
    target = config["onebot_target"]
    if target.startswith("private:"):
        path, key = "/send_private_msg", "user_id"
        target = target.removeprefix("private:")
    elif target.startswith("group:"):
        path, key = "/send_group_msg", "group_id"
        target = target.removeprefix("group:")
    else:
        path, key = "/send_group_msg", "group_id"
    headers = {"Content-Type": "application/json"}
    if config.get("onebot_token"):
        headers["Authorization"] = f"Bearer {config['onebot_token']}"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(config["onebot_url"].rstrip("/") + path, json={key: int(target), "message": message}, headers=headers)
        response.raise_for_status()


def release_change_details(item: dict[str, Any]) -> list[str]:
    previous = item.get("_previous")
    if previous is None:
        details = ["新增 CD/资料"]
    else:
        details = []

    if item.get("_cover_changed"):
        details.append("封面已更新" if previous is not None else "封面已添加")

    basic_labels = {
        "title": "标题",
        "subtitle": "副标题",
        "artist": "艺人",
        "release_date": "发售日",
        "price": "价格",
    }
    if previous is None:
        changed_basic = [key for key in basic_labels if item.get(key, "")]
    else:
        changed_basic = [key for key in basic_labels if previous.get(key, "") != item.get(key, "")]
    if changed_basic:
        basic_values = [f"{basic_labels[key]} {str(item.get(key, '')).strip()[:80]}" for key in changed_basic if item.get(key, "")]
        summary = f"基本信息已更新：{'、'.join(basic_labels[key] for key in changed_basic)}"
        if basic_values:
            summary += f"（{'；'.join(basic_values)}）"
        details.append(summary)

    old_tracks = decode_json(previous.get("tracks_json"), []) if previous else []
    new_tracks = decode_json(item.get("tracks_json"), [])
    if not isinstance(old_tracks, list):
        old_tracks = []
    if not isinstance(new_tracks, list):
        new_tracks = []
    if (previous is None and new_tracks) or (previous is not None and old_tracks != new_tracks):
        titles = [str(track.get("title", "")).strip() for track in new_tracks if isinstance(track, dict) and track.get("title")]
        track_summary = f"曲目（{len(new_tracks)} 首）"
        if titles:
            track_summary += f"：{' / '.join(titles[:6])}"
            if len(titles) > 6:
                track_summary += f" 等 {len(titles)} 首"
        details.append(track_summary)

    old_specs = decode_json(previous.get("spec_json"), {}) if previous else {}
    new_specs = decode_json(item.get("spec_json"), {})
    if not isinstance(old_specs, dict):
        old_specs = {}
    if not isinstance(new_specs, dict):
        new_specs = {}
    changed_specs = [key for key in new_specs if old_specs.get(key) != new_specs.get(key)]
    if previous is None or changed_specs:
        spec_labels = {
            "仕様": "收录/规格",
            "収録内容": "收录内容",
            "収録曲": "收录曲目",
        }
        detail_keys = [spec_labels.get(key, key) for key in changed_specs if key not in {"アーティスト", "発売日", "一般発売日", "劇場先行発売日", "価格"}]
        if previous is None:
            detail_keys = [spec_labels.get(key, key) for key in new_specs if key not in {"アーティスト", "発売日", "一般発売日", "劇場先行発売日", "価格"}]
        if detail_keys:
            details.append(f"资料字段已更新：{'、'.join(detail_keys[:6])}")

    old_extras = decode_json(previous.get("extras_json"), []) if previous else []
    new_extras = decode_json(item.get("extras_json"), [])
    if not isinstance(old_extras, list):
        old_extras = []
    if not isinstance(new_extras, list):
        new_extras = []
    if (previous is None and new_extras) or (previous is not None and old_extras != new_extras):
        details.append(f"特典/相关链接已更新（{len(new_extras)} 项）")

    if previous is not None and previous.get("detail_html", "") != item.get("detail_html", "") and len(details) == 0:
        details.append("详情页面内容已更新")
    return details[:6]


async def notify(changed: list[dict[str, Any]], config: dict[str, str], category: str) -> None:
    if not changed or not config.get("onebot_url") or not config.get("onebot_target"):
        return
    lines = [f"[{category}]", f"虹咲音乐资料有 {len(changed)} 项更新："]
    for item in changed[:10]:
        lines.append(f"• {item['title']}")
        lines.extend(f"  - {detail}" for detail in release_change_details(item))
    if len(changed) > 10:
        lines.append(f"以及其他 {len(changed) - 10} 项")
    await send_onebot("\n".join(lines), config)


def cover_cache_is_current() -> bool:
    try:
        return COVER_CACHE_VERSION_PATH.read_text(encoding="utf-8").strip() == COVER_CACHE_VERSION
    except OSError:
        return False


def mark_cover_cache_current() -> None:
    temporary = COVER_CACHE_VERSION_PATH.with_name(f".{COVER_CACHE_VERSION_PATH.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(f"{COVER_CACHE_VERSION}\n", encoding="utf-8")
        temporary.replace(COVER_CACHE_VERSION_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def cover_refresh_ids(records: list[dict[str, str]]) -> set[str]:
    if not records:
        return set()
    with db() as conn:
        existing = {row["id"]: row["fingerprint"] for row in conn.execute("SELECT id, fingerprint FROM releases")}
    return {item["id"] for item in records if existing.get(item["id"]) != item["fingerprint"]}


def r2_upload_is_configured() -> bool:
    return bool(R2_ENDPOINT and R2_BUCKET and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY)


def r2_is_configured() -> bool:
    return bool(r2_upload_is_configured() and R2_PUBLIC_BASE_URL)


def r2_object_key(filename: str) -> str:
    return f"{R2_IMAGE_PREFIX}/{filename}" if R2_IMAGE_PREFIX else filename


def public_image_url(filename: str) -> str:
    if not r2_is_configured():
        return f"/media/{filename}"
    return f"{R2_PUBLIC_BASE_URL}/{quote(r2_object_key(filename), safe='/')}"


def upload_cover_to_r2(path: Path) -> None:
    global _r2_client
    if _r2_client is None:
        _r2_client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name=os.getenv("R2_REGION", "auto"),
        )
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    _r2_client.upload_file(
        str(path),
        R2_BUCKET,
        r2_object_key(path.name),
        ExtraArgs={"ContentType": content_type, "CacheControl": "no-cache"},
    )


async def cache_cover(item: dict[str, str], client: httpx.AsyncClient, force_refresh: bool = False) -> bool:
    if not item["cover_url"]:
        return False
    extension = Path(urlparse(item["cover_url"]).path).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        extension = ".jpg"
    target = MEDIA_DIR / f"{item['id']}{extension}"
    cover_changed = not target.exists()
    if force_refresh or not target.exists():
        response = await client.get(item["cover_url"])
        response.raise_for_status()
        cover_changed = not target.exists() or target.read_bytes() != response.content
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
        try:
            temporary.write_bytes(response.content)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        if r2_upload_is_configured():
            await asyncio.to_thread(upload_cover_to_r2, target)
    source_url = item["cover_url"]
    item["cover_url"] = public_image_url(target.name)
    item["detail_html"] = item["detail_html"].replace(source_url, item["cover_url"])
    return cover_changed


async def store_records(
    records: list[dict[str, str]],
    assign_positions: bool = False,
    refreshed_cover_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    changed: list[dict[str, Any]] = []
    refreshed_cover_ids = refreshed_cover_ids or set()
    with db() as conn:
        existing_count = conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
        for position, item in enumerate(records):
            if assign_positions:
                item["position"] = position
            old = conn.execute("SELECT * FROM releases WHERE id = ?", (item["id"],)).fetchone()
            if old and old["fingerprint"] != item["fingerprint"]:
                changed.append({**item, "_previous": dict(old), "_cover_changed": item["id"] in refreshed_cover_ids})
            elif not old and existing_count > 0:
                changed.append({**item, "_previous": None, "_cover_changed": item["id"] in refreshed_cover_ids})
            conn.execute("""INSERT INTO releases (id,title,subtitle,artist,release_date,price,cover_url,detail_html,source_url,fingerprint,updated_at,position,tracks_json,spec_json,extras_json)
                VALUES (:id,:title,:subtitle,:artist,:release_date,:price,:cover_url,:detail_html,:source_url,:fingerprint,:updated_at,:position,:tracks_json,:spec_json,:extras_json)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, subtitle=excluded.subtitle, artist=excluded.artist, release_date=excluded.release_date, price=excluded.price, cover_url=excluded.cover_url, detail_html=excluded.detail_html, source_url=excluded.source_url, fingerprint=excluded.fingerprint, updated_at=excluded.updated_at, position=excluded.position, tracks_json=excluded.tracks_json, spec_json=excluded.spec_json, extras_json=excluded.extras_json""", {**item, "updated_at": datetime.now(timezone.utc).isoformat()})
        conn.execute("INSERT INTO sync_log(checked_at, changed_count) VALUES (?, ?)", (datetime.now(timezone.utc).isoformat(), len(changed)))
    return changed


async def sync_once() -> tuple[int, str | None]:
    print("[sync] started", flush=True)
    async with sync_lock:
        try:
            records = await scrape()
            refresh_ids = cover_refresh_ids(records)
            refresh_all = not cover_cache_is_current()
            image_headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
                "Referer": SOURCE_URL,
            }
            image_errors = 0
            refreshed_cover_ids: set[str] = set()
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=image_headers) as image_client:
                for item in records:
                    try:
                        if await cache_cover(item, image_client, force_refresh=refresh_all or item["id"] in refresh_ids):
                            refreshed_cover_ids.add(item["id"])
                    except Exception as exc:
                        image_errors += 1
                        print(f"[sync] cover failed {item['id']}: {exc}", flush=True)
            changed = await store_records(records, assign_positions=True, refreshed_cover_ids=refreshed_cover_ids)
            if records and not image_errors:
                mark_cover_cache_current()
            config = settings()
            await notify([item for item in changed if "_" not in item["id"]], config, "目录更新")
            await notify([item for item in changed if "_" in item["id"]], config, "异步详情更新")
            print(f"[sync] completed: {len(changed)} changed, {len(records) - image_errors} covers cached", flush=True)
            return len(changed), None
        except Exception as exc:
            print(f"[sync] failed: {exc}", flush=True)
            with db() as conn:
                conn.execute("INSERT INTO sync_log(checked_at, changed_count, error) VALUES (?, 0, ?)", (datetime.now(timezone.utc).isoformat(), str(exc)))
            return 0, str(exc)


async def refresh_deferred_once() -> tuple[int, str | None]:
    print("[detail-sync] started", flush=True)
    async with sync_lock:
        try:
            with db() as conn:
                rows = conn.execute("SELECT id, position FROM releases WHERE id LIKE '%\\_%' ESCAPE '\\' ORDER BY position").fetchall()
            if not rows:
                print("[detail-sync] no deferred releases", flush=True)
                return 0, None
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
                "Referer": SOURCE_URL,
                "Content-Type": "application/json",
            }
            records = []
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
                for row in rows:
                    response = await client.post(urljoin(SOURCE_URL, "cd_detail.php"), json=row["id"].removeprefix("cd"))
                    response.raise_for_status()
                    item = parse_release(row["id"], BeautifulSoup(response.json(), "html.parser"))
                    item["position"] = row["position"]
                    records.append(item)
            image_errors = 0
            refreshed_cover_ids: set[str] = set()
            refresh_ids = cover_refresh_ids(records)
            refresh_all = not cover_cache_is_current()
            image_headers = {"User-Agent": headers["User-Agent"], "Referer": SOURCE_URL}
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=image_headers) as image_client:
                for item in records:
                    try:
                        if await cache_cover(item, image_client, force_refresh=refresh_all or item["id"] in refresh_ids):
                            refreshed_cover_ids.add(item["id"])
                    except Exception as exc:
                        image_errors += 1
                        print(f"[detail-sync] cover failed {item['id']}: {exc}", flush=True)
            changed = await store_records(records, refreshed_cover_ids=refreshed_cover_ids)
            await notify(changed, settings(), "异步详情更新")
            print(f"[detail-sync] completed: {len(changed)} changed, {len(records) - image_errors} checked", flush=True)
            return len(changed), None
        except Exception as exc:
            print(f"[detail-sync] failed: {exc}", flush=True)
            return 0, str(exc)


async def wait_or_stop(seconds: int) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def source_scheduler() -> None:
    while not stop_event.is_set():
        await sync_once()
        config = settings()
        try:
            seconds = max(300, min(3600, int(float(config.get("interval_minutes", "10")) * 60)))
        except ValueError:
            seconds = 600
        await wait_or_stop(seconds)


async def detail_scheduler() -> None:
    await wait_or_stop(30)
    while not stop_event.is_set():
        await refresh_deferred_once()
        config = settings()
        try:
            seconds = max(60, min(1800, int(float(config.get("detail_interval_minutes", "5")) * 60)))
        except ValueError:
            seconds = 300
        await wait_or_stop(seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    source_task = asyncio.create_task(source_scheduler())
    detail_task = asyncio.create_task(detail_scheduler())
    yield
    stop_event.set()
    await asyncio.gather(source_task, detail_task)


app = FastAPI(title="Nijigasaki DB", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")
if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="frontend-assets")


@app.get("/api/releases")
async def api_releases(q: str = "") -> dict[str, Any]:
    with db() as conn:
        releases = conn.execute("SELECT * FROM releases WHERE title LIKE ? OR artist LIKE ? ORDER BY position", (f"%{q}%", f"%{q}%")).fetchall()
        last = conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    return {"releases": [release_summary(row) for row in releases], "last": dict(last) if last else None, "q": q, "source_url": SOURCE_URL}


@app.get("/api/releases/{release_id}")
async def api_release_detail(release_id: str) -> dict[str, Any]:
    with db() as conn:
        release = conn.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
        previous = conn.execute("SELECT id, title FROM releases WHERE position < ? ORDER BY position DESC LIMIT 1", (release["position"],)).fetchone() if release else None
        following = conn.execute("SELECT id, title FROM releases WHERE position > ? ORDER BY position LIMIT 1", (release["position"],)).fetchone() if release else None
    if not release:
        raise HTTPException(404, "Release not found")
    return {
        "release": release_payload(release),
        "previous": dict(previous) if previous else None,
        "following": dict(following) if following else None,
        "source_url": SOURCE_URL,
    }


@app.get("/api/programs")
async def api_programs() -> dict[str, Any]:
    return {"programs": program_rows()}


@app.get("/api/programs/calendar")
async def api_program_calendar(start: str = "", end: str = "") -> dict[str, Any]:
    today = datetime.now(JAPAN_TZ).date()
    default_start = date(today.year, today.month, 1)
    range_start = calendar_date(start, default_start)
    range_end = calendar_date(end, range_start + timedelta(days=42))
    if end:
        range_end -= timedelta(days=1)
    if range_end < range_start:
        raise HTTPException(400, "日历结束日期不能早于开始日期")
    programs = program_rows()
    events = [event for program in programs for event in program_calendar_events(program, range_start, range_end)]
    return {"events": events, "programs": programs, "start": range_start.isoformat(), "end": range_end.isoformat()}


@app.get("/api/programs/{program_id}/occurrences")
async def api_public_program_occurrences(program_id: str, start: str = "", end: str = "") -> dict[str, Any]:
    program = next((item for item in program_rows() if item["id"] == program_id), None)
    if not program:
        raise HTTPException(404, "节目不存在")
    return program_occurrence_list(program, start, end)


@app.get("/api/programs/{program_id}")
async def api_program_detail(program_id: str) -> dict[str, Any]:
    program = next((item for item in program_rows() if item["id"] == program_id), None)
    if not program:
        raise HTTPException(404, "节目不存在")
    return {"program": program}


@app.get("/api/auth/session")
async def api_session(request: Request) -> dict[str, bool]:
    return {"authenticated": admin_cookie(request)}


@app.post("/api/auth/login")
async def api_login(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求格式无效")
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    if hmac.compare_digest(username, os.getenv("ADMIN_USERNAME", "admin")) and verify_password(password, settings().get("admin_password_hash", "")):
        response = JSONResponse({"authenticated": True})
        set_admin_cookie(response)
        return response
    raise HTTPException(401, "账号或密码错误")


@app.post("/api/auth/logout")
async def api_logout() -> JSONResponse:
    response = JSONResponse({"authenticated": False})
    response.delete_cookie("nijidb_admin")
    return response


@app.get("/api/admin/settings")
async def api_get_settings(request: Request) -> dict[str, Any]:
    require_api_admin(request)
    return {"settings": public_settings(), "activity_logs": recent_database_logs()}


@app.patch("/api/admin/settings")
async def api_save_settings(request: Request) -> dict[str, dict[str, str]]:
    require_api_admin(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求格式无效")
    values = normalized_settings({**settings(), **payload})
    save_settings(values)
    return {"settings": values}


async def restore_validated_database(source_path: Path) -> dict[str, str]:
    async with sync_lock:
        rollback_path = create_persistent_database_backup("before-restore")
        try:
            restore_database_file(source_path)
            init_db()
            remove_file(COVER_CACHE_VERSION_PATH)
        except Exception as exc:
            try:
                restore_database_file(rollback_path)
                init_db()
            except Exception as rollback_error:
                print(f"[backup] rollback failed: {type(rollback_error).__name__}", flush=True)
            raise HTTPException(500, "数据库还原失败，原数据库已保留") from exc
    print("[backup] database restored", flush=True)
    return {"message": "数据库还原成功，下一次同步会重新检查封面缓存"}


@app.get("/api/admin/backups")
async def api_list_backups(request: Request) -> dict[str, Any]:
    require_api_admin(request)
    return {"backups": list_database_backups()}


@app.get("/api/admin/backups/{filename}/download")
async def api_download_stored_backup(filename: str, request: Request) -> FileResponse:
    require_api_admin(request)
    try:
        backup_path = resolve_database_backup(filename)
    except FileNotFoundError as exc:
        raise HTTPException(404, "数据库备份不存在") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return FileResponse(backup_path, media_type="application/vnd.sqlite3", filename=backup_path.name)


@app.post("/api/admin/backups/{filename}/restore")
async def api_restore_stored_backup(filename: str, request: Request) -> dict[str, str]:
    require_api_admin(request)
    try:
        backup_path = resolve_database_backup(filename)
    except FileNotFoundError as exc:
        raise HTTPException(404, "数据库备份不存在") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        validate_database_backup(backup_path)
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        raise HTTPException(400, "数据库备份文件无效") from exc
    return await restore_validated_database(backup_path)


@app.get("/api/admin/backup")
async def api_download_backup(request: Request) -> FileResponse:
    require_api_admin(request)
    async with sync_lock:
        backup_path = create_persistent_database_backup("manual")
    return FileResponse(backup_path, media_type="application/vnd.sqlite3", filename=backup_path.name)


@app.post("/api/admin/backup/restore")
async def api_restore_backup(request: Request) -> dict[str, str]:
    require_api_admin(request)
    upload_path = await save_backup_upload(request)
    try:
        try:
            validate_database_backup(upload_path)
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            raise HTTPException(400, "数据库备份文件无效") from exc

        return await restore_validated_database(upload_path)
    finally:
        remove_file(upload_path)


@app.post("/api/admin/test-onebot")
async def api_test_onebot(request: Request) -> dict[str, str]:
    require_api_admin(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求格式无效")
    config = settings()
    for key in ("onebot_url", "onebot_token", "onebot_target", "onebot_profile"):
        if key in payload:
            config[key] = str(payload[key] or "").strip()
    config["onebot_profile"] = config.get("onebot_profile") or "bot"
    try:
        await send_onebot("[虹咲音乐资料]\nOneBot 测试消息发送成功。", config)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        print(f"[onebot] test failed: {type(exc).__name__}", flush=True)
        raise HTTPException(502, "OneBot 请求失败，请检查接口地址、Token 和接收目标") from exc
    return {"message": "测试消息已发送"}


@app.patch("/api/admin/password")
async def api_change_password(request: Request) -> dict[str, str]:
    require_api_admin(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求格式无效")
    current_password = payload.get("current_password")
    new_password = payload.get("new_password")
    confirm_password = payload.get("confirm_password")
    if not all(isinstance(value, str) for value in (current_password, new_password, confirm_password)):
        raise HTTPException(400, "密码格式无效")
    if not verify_password(current_password, settings().get("admin_password_hash", "")):
        raise HTTPException(400, "当前密码错误")
    if len(new_password) < 8:
        raise HTTPException(400, "新密码至少需要 8 位")
    if len(new_password) > 256:
        raise HTTPException(400, "新密码不能超过 256 位")
    if new_password != confirm_password:
        raise HTTPException(400, "两次输入的新密码不一致")
    save_settings({"admin_password_hash": hash_password(new_password)})
    return {"message": "管理员密码已更新"}


@app.post("/api/admin/sync")
async def api_manual_sync(request: Request) -> dict[str, Any]:
    require_api_admin(request)
    changed, error = await sync_once()
    return {"changed_count": changed, "error": error, "activity_logs": recent_database_logs()}


@app.get("/api/admin/program-json-template")
async def api_program_json_template(request: Request) -> dict[str, Any]:
    require_api_admin(request)
    return program_json_template()


@app.get("/api/admin/programs/{program_id}/export")
async def api_export_program(program_id: str, request: Request, mode: str = "individual") -> dict[str, Any]:
    require_api_admin(request)
    program = next((item for item in program_rows() if item["id"] == program_id), None)
    if not program:
        raise HTTPException(404, "节目不存在")
    try:
        return program_json_export(program, mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/admin/programs/import/preview")
async def api_preview_program_import(request: Request) -> dict[str, Any]:
    require_api_admin(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求格式无效")
    try:
        options = import_payload_options(payload)
        program, occurrences, warnings = normalize_import_payload(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    matches = program_import_matches(options["source_program_id"], program["title"])
    return import_preview_payload(program, occurrences, warnings, options, matches)


@app.post("/api/admin/programs/import")
async def api_import_program(request: Request) -> dict[str, Any]:
    require_api_admin(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求格式无效")
    try:
        options = import_payload_options(payload)
        program_values, occurrence_values, warnings = normalize_import_payload(payload)
        with db() as conn:
            target_id = options["target_program_id"] if options["target_mode"] == "overwrite" else ""
            existing = conn.execute("SELECT id, created_at FROM programs WHERE id = ?", (target_id,)).fetchone() if target_id else None
            if options["target_mode"] == "overwrite" and not existing:
                raise ValueError("覆盖导入需要选择一个存在的目标节目")
            program_values = validate_program_group(conn, program_values, target_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    now = datetime.now(timezone.utc).isoformat()
    overwrite = options["target_mode"] == "overwrite"
    target_id = options["target_program_id"] if overwrite else f"program-{secrets.token_hex(6)}"
    program_values.update({"id": target_id, "created_at": existing["created_at"] if overwrite else now, "updated_at": now})
    automatic_backup_path: Path | None = None
    try:
        async with sync_lock:
            automatic_backup_path = create_persistent_database_backup("before-json-import")
            with db() as conn:
                if overwrite:
                    replace_imported_program_row(conn, program_values, target_id, program_values["created_at"], now)
                else:
                    insert_program_row(conn, program_values)
                for occurrence in occurrence_values:
                    values = {
                        **occurrence,
                        "program_id": program_values["id"],
                        "created_at": now,
                        "updated_at": now,
                    }
                    insert_occurrence_row(conn, values)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "导入的单集存在重复原定日期") from exc

    action = "覆盖节目" if overwrite else "导入节目"
    log_database_activity("program", f"{action}：{program_values['title']}（{len(occurrence_values)} 期）")
    program = next(item for item in program_rows() if item["id"] == program_values["id"])
    return {
        "program": program,
        "warnings": warnings,
        "imported_occurrences": len(occurrence_values),
        "overwritten": overwrite,
        "automatic_backup": {"filename": automatic_backup_path.name} if automatic_backup_path else None,
    }


@app.post("/api/admin/programs")
async def api_create_program(request: Request) -> dict[str, Any]:
    require_api_admin(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求格式无效")
    try:
        values = normalized_program(payload)
        with db() as conn:
            values = validate_program_group(conn, values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    now = datetime.now(timezone.utc).isoformat()
    values.update({"id": f"program-{secrets.token_hex(6)}", "created_at": now, "updated_at": now})
    with db() as conn:
        insert_program_row(conn, values)
    log_database_activity("program", f"新增节目：{values['title']}")
    program = next(item for item in program_rows() if item["id"] == values["id"])
    return {"program": program}


@app.patch("/api/admin/programs/{program_id}")
async def api_update_program(program_id: str, request: Request) -> dict[str, Any]:
    require_api_admin(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求格式无效")
    with db() as conn:
        existing = conn.execute("SELECT * FROM programs WHERE id = ?", (program_id,)).fetchone()
    if not existing:
        raise HTTPException(404, "节目不存在")
    try:
        values = normalized_program({**dict(existing), **payload})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    values.update({"id": program_id, "updated_at": datetime.now(timezone.utc).isoformat()})
    with db() as conn:
        try:
            values = validate_program_group(conn, values, program_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        old_periods = [
            period_payload(row)
            for row in conn.execute("SELECT * FROM program_periods WHERE program_id = ? ORDER BY start_date, id", (program_id,)).fetchall()
        ]
        conn.execute("""UPDATE programs SET
            title=:title, status=:status, category=:category, format=:format, platform=:platform, delivery=:delivery, auto_generate=:auto_generate,
            people=:people, official_url=:official_url, description=:description,
            frequency=:frequency, week_interval=:week_interval, monthly_mode=:monthly_mode,
            week_index=:week_index, weekday=:weekday, schedule_time=:schedule_time,
            start_date=:start_date, end_date=:end_date, parent_id=:parent_id, subprogram_name=:subprogram_name, episode_start=:episode_start, updated_at=:updated_at
            WHERE id=:id""", values)
        if not values["parent_id"]:
            conn.execute(
                "UPDATE programs SET title = ?, updated_at = ? WHERE parent_id = ?",
                (values["title"], values["updated_at"], program_id),
            )
        backfill_individual_occurrence_anchors(conn, program_id, old_periods, values["periods"])
        replace_program_periods(conn, program_id, values["periods"], values["updated_at"])
    log_database_activity("program", f"更新节目：{values['title']}")
    program = next(item for item in program_rows() if item["id"] == program_id)
    return {"program": program}


@app.delete("/api/admin/programs/{program_id}")
async def api_delete_program(program_id: str, request: Request) -> dict[str, str]:
    require_api_admin(request)
    with db() as conn:
        program = conn.execute("SELECT id, title, parent_id FROM programs WHERE id = ?", (program_id,)).fetchone()
        if not program:
            raise HTTPException(404, "节目不存在")
        if not program["parent_id"] and conn.execute("SELECT 1 FROM programs WHERE parent_id = ? LIMIT 1", (program_id,)).fetchone():
            raise HTTPException(409, "请先删除该主节目下的子节目")
        conn.execute("DELETE FROM program_periods WHERE program_id = ?", (program_id,))
        conn.execute("DELETE FROM program_occurrences WHERE program_id = ?", (program_id,))
        result = conn.execute("DELETE FROM programs WHERE id = ?", (program_id,))
    if result.rowcount == 0:
        raise HTTPException(404, "节目不存在")
    log_database_activity("program", f"删除节目：{program['title']}")
    return {"message": "节目已删除"}


@app.patch("/api/admin/programs/{program_id}/auto-generation")
async def api_update_auto_generation(program_id: str, request: Request) -> dict[str, Any]:
    require_api_admin(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(payload, dict) or "auto_generate" not in payload:
        raise HTTPException(400, "自动生成设置无效")
    auto_generate = boolean_value(payload["auto_generate"], True)
    program = next((item for item in program_rows() if item["id"] == program_id), None)
    if not program:
        raise HTTPException(404, "节目不存在")
    with db() as conn:
        materialized_count = materialize_generated_occurrences(conn, program) if not auto_generate else 0
        conn.execute(
            "UPDATE programs SET auto_generate = ?, updated_at = ? WHERE id = ?",
            (int(auto_generate), datetime.now(timezone.utc).isoformat(), program_id),
        )
    state = "开启" if auto_generate else "关闭"
    suffix = f"，保存 {materialized_count} 期" if materialized_count else ""
    log_database_activity("program", f"{state}自动生成：{program['title']}{suffix}")
    return {"auto_generate": auto_generate, "materialized_count": materialized_count}


def program_occurrence_list(program: dict[str, Any], start: str = "", end: str = "") -> dict[str, Any]:
    today = datetime.now(JAPAN_TZ).date()
    program_start = calendar_date(program.get("start_date", ""), today)
    default_start = program_start
    default_end = calendar_date(program.get("end_date", ""), today + timedelta(days=PROGRAM_FORECAST_DAYS))
    if default_start > default_end:
        default_end = default_start + timedelta(days=PROGRAM_FORECAST_DAYS)
    range_start = calendar_date(start, default_start)
    range_end = calendar_date(end, default_end)
    if range_end < range_start:
        raise HTTPException(400, "排期结束日期不能早于开始日期")
    records = sorted(program_occurrence_records(program, program_start, range_end), key=lambda item: item["original_date"])
    episode_numbers = occurrence_episode_numbers(records, program_episode_start(program))
    visible_records = [
        (episode_numbers[index], record)
        for index, record in enumerate(records)
        if range_start <= record["date"] <= range_end or record["id"]
    ]
    return {
        "occurrences": [
            {
                **record,
                "date": record["date"].isoformat(),
                "episode": episode_number,
            }
            for episode_number, record in sorted(visible_records, key=lambda item: (item[1]["date"], item[1]["time"], item[1]["original_date"]))
        ],
        "start": range_start.isoformat(),
        "end": range_end.isoformat(),
    }


@app.get("/api/admin/programs/{program_id}/occurrences")
async def api_program_occurrences(program_id: str, request: Request, start: str = "", end: str = "") -> dict[str, Any]:
    require_api_admin(request)
    program = next((item for item in program_rows() if item["id"] == program_id), None)
    if not program:
        raise HTTPException(404, "节目不存在")
    return program_occurrence_list(program, start, end)


@app.post("/api/admin/programs/{program_id}/occurrences")
async def api_create_occurrence(program_id: str, request: Request) -> dict[str, Any]:
    require_api_admin(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求格式无效")
    try:
        values = normalized_occurrence(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    now = datetime.now(timezone.utc).isoformat()
    values.update({"program_id": program_id, "created_at": now, "updated_at": now})
    with db() as conn:
        program = conn.execute("SELECT title FROM programs WHERE id = ?", (program_id,)).fetchone()
        if not program:
            raise HTTPException(404, "节目不存在")
        try:
            cursor = insert_occurrence_row(conn, values)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "该原定日期已经有单集调整") from exc
        row = conn.execute("SELECT * FROM program_occurrences WHERE id = ?", (cursor.lastrowid,)).fetchone()
    log_database_activity("program", f"新增单集：{program['title']}（{values['original_date']}）")
    return {"occurrence": occurrence_payload(row)}


@app.post("/api/admin/programs/{program_id}/occurrences/restore-rescheduled")
async def api_restore_rescheduled_occurrences(program_id: str, request: Request) -> dict[str, int]:
    require_api_admin(request)
    with db() as conn:
        program = conn.execute("SELECT title FROM programs WHERE id = ?", (program_id,)).fetchone()
        if not program:
            raise HTTPException(404, "节目不存在")
        try:
            # Keep the old date as the generation key so monthly schedules do not duplicate the row.
            conn.execute(
                """UPDATE program_occurrences
                   SET generated_date = original_date, shift_following_days = 0
                   WHERE program_id = ? AND generated_date = ''
                     AND (status = 'rescheduled' OR adjusted_date != '' OR adjusted_time != '')""",
                (program_id,),
            )
            result = conn.execute(
                """UPDATE program_occurrences
                   SET original_date = CASE WHEN adjusted_date != '' THEN adjusted_date ELSE original_date END,
                       original_time = CASE WHEN adjusted_time != '' THEN adjusted_time ELSE original_time END,
                       status = 'scheduled', adjusted_date = '', adjusted_time = '', shift_following_days = 0, updated_at = ?
                   WHERE program_id = ?
                     AND (status = 'rescheduled' OR adjusted_date != '' OR adjusted_time != '')""",
                (datetime.now(timezone.utc).isoformat(), program_id),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "改期时间覆盖后出现重复的原定日期") from exc
    if result.rowcount:
        log_database_activity("program", f"恢复改期单集：{program['title']}（{result.rowcount} 期）")
    return {"count": result.rowcount}


@app.patch("/api/admin/programs/{program_id}/occurrences/{occurrence_id}")
async def api_update_occurrence(program_id: str, occurrence_id: int, request: Request) -> dict[str, Any]:
    require_api_admin(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求格式无效")
    with db() as conn:
        existing = conn.execute("SELECT * FROM program_occurrences WHERE id = ? AND program_id = ?", (occurrence_id, program_id)).fetchone()
        program = conn.execute("SELECT title FROM programs WHERE id = ?", (program_id,)).fetchone()
    if not existing:
        raise HTTPException(404, "单集排期不存在")
    if not program:
        raise HTTPException(404, "节目不存在")
    try:
        values = normalized_occurrence({**dict(existing), **payload})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    values.update({"id": occurrence_id, "program_id": program_id, "updated_at": datetime.now(timezone.utc).isoformat()})
    with db() as conn:
        try:
            conn.execute("""UPDATE program_occurrences SET
                original_date=:original_date, title=:title, generated_date=:generated_date, original_time=:original_time, delivery=:delivery, shift_following_days=:shift_following_days, status=:status,
                source_url=:source_url, mirror_url=:mirror_url, subtitle_url=:subtitle_url,
                adjusted_date=:adjusted_date, adjusted_time=:adjusted_time, note=:note, guests=:guests, special=:special, updated_at=:updated_at
                WHERE id=:id AND program_id=:program_id""", values)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "该原定日期已经有单集调整") from exc
        row = conn.execute("SELECT * FROM program_occurrences WHERE id = ?", (occurrence_id,)).fetchone()
    log_database_activity("program", f"修改单集：{program['title']}（{values['original_date']}）")
    return {"occurrence": occurrence_payload(row)}


@app.delete("/api/admin/programs/{program_id}/occurrences/{occurrence_id}")
async def api_delete_occurrence(program_id: str, occurrence_id: int, request: Request) -> dict[str, str]:
    require_api_admin(request)
    with db() as conn:
        program = conn.execute("SELECT title FROM programs WHERE id = ?", (program_id,)).fetchone()
        result = conn.execute(
            """UPDATE program_occurrences
               SET status = 'deleted', adjusted_date = '', adjusted_time = '', shift_following_days = 0, updated_at = ?
               WHERE id = ? AND program_id = ?""",
            (datetime.now(timezone.utc).isoformat(), occurrence_id, program_id),
        )
    if result.rowcount == 0:
        raise HTTPException(404, "单集排期不存在")
    if program:
        log_database_activity("program", f"删除单集：{program['title']}（编号 {occurrence_id}）")
    return {"message": "单集已删除"}


@app.get("/rainbow.svg", include_in_schema=False)
async def rainbow_favicon():
    return FileResponse(FRONTEND_DIST / "rainbow.svg", media_type="image/svg+xml")


@app.get("/{path:path}")
async def frontend(path: str):
    index = FRONTEND_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(503, "Vue 前端尚未构建，请运行 npm install && npm run build")
    return FileResponse(index)
