#!/usr/bin/env python3
"""Safely convert only PNG objects referenced by Nijidb SQLite/R2 URLs.

Default operation is a read-only inventory. --apply requires the explicit
--delete-old-png switch: it snapshots SQLite and the source objects, uploads
and verifies JPEGs, updates all matching DB URLs in one transaction, confirms
no DB references remain, and only then deletes those exact old R2 keys.

Run inside the dedicated worker image with /data and /recovery mounted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError

DATABASE = Path("/data/nijidb.sqlite3")
MEDIA_ROOT = Path("/data/images")
WORK_ROOT = Path(os.getenv("R2_PNG_JPG_WORKDIR", "/recovery"))
MAX_SOURCE_BYTES = 100 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
SHA256_NAME = re.compile(r"^[0-9a-f]{64}$")
URL_END = set(" \t\r\n\"'<>)]}")
SUPPORTED_TEXT_FIELDS = {
    "program_occurrence_images": {"public_url"},
    "news_images": {"public_url"},
    "collaboration_images": {"public_url", "source_url"},
    "releases": {"cover_url", "detail_html"},
    "news_articles": {"body_markdown", "summary"},
}
IMAGE_TABLES = {"program_occurrence_images", "news_images", "collaboration_images"}


def emit(**values: Any) -> None:
    print(json.dumps(values, ensure_ascii=False, separators=(",", ":")), flush=True)


def set_recovery_owner(path: Path) -> None:
    uid = int(os.environ.get("R2_PNG_JPG_UID", "-1"))
    gid = int(os.environ.get("R2_PNG_JPG_GID", "-1"))
    if uid >= 0 and gid >= 0:
        os.chown(path, uid, gid)


def write_bytes(path: Path, value: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if path.read_bytes() != value:
            raise RuntimeError("A migration artifact already exists with different contents")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, mode)
    set_recovery_owner(path)
    set_recovery_owner(path.parent)


def write_json(path: Path, value: Any) -> None:
    body = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    set_recovery_owner(path)
    set_recovery_owner(path.parent)


def atomic_write(path: Path, data: bytes) -> None:
    write_bytes(path, data)


def atomic_json(path: Path, value: Any) -> None:
    write_json(path, value)


def json_line(value: dict[str, Any]) -> None:
    emit(**value)


def db_connection(readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=30)
        connection.execute("PRAGMA query_only = ON")
    else:
        connection = sqlite3.connect(DATABASE, timeout=60)
    connection.row_factory = sqlite3.Row
    return connection


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def r2_config() -> tuple[Any, str, str, str]:
    names = ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_PUBLIC_BASE_URL")
    values = {name: os.getenv(name, "").strip() for name in names}
    if any(not value for value in values.values()):
        raise RuntimeError("R2 runtime configuration is incomplete")
    parsed = urlsplit(values["R2_PUBLIC_BASE_URL"])
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("R2_PUBLIC_BASE_URL is invalid")
    prefix = os.getenv("R2_IMAGE_PREFIX", "images").strip("/")
    if not prefix or any(part in {"", ".", ".."} for part in prefix.split("/")) or "\\" in prefix:
        raise RuntimeError("R2_IMAGE_PREFIX is invalid")
    client = boto3.client(
        "s3",
        endpoint_url=values["R2_ENDPOINT"],
        aws_access_key_id=values["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=values["R2_SECRET_ACCESS_KEY"],
        region_name=os.getenv("R2_REGION", "auto"),
        config=Config(connect_timeout=10, read_timeout=90, retries={"max_attempts": 3, "mode": "standard"}),
    )
    return client, values["R2_BUCKET"], prefix, values["R2_PUBLIC_BASE_URL"].rstrip("/")


def key_from_public_url(value: str, base_url: str, prefix: str) -> str | None:
    try:
        candidate = urlsplit(str(value or "").strip())
        base = urlsplit(base_url)
        if (
            candidate.scheme.lower() != base.scheme.lower()
            or (candidate.hostname or "").lower() != (base.hostname or "").lower()
            or candidate.port != base.port
            or candidate.username
            or candidate.password
            or candidate.query
            or candidate.fragment
        ):
            return None
        path = unquote(candidate.path)
        base_path = unquote(base.path).rstrip("/")
    except (TypeError, ValueError):
        return None
    root = f"{base_path}/{prefix}/" if base_path else f"/{prefix}/"
    if not path.startswith(root):
        return None
    relative = path[len(root) :]
    if (
        not relative
        or len(relative) > 900
        or "\\" in relative
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in relative)
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or not relative.lower().endswith(".png")
    ):
        return None
    return f"{prefix}/{relative}"


def png_urls_in_text(value: Any, base_url: str, prefix: str) -> list[tuple[str, str]]:
    text = str(value or "")
    found = []
    cursor = 0
    while True:
        start = text.find(base_url, cursor)
        if start < 0:
            break
        end = start
        while end < len(text) and text[end] not in URL_END:
            end += 1
        url = text[start:end].rstrip(".,;:!")
        key = key_from_public_url(url, base_url, prefix)
        if key:
            found.append((url, key))
        cursor = max(end, start + len(base_url))
    return found


def scan_db_references(connection: sqlite3.Connection, base_url: str, prefix: str) -> list[dict[str, Any]]:
    found: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    tables = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for table_row in tables:
        table, create_sql = table_row["name"], str(table_row["sql"] or "")
        if create_sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise RuntimeError("Database contains an unsafe table identifier")
        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        for column_row in columns:
            column, datatype = column_row[1], str(column_row[2] or "").upper()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column):
                raise RuntimeError("Database contains an unsafe column identifier")
            if not any(kind in datatype for kind in ("TEXT", "CHAR", "CLOB", "JSON")):
                continue
            try:
                rows = connection.execute(
                    f'SELECT rowid AS "__migration_rowid__", "{column}" FROM "{table}" WHERE "{column}" LIKE ?',
                    ("%" + base_url + "%",),
                ).fetchall()
            except sqlite3.Error as exc:
                raise RuntimeError(f"Could not safely scan {table}.{column}") from exc
            for row in rows:
                rowid = int(row["__migration_rowid__"])
                value = row[column]
                for url, key in png_urls_in_text(value, base_url, prefix):
                    identity = (table, rowid, column, url)
                    metadata = {}
                    if table in IMAGE_TABLES:
                        try:
                            record = connection.execute(f'SELECT * FROM "{table}" WHERE rowid = ?', (rowid,)).fetchone()
                        except sqlite3.Error as exc:
                            raise RuntimeError(f"Could not safely inspect {table} image metadata") from exc
                        if record:
                            record_columns = set(record.keys())
                            metadata = {
                                name: record[name]
                                for name in ("local_path", "path", "sha256", "bytes", "width", "height")
                                if name in record_columns
                            }
                    found[identity] = {
                        "table": table,
                        "rowid": rowid,
                        "column": column,
                        "old_url": url,
                        "key": key,
                        "metadata": metadata,
                        "supported": column in SUPPORTED_TEXT_FIELDS.get(table, set()),
                    }
    return sorted(
        found.values(), key=lambda item: (item["key"], item["table"], item["rowid"], item["column"], item["old_url"])
    )


def reference_digest(references: list[dict[str, Any]]) -> str:
    body = json.dumps(references, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return sha256(body)


def list_objects(client: Any, bucket: str, prefix: str) -> dict[str, dict[str, Any]]:
    result = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "/"):
        for item in page.get("Contents", []):
            result[item["Key"]] = {"size": int(item["Size"]), "etag": str(item.get("ETag") or "").strip('"')}
    return result


def make_plan(client: Any, bucket: str, prefix: str, base_url: str) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        references = scan_db_references(connection, base_url, prefix)
    finally:
        connection.close()
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for reference in references:
        by_key[reference["key"]].append(reference)
    objects = list_objects(client, bucket, prefix)
    missing = sorted(key for key in by_key if key not in objects)
    targets = sorted(key for key in by_key if key in objects)
    orphan_pngs = sorted(key for key in objects if key.lower().endswith(".png") and key not in by_key)
    unsupported = [reference for reference in references if not reference["supported"]]
    return {
        "version": 1,
        "bucket": bucket,
        "prefix": prefix,
        "public_base": base_url,
        "reference_digest": reference_digest(references),
        "references": references,
        "objects": [{"key": key, **objects[key], "references": by_key[key]} for key in targets],
        "missing": missing,
        "unsupported": unsupported,
        "orphan_pngs": orphan_pngs,
        "summary": {
            "referenced_png_objects": len(targets),
            "referenced_png_bytes": sum(objects[key]["size"] for key in targets),
            "database_reference_rows": len(references),
            "references_by_field": dict(
                sorted(Counter(ref["table"] + "." + ref["column"] for ref in references).items())
            ),
            "orphan_png_objects_left_untouched": len(orphan_pngs),
        },
    }


def probe_png(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_SOURCE_BYTES or not data.startswith(PNG_SIGNATURE):
        raise ValueError("PNG object signature/size is invalid")
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-f",
            "image2pipe",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,nb_frames",
            "-of",
            "json",
            "pipe:0",
        ],
        input=data,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if process.returncode:
        raise RuntimeError("ffprobe failed to decode a PNG")
    streams = json.loads(process.stdout).get("streams", [])
    if not streams:
        raise RuntimeError("PNG contains no video/image stream")
    stream = streams[0]
    width, height = int(stream["width"]), int(stream["height"])
    if width < 1 or height < 1 or width > 10000 or height > 10000:
        raise RuntimeError("PNG image dimensions are outside the supported limits")
    return {"width": width, "height": height, "codec": stream.get("codec_name", ""), "frames": stream.get("nb_frames")}


def convert_png(data: bytes, dimensions: dict[str, Any]) -> bytes:
    width, height = dimensions["width"], dimensions["height"]
    process = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            f"color=c=white:s={width}x{height}:r=1",
            "-f",
            "image2pipe",
            "-i",
            "pipe:0",
            "-filter_complex",
            "[0:v][1:v]overlay=shortest=1:format=auto,format=yuvj420p",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ],
        input=data,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if process.returncode or not process.stdout.startswith(JPEG_SIGNATURE):
        raise RuntimeError("ffmpeg failed to create a JPEG")
    return process.stdout


def r2_head(client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise RuntimeError("R2 object HEAD failed") from exc


def object_sha(
    client: Any, bucket: str, key: str, head: dict[str, Any] | None = None
) -> tuple[str | None, bytes | None]:
    response = client.get_object(Bucket=bucket, Key=key)
    data = response["Body"].read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise RuntimeError("R2 object exceeds the migration size limit")
    if head and len(data) != int(head.get("ContentLength", -1)):
        raise RuntimeError("R2 object size changed while reading")
    return sha256(data), data


def backup_png(path: Path, data: bytes, expected_size: int) -> str:
    if len(data) != expected_size or not data.startswith(PNG_SIGNATURE):
        raise RuntimeError("PNG rollback copy failed signature or size verification")
    write_bytes(path, data)
    return sha256(data)


def preferred_jpeg_key(old_key: str, digest: str, prefix: str, references: list[dict[str, Any]]) -> str:
    candidates = set()
    for ref in references:
        meta = ref["metadata"]
        if ref["table"] in {"program_occurrence_images", "news_images"}:
            local = str(meta.get("local_path") or "")
            if local and old_key == prefix + "/" + local:
                new_local = str(PurePosixPath(local).with_name(digest + ".jpg"))
                candidates.add(prefix + "/" + new_local)
        elif ref["table"] == "collaboration_images":
            local = str(meta.get("path") or "")
            if local and old_key == prefix + "/illustrations/" + local:
                new_local = str(PurePosixPath(local).with_name(digest + ".jpg"))
                candidates.add(prefix + "/illustrations/" + new_local)
    if len(candidates) > 1:
        raise RuntimeError("An R2 object maps to multiple incompatible local paths")
    if candidates:
        return candidates.pop()
    relative = old_key[len(prefix) + 1 :]
    directory, filename = relative.rsplit("/", 1) if "/" in relative else ("", relative)
    stem = filename[:-4]
    name = (digest if SHA256_NAME.fullmatch(stem.lower()) else stem) + ".jpg"
    new_relative = f"{directory}/{name}" if directory else name
    return prefix + "/" + new_relative


def choose_target_key(client: Any, bucket: str, preferred: str, digest: str, size: int) -> tuple[str, bool]:
    candidate = preferred
    head = r2_head(client, bucket, candidate)
    if head is not None:
        metadata = {str(k).lower(): str(v).lower() for k, v in head.get("Metadata", {}).items()}
        if metadata.get("sha256") == digest and int(head.get("ContentLength", -1)) == size:
            return candidate, True
        existing_hash, _ = object_sha(client, bucket, candidate, head)
        if existing_hash == digest:
            return candidate, True
        directory, filename = candidate.rsplit("/", 1) if "/" in candidate else ("", candidate)
        stem = filename[:-4] if filename.lower().endswith(".jpg") else filename
        suffix = f"{stem}-{digest[:12]}.jpg"
        candidate = f"{directory}/{suffix}" if directory else suffix
        head = r2_head(client, bucket, candidate)
        if head is not None:
            existing_hash, existing_bytes = object_sha(client, bucket, candidate, head)
            if existing_hash == digest and len(existing_bytes or b"") == size:
                return candidate, True
            raise RuntimeError("JPEG collision-safe key also contains different data")
    return candidate, False


def choose_target(client: Any, bucket: str, preferred: str, digest: str, size: int) -> tuple[str, bool]:
    return choose_target_key(client, bucket, preferred, digest, size)


def local_destination_for_ref(ref: dict[str, Any], old_key: str, new_key: str, prefix: str) -> tuple[str, str] | None:
    metadata = ref.get("metadata", {})
    if ref["table"] in {"program_occurrence_images", "news_images"}:
        local = str(metadata.get("local_path") or "")
        if local and old_key == prefix + "/" + local:
            return "media", new_key[len(prefix) + 1 :]
    if ref["table"] == "collaboration_images":
        local = str(metadata.get("path") or "")
        expected = prefix + "/illustrations/" + local if local else ""
        if local and old_key == expected:
            return "illustrations", new_key[len(prefix + "/illustrations/") :]
    return None


def write_local_file(root: Path, relative: str, data: bytes, digest: str) -> None:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError("Unsafe converted local image path")
    if not SHA256_NAME.fullmatch(path.stem):
        raise RuntimeError("Converted local image key must be content-addressed")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise RuntimeError("Local image root is a symlink")
    current = root
    for part in path.parts[:-1]:
        current = current / part
        current.mkdir(exist_ok=True)
        if current.is_symlink():
            raise RuntimeError("Converted local image path contains a symlink")
    target = root.joinpath(*path.parts)
    if target.exists():
        if sha256(target.read_bytes()) != digest:
            raise RuntimeError("Converted local image target contains different data")
        return
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("Local image write failed")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if sha256(target.read_bytes()) != digest:
        raise RuntimeError("Converted local image verification failed")


def verify_public_jpeg(client: httpx.Client, url: str) -> None:
    with client.stream("GET", url, headers={"Range": "bytes=0-2", "Accept": "image/jpeg"}) as response:
        if response.status_code not in {200, 206} or not response.headers.get("content-type", "").lower().startswith(
            "image/jpeg"
        ):
            raise RuntimeError("Public R2 JPEG verification failed")
        initial = next(response.iter_bytes(chunk_size=3), b"")
        if not initial.startswith(JPEG_SIGNATURE):
            raise RuntimeError("Public image is not JPEG encoded")


def online_backup(path: Path) -> None:
    if path.exists():
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Existing SQLite backup is corrupt")
        finally:
            conn.close()
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    src = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(path, timeout=30)
    try:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite online backup integrity failed")
    except Exception:
        dst.close()
        src.close()
        path.unlink(missing_ok=True)
        raise
    else:
        dst.close()
        src.close()
    os.chmod(path, 0o600)
    set_recovery_owner(path)
    counts = {}
    verify = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table in (
            "programs",
            "program_periods",
            "program_occurrences",
            "program_occurrence_images",
            "news_images",
            "collaboration_images",
            "releases",
        ):
            counts[table] = verify.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
    finally:
        verify.close()
    emit(
        phase="sqlite-backup",
        bytes=path.stat().st_size,
        mode=oct(stat.S_IMODE(path.stat().st_mode)),
        integrity="ok",
        row_counts=counts,
    )


def backup_database(path: Path) -> None:
    online_backup(path)


def old_url_to_new_map(plan: dict[str, Any], objects: dict[str, dict[str, Any]]) -> dict[str, str]:
    result = {}
    for item in plan["objects"]:
        obj = objects[item["key"]]
        new_url = plan["public_base"] + "/" + quote(obj["new_key"], safe="/")
        for ref in item["references"]:
            previous = result.get(ref["old_url"])
            if previous and previous != new_url:
                raise RuntimeError("One public PNG URL maps to conflicting JPEG URLs")
            result[ref["old_url"]] = new_url
    return result


def update_database(plan: dict[str, Any], objects: dict[str, dict[str, Any]], prefix: str) -> dict[str, int]:
    references = scan_db_references(db_connection(readonly=True), plan["public_base"], plan["prefix"])
    if reference_digest(references) != plan["reference_digest"]:
        raise RuntimeError("PNG references changed during conversion; refusing stale database update")
    url_map = old_url_to_new_map(plan, objects)
    metadata_by_row: dict[tuple[str, int], dict[str, Any]] = {}
    for item in plan["objects"]:
        image = objects[item["key"]]
        staged = WORK_ROOT / "converted" / (sha256(item["key"].encode()) + ".jpg")
        jpeg = staged.read_bytes()
        if len(jpeg) != image["bytes"] or sha256(jpeg) != image["sha256"]:
            raise RuntimeError("Staged local JPEG failed verification before SQLite update")
        for ref in item["references"]:
            destination = local_destination_for_ref(ref, item["key"], image["new_key"], prefix)
            if destination:
                root = MEDIA_ROOT / "illustrations" if destination[0] == "illustrations" else MEDIA_ROOT
                write_local_file(root, destination[1], jpeg, image["sha256"])
    for item in plan["objects"]:
        image = objects[item["key"]]
        for ref in item["references"]:
            table = ref["table"]
            meta = ref["metadata"]
            local_path = ""
            local_root = ""
            if table in {"program_occurrence_images", "news_images"}:
                old_local = str(meta.get("local_path") or "")
                if old_local and item["key"] == plan["prefix"] + "/" + old_local:
                    local_root = "media"
                    local_path = image["new_key"][len(plan["prefix"]) + 1 :]
            elif table == "collaboration_images":
                old_local = str(meta.get("path") or "")
                expected = plan["prefix"] + "/illustrations/" + old_local if old_local else ""
                if old_local and item["key"] == expected:
                    local_root = "illustrations"
                    local_path = image["new_key"][len(plan["prefix"] + "/illustrations/") :]
            key = (table, ref["rowid"])
            row = metadata_by_row.setdefault(key, {"table": table, "rowid": ref["rowid"], "fields": set()})
            if local_path:
                if row.get("local_path") and row["local_path"] != local_path:
                    raise RuntimeError("One image row maps to multiple converted local paths")
                row.update(
                    {
                        "local_path": local_path,
                        "local_root": local_root,
                        "sha256": image["sha256"],
                        "bytes": image["bytes"],
                        "width": image["width"],
                        "height": image["height"],
                    }
                )
    conn = db_connection(readonly=False)
    changed = Counter()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for ref in references:
            table, column, rowid, old_url = ref["table"], ref["column"], ref["rowid"], ref["old_url"]
            current = conn.execute(f'SELECT "{column}" FROM "{table}" WHERE rowid = ?', (rowid,)).fetchone()
            if current is None or old_url not in str(current[0] or ""):
                raise RuntimeError("A database PNG reference changed before update")
            replacement = str(current[0]).replace(old_url, url_map[old_url])
            conn.execute(f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?', (replacement, rowid))
            changed[table + "." + column] += 1
        for (table, rowid), item in metadata_by_row.items():
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            assignments, values = [], []
            local_column = "path" if table == "collaboration_images" else "local_path"
            if item.get("local_path") and local_column in columns:
                assignments.append(f'"{local_column}" = ?')
                values.append(item["local_path"])
            for column, value in (
                ("sha256", item.get("sha256")),
                ("bytes", item.get("bytes")),
                ("width", item.get("width")),
                ("height", item.get("height")),
            ):
                if value is not None and column in columns:
                    assignments.append(f'"{column}" = ?')
                    values.append(value)
            if assignments:
                conn.execute(f'UPDATE "{table}" SET {", ".join(assignments)} WHERE rowid = ?', [*values, rowid])
                changed[table + ".image_metadata"] += 1
        leftovers = scan_db_references(conn, plan["public_base"], plan["prefix"])
        old_keys = {item["key"] for item in plan["objects"]}
        if any(ref["key"] in old_keys for ref in leftovers):
            raise RuntimeError("Old PNG URLs remain in SQLite; transaction rolled back")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity failed; transaction rolled back")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return dict(changed)


def references_are_migrated(plan: dict[str, Any], converted: dict[str, dict[str, Any]]) -> bool:
    if set(converted) != {item["key"] for item in plan["objects"]}:
        return False
    connection = db_connection(readonly=True)
    try:
        current = scan_db_references(connection, plan["public_base"], plan["prefix"])
        old_keys = {item["key"] for item in plan["objects"]}
        if any(ref["key"] in old_keys for ref in current):
            return False
        for item in plan["objects"]:
            new_url = converted[item["key"]]["new_url"]
            for ref in item["references"]:
                row = connection.execute(
                    f'SELECT "{ref["column"]}" FROM "{ref["table"]}" WHERE rowid = ?', (ref["rowid"],)
                ).fetchone()
                if row is None or ref["old_url"] in str(row[0] or "") or new_url not in str(row[0] or ""):
                    return False
        return True
    finally:
        connection.close()


def migrate(args: argparse.Namespace) -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if WORK_ROOT.is_symlink():
        raise RuntimeError("Recovery directory must not be a symlink")
    os.chmod(WORK_ROOT, 0o700)
    set_recovery_owner(WORK_ROOT)
    client, bucket, prefix, public_base = r2_config()
    plan_path = WORK_ROOT / "plan.json"
    state_path = WORK_ROOT / "object-mapping.json"
    if args.dry_run:
        if plan_path.exists():
            raise RuntimeError("A plan already exists; use a new recovery directory")
        plan = make_plan(client, bucket, prefix, public_base)
        if plan["missing"]:
            raise RuntimeError("Database references PNG objects missing from R2")
        if plan["unsupported"]:
            fields = sorted({ref["table"] + "." + ref["column"] for ref in plan["unsupported"]})
            raise RuntimeError("R2 PNG URL references in unsupported DB fields: " + ", ".join(fields))
        atomic_json(plan_path, plan)
        json_line(
            {
                "phase": "dry-run",
                **plan["summary"],
                "r2_png_objects_not_referenced_and_will_be_left_untouched": len(plan["orphan_pngs"]),
                "missing_keys": len(plan["missing"]),
                "unsupported_fields": len(plan["unsupported"]),
                "examples": [obj["key"] for obj in plan["objects"][:6]],
            }
        )
        return
    if not args.delete_old_png:
        raise RuntimeError("--apply requires --delete-old-png")
    if not plan_path.exists():
        raise RuntimeError("Run --dry-run and review its plan before applying")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("bucket") != bucket or plan.get("prefix") != prefix or plan.get("public_base") != public_base:
        raise RuntimeError("R2 configuration changed since dry-run")
    state_path = WORK_ROOT / "object-mapping.json"
    conversion_state = json.loads(state_path.read_text()) if state_path.exists() else {"version": 1, "objects": {}}
    completed = conversion_state["objects"]
    already_switched = references_are_migrated(plan, completed)
    completion_marker = WORK_ROOT / "database-migration-complete.json"
    if completion_marker.exists() and not already_switched:
        raise RuntimeError("Migration completion marker disagrees with SQLite references")
    if not already_switched:
        current = make_plan(client, bucket, prefix, public_base)
        if current["reference_digest"] != plan["reference_digest"]:
            raise RuntimeError("Database references changed since dry-run; create a fresh plan")
        if current["missing"] or current["unsupported"]:
            raise RuntimeError("Current DB/R2 inventory is not safe to apply")
        by_key = {item["key"]: item for item in current["objects"]}
        expected = {item["key"]: item for item in plan["objects"]}
        if set(by_key) != set(expected) or any(
            by_key[key]["size"] != expected[key]["size"] or by_key[key]["etag"] != expected[key]["etag"]
            for key in expected
        ):
            raise RuntimeError("R2 PNG objects changed since dry-run; create a fresh plan")
    backup_path = WORK_ROOT / "database-before.sqlite3"
    if not (WORK_ROOT / "database-backup.json").exists():
        backup_database(backup_path)
        atomic_json(
            WORK_ROOT / "database-backup.json",
            {"file": backup_path.name, "integrity": "ok", "bytes": backup_path.stat().st_size},
        )
    else:
        if not backup_path.exists():
            raise RuntimeError("Migration backup marker exists but the SQLite backup is missing")
        check = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Migration SQLite backup failed integrity check")
        finally:
            check.close()
    http = httpx.Client(timeout=30, trust_env=False, follow_redirects=False)
    try:
        if already_switched:
            for item in plan["objects"]:
                migrated = completed[item["key"]]
                head = r2_head(client, bucket, migrated["new_key"])
                if head is None or int(head.get("ContentLength", -1)) != migrated["bytes"]:
                    raise RuntimeError("A migrated JPEG is missing while resuming PNG cleanup")
            emit(phase="sqlite-update", already_complete=True)
        else:
            for index, item in enumerate(plan["objects"], 1):
                old_key = item["key"]
                key_hash = sha256(old_key.encode())
                original_path = WORK_ROOT / "originals" / (key_hash + ".png")
                converted_path = WORK_ROOT / "converted" / (key_hash + ".jpg")
                state = completed.get(old_key)
                if state and converted_path.exists() and sha256(converted_path.read_bytes()) == state.get("sha256"):
                    jpeg = converted_path.read_bytes()
                    details = state["image"]
                else:
                    head = r2_head(client, bucket, old_key)
                    if head is None or int(head.get("ContentLength", -1)) != item["size"]:
                        raise RuntimeError("A source PNG disappeared or changed size")
                    if head.get("ETag") and item.get("etag") and str(head["ETag"]).strip('"') != item["etag"]:
                        raise RuntimeError("A source PNG changed since dry-run")
                    if original_path.exists():
                        png = original_path.read_bytes()
                        if len(png) != item["size"]:
                            raise RuntimeError("Saved rollback PNG has unexpected size")
                    else:
                        png = client.get_object(Bucket=bucket, Key=old_key)["Body"].read(MAX_SOURCE_BYTES + 1)
                        if len(png) > MAX_SOURCE_BYTES:
                            raise RuntimeError("PNG object exceeds conversion size limit")
                        backup_png(original_path, png, item["size"])
                    details = probe_png(png)
                    jpeg = convert_png(png, details)
                    atomic_write(converted_path, jpeg)
                digest = sha256(jpeg)
                new_key = preferred_jpeg_key(old_key, digest, prefix, item["references"])
                new_key, reused = choose_target(client, bucket, new_key, digest, len(jpeg))
                if not reused:
                    client.upload_fileobj(
                        __import__("io").BytesIO(jpeg),
                        bucket,
                        new_key,
                        ExtraArgs={
                            "ContentType": "image/jpeg",
                            "CacheControl": "no-cache",
                            "Metadata": {"sha256": digest},
                        },
                    )
                uploaded = r2_head(client, bucket, new_key)
                if uploaded is None or int(uploaded.get("ContentLength", -1)) != len(jpeg):
                    raise RuntimeError("JPEG R2 object failed size verification")
                meta = {str(k).lower(): str(v).lower() for k, v in uploaded.get("Metadata", {}).items()}
                if meta.get("sha256") and meta["sha256"] != digest:
                    raise RuntimeError("JPEG R2 object failed SHA-256 verification")
                new_url = public_base + "/" + quote(new_key, safe="/")
                verify_public_jpeg(http, new_url)
                destinations = []
                for ref in item["references"]:
                    destination = local_destination_for_ref(ref, old_key, new_key, prefix)
                    if destination and destination not in destinations:
                        destinations.append(destination)
                completed[old_key] = {
                    "new_key": new_key,
                    "new_url": new_url,
                    "sha256": digest,
                    "bytes": len(jpeg),
                    "width": details["width"],
                    "height": details["height"],
                    "image": details,
                    "local_destinations": destinations,
                }
                atomic_json(state_path, conversion_state)
                emit(
                    phase="jpeg-upload-verified",
                    current=index,
                    total=len(plan["objects"]),
                    jpeg_bytes=len(jpeg),
                    upload="reused" if reused else "uploaded",
                    public_get="verified",
                )
        if not already_switched:
            if set(completed) != {item["key"] for item in plan["objects"]}:
                raise RuntimeError("Not every referenced PNG has a verified JPEG replacement")
            updates = update_database(plan, completed, prefix)
            atomic_json(completion_marker, {"ok": True, "converted": len(completed)})
            emit(phase="sqlite-updated", rows_by_table=updates)
    finally:
        http.close()
    if not references_are_migrated(plan, completed):
        raise RuntimeError("Old PNG URL references remain; refusing to delete old R2 objects")
    # Recheck each new object immediately before deleting any old object.
    for item in plan["objects"]:
        new = completed[item["key"]]
        head = r2_head(client, bucket, new["new_key"])
        if head is None or int(head.get("ContentLength", -1)) != new["bytes"]:
            raise RuntimeError("A migrated JPEG disappeared before old-object cleanup")
    old_keys = [item["key"] for item in plan["objects"]]
    removed = 0
    for offset in range(0, len(old_keys), 1000):
        batch = old_keys[offset : offset + 1000]
        result = client.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True}
        )
        if result.get("Errors"):
            raise RuntimeError("R2 returned errors while deleting superseded PNG keys")
        removed += len(batch)
        atomic_json(WORK_ROOT / "delete-progress.json", {"deleted": removed, "total": len(old_keys)})
    for old_key in old_keys:
        if r2_head(client, bucket, old_key) is not None:
            raise RuntimeError("An old PNG object remains after deletion")
    result = {
        "ok": True,
        "converted_referenced_png_objects": len(plan["objects"]),
        "old_referenced_png_objects_deleted": removed,
        "unreferenced_png_objects_left_untouched": len(plan["orphan_pngs"]),
        "sqlite_backup": (WORK_ROOT / "database-before.sqlite3").name,
        "local_png_files_retained": True,
        "database_integrity": "ok",
    }
    result["original_png_rollback_copies"] = len(list((WORK_ROOT / "originals").glob("*.png")))
    atomic_json(WORK_ROOT / "migration-complete.json", result)
    json_line({"phase": "complete", **result})


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert DB-referenced R2 PNG images to JPG.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Read-only DB/R2 inventory; no object or DB writes.")
    group.add_argument(
        "--apply", action="store_true", help="Upload/verify JPEGs, rewrite DB, then delete replaced PNGs."
    )
    parser.add_argument(
        "--delete-old-png",
        action="store_true",
        help="Required with --apply; removes only converted DB-referenced keys.",
    )
    args = parser.parse_args()
    if args.apply and not args.delete_old_png:
        parser.error("--apply requires --delete-old-png")
    if args.dry_run and args.delete_old_png:
        parser.error("--delete-old-png is invalid in dry-run mode")
    migrate(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - reveal only controlled errors, never SDK/request internals.
        message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else ""
        json_line({"migration_failed": type(exc).__name__, "reason": message[:240]})
        raise SystemExit(1)
