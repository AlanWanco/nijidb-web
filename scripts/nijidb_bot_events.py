"""Durable, HMAC-authenticated handoff from the existing Poller to awesome-bot."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

MAX_PENDING_ITEMS = 10_000
MAX_PENDING_BYTES = 128 * 1024 * 1024
MAX_SITE_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_EVENT_BATCH = 20
MAX_EVENT_BATCH_BYTES = 1800 * 1024
MAX_BOT_RESPONSE_BYTES = 16 * 1024


def outbox_path(config: Any) -> Path:
    explicit = getattr(config, "bot_event_outbox_file", None)
    if explicit:
        return Path(explicit).expanduser()
    state = getattr(config, "state_file", None)
    if state is None:
        state = getattr(config, "news_state_file", None) or getattr(config, "music_state_file", None)
    if state is None:
        raise ValueError("Poller outbox requires a configured state path")
    return Path(state).expanduser().with_name("nijidb-bot-outbox.sqlite3")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS events (
            event_id TEXT NOT NULL,
            source TEXT NOT NULL,
            item_id TEXT NOT NULL,
            change_type TEXT NOT NULL,
            previous_id TEXT NOT NULL DEFAULT '',
            snapshot_json TEXT NOT NULL,
            site_path TEXT NOT NULL,
            site_payload_json TEXT NOT NULL,
            site_pending INTEGER NOT NULL DEFAULT 1,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(source, item_id)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_events_due ON events(site_pending,next_attempt_at,created_at)"
    )
    return connection


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_id(
    source: str,
    item_id: str,
    previous_id: str,
    change: str,
    snapshot: dict[str, Any],
) -> str:
    encoded = _json({
        "source": source,
        "item_id": item_id,
        "previous_id": previous_id,
        "change": change,
        "snapshot": snapshot,
    }).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def queue_site_event(
    config: Any,
    *,
    source: str,
    item_id: str,
    change: str,
    snapshot: dict[str, Any],
    previous_id: str = "",
    site_path: str,
    site_payload: dict[str, Any],
) -> str:
    """Persist the full site write before HTTP, then hand off only after its ACK."""
    if source not in {"news", "music"} or change not in {"created", "updated", "baseline"}:
        raise ValueError("Poller event source/change is invalid")
    if not item_id or len(item_id) > 128:
        raise ValueError("Poller event item ID is invalid")
    snapshot_json = _json(snapshot)
    payload_json = _json(site_payload)
    if len(payload_json.encode("utf-8")) > MAX_SITE_PAYLOAD_BYTES:
        raise RuntimeError("Poller 待提交站点内容超过大小限制")
    now = time.time()
    path = outbox_path(config)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT event_id,change_type,site_pending,previous_id,site_payload_json FROM events WHERE source=? AND item_id=?",
            (source, item_id),
        ).fetchone()
        original_previous_id = previous_id
        if existing and int(existing["site_pending"]):
            # Keep the first baseline while retrying a write that has no site ACK yet.
            previous_id = str(existing["previous_id"] or "")
        change_type = "created" if existing and existing["change_type"] == "created" else change
        if existing and existing["change_type"] in {"baseline", "created"} and change in {"baseline", "updated"}:
            change_type = str(existing["change_type"])
        event_id = _event_id(source, item_id, previous_id, change_type, snapshot)
        if existing and hmac.compare_digest(str(existing["event_id"]), event_id):
            if int(existing["site_pending"]):
                queued_payload = json.loads(str(existing["site_payload_json"]))
                incoming_payload = json.loads(payload_json)
                if source == "music":
                    incoming_payload["baseline"] = bool(queued_payload.get("baseline") or queued_payload.get("created"))
                elif queued_payload.get("baseline") or queued_payload.get("created"):
                    incoming_payload["baseline"] = True
                if original_previous_id != previous_id:
                    connection.execute(
                        "UPDATE events SET site_payload_json=?,updated_at=? WHERE source=? AND item_id=?",
                        (_json(incoming_payload), time.time(), source, item_id),
                    )
                connection.commit()
            else:
                connection.commit()
            return event_id
        queued_payload_json = payload_json
        if existing and int(existing["site_pending"]):
            try:
                queued_payload = json.loads(str(existing["site_payload_json"]))
                incoming_payload = json.loads(payload_json)
                if source == "music":
                    incoming_payload["baseline"] = bool(queued_payload.get("baseline") or queued_payload.get("created"))
                elif queued_payload.get("baseline") or queued_payload.get("created"):
                    incoming_payload["baseline"] = True
                queued_payload_json = _json(incoming_payload)
            except (ValueError, TypeError, json.JSONDecodeError):
                queued_payload_json = payload_json
        connection.execute(
            """INSERT INTO events(
                event_id,source,item_id,change_type,previous_id,snapshot_json,site_path,
                site_payload_json,site_pending,attempts,next_attempt_at,last_error,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,1,0,0,'',?,?)
            ON CONFLICT(source,item_id) DO UPDATE SET
                event_id=excluded.event_id,
                change_type=CASE WHEN events.change_type='created' THEN 'created' ELSE excluded.change_type END,
                previous_id=excluded.previous_id,
                snapshot_json=excluded.snapshot_json,
                site_path=excluded.site_path,
                site_payload_json=excluded.site_payload_json,
                site_pending=1,
                attempts=0,
                next_attempt_at=0,
                last_error='',
                updated_at=excluded.updated_at""",
            (event_id, source, item_id, change_type, previous_id, snapshot_json, site_path,
             queued_payload_json, now, now),
        )
        count, size = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(length(snapshot_json)+length(site_payload_json)),0) FROM events"
        ).fetchone()
        if int(count) > MAX_PENDING_ITEMS or int(size) > MAX_PENDING_BYTES:
            connection.rollback()
            raise RuntimeError("Poller outbox 已满，停止覆盖未完成事件")
        connection.commit()
    finally:
        connection.close()
    return event_id


def mark_site_event_imported(config: Any, event_id: str, *, changed: bool = True) -> bool:
    """Release an event to Bot delivery after a successful site response."""
    path = outbox_path(config)
    connection = _connect(path)
    try:
        if not changed:
            row = connection.execute(
                "SELECT change_type FROM events WHERE event_id=? AND site_pending=1", (event_id,)
            ).fetchone()
            if row and row["change_type"] in {"baseline", "created"}:
                connection.execute(
                    "UPDATE events SET site_pending=0,attempts=0,next_attempt_at=0,last_error='',updated_at=? WHERE event_id=?",
                    (time.time(), event_id),
                )
            else:
                connection.execute("DELETE FROM events WHERE event_id=? AND site_pending=1", (event_id,))
            connection.commit()
            return True
        cursor = connection.execute(
            "UPDATE events SET site_pending=0,attempts=0,next_attempt_at=0,last_error='',updated_at=? "
            "WHERE event_id=?",
            (time.time(), event_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def _retry(connection: sqlite3.Connection, event_ids: list[str], error: str) -> None:
    now = time.time()
    for event_id in event_ids:
        row = connection.execute("SELECT attempts FROM events WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            continue
        attempts = int(row["attempts"] or 0) + 1
        delay = min(60 * (2 ** min(attempts - 1, 10)), 3600)
        connection.execute(
            "UPDATE events SET attempts=?,next_attempt_at=?,last_error=?,updated_at=? WHERE event_id=?",
            (attempts, now + delay, error[:200], now, event_id),
        )


async def _read_limited(response: httpx.Response, maximum: int) -> bytes:
    content_length = response.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > maximum:
        raise RuntimeError("Bot 响应超过大小限制")
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=4096):
        if len(body) + len(chunk) > maximum:
            raise RuntimeError("Bot 响应超过大小限制")
        body.extend(chunk)
    return bytes(body)


def _signed_headers(secret: str, raw: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + raw, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Nijidb-Timestamp": timestamp,
        "X-Nijidb-Signature": signature,
    }


def _batch_id(items: list[dict[str, Any]]) -> str:
    return hashlib.sha256("\n".join(sorted(item["event_id"] for item in items)).encode("ascii")).hexdigest()


async def flush_site_event_outbox(config: Any, client: httpx.AsyncClient) -> dict[str, int]:
    """Replay site imports that were durably queued before a previous request failed."""
    path = outbox_path(config)
    connection = _connect(path)
    try:
        rows = connection.execute(
            "SELECT * FROM events WHERE site_pending=1 AND next_attempt_at<=? ORDER BY created_at,event_id LIMIT ?",
            (time.time(), MAX_EVENT_BATCH),
        ).fetchall()
    finally:
        connection.close()
    sent = failed = 0
    for row in rows:
        source = str(row["source"])
        key = getattr(config, "music_api_key", "") if source == "music" else getattr(config, "api_key", "")
        headers = {
            "X-Nijidb-API-Key": key,
            "X-Nijidb-Poller-Event-ID": str(row["event_id"]),
            "X-Nijidb-Poller": "1",
        }
        payload = json.loads(str(row["site_payload_json"]))
        if source == "news":
            headers["X-Nijidb-News-Notify"] = "1"
        try:
            async with client.stream(
                "POST",
                f"{config.base_url}{row['site_path']}",
                json=payload,
                headers=headers,
            ) as response:
                body = await _read_limited(response, MAX_BOT_RESPONSE_BYTES)
                if response.status_code not in {200, 202}:
                    raise RuntimeError(f"site_http_{response.status_code}")
                try:
                    result = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                    raise RuntimeError("invalid_site_ack") from exc
                if not isinstance(result, dict) or type(result.get("changed")) is not bool:
                    raise RuntimeError("invalid_site_ack")
            mark_site_event_imported(config, str(row["event_id"]), changed=result["changed"])
            if result["changed"]:
                sent += 1
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            connection = _connect(path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                _retry(connection, [str(row["event_id"])], type(exc).__name__)
                connection.commit()
            finally:
                connection.close()
            failed += 1
    with _connect(path) as connection:
        pending = int(connection.execute("SELECT COUNT(*) FROM events WHERE site_pending=1").fetchone()[0])
    return {"sent": sent, "failed": failed, "pending": pending}


async def flush_bot_event_outbox(config: Any, client: httpx.AsyncClient) -> dict[str, int]:
    if not config.bot_event_url or not config.bot_event_secret:
        return {"sent": 0, "pending": 0}
    path = outbox_path(config)
    connection = _connect(path)
    try:
        rows = connection.execute(
            "SELECT * FROM events WHERE site_pending=0 AND next_attempt_at<=? ORDER BY created_at,event_id LIMIT ?",
            (time.time(), MAX_PENDING_ITEMS),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        with _connect(path) as connection:
            pending = int(connection.execute("SELECT COUNT(*) FROM events WHERE site_pending=0").fetchone()[0])
        return {"sent": 0, "pending": pending}
    source = str(rows[0]["source"])
    selected_rows = []
    estimated_bytes = 64
    for row in rows:
        if row["source"] != source:
            continue
        row_size = len(str(row["snapshot_json"]).encode("utf-8")) + 256
        if len(selected_rows) >= MAX_EVENT_BATCH or estimated_bytes + row_size > MAX_EVENT_BATCH_BYTES:
            break
        if row_size + 64 > MAX_EVENT_BATCH_BYTES:
            continue
        selected_rows.append(row)
        estimated_bytes += row_size
    rows = selected_rows
    if not rows:
        with _connect(path) as connection:
            pending = int(connection.execute("SELECT COUNT(*) FROM events WHERE site_pending=0").fetchone()[0])
        return {"sent": 0, "pending": pending}
    event_ids = [str(row["event_id"]) for row in rows]
    items = []
    for row in rows:
        try:
            saved_snapshot = json.loads(str(row["snapshot_json"]))
        except (ValueError, json.JSONDecodeError, RecursionError):
            saved_snapshot = {}
        items.append({
            "event_id": str(row["event_id"]),
            "source": str(row["source"]),
            "item_id": str(row["item_id"]),
            "change": str(row["change_type"]),
            "previous_id": str(row["previous_id"] or ""),
            "snapshot": saved_snapshot,
        })
    batch_id = _batch_id(items)
    raw = _json({"event_id": batch_id, "items": items}).encode("utf-8")
    error = ""
    try:
        async with client.stream(
            "POST", config.bot_event_url, content=raw,
            headers=_signed_headers(config.bot_event_secret, raw),
        ) as response:
            body = await _read_limited(response, MAX_BOT_RESPONSE_BYTES)
            if response.status_code not in {200, 202}:
                error = f"http_{response.status_code}"
            else:
                try:
                    result = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                    result = None
                if not (
                    isinstance(result, dict)
                    and result.get("accepted") is True
                    and result.get("event_id") == batch_id
                    and result.get("accepted_items") == len(items)
                ):
                    error = "invalid_ack"
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        error = type(exc).__name__

    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if error:
            _retry(connection, event_ids, error)
        else:
            connection.executemany(
                "DELETE FROM events WHERE event_id=? AND site_pending=0",
                [(event_id,) for event_id in event_ids],
            )
        connection.commit()
    finally:
        connection.close()
    connection = _connect(path)
    try:
        pending = int(connection.execute("SELECT COUNT(*) FROM events WHERE site_pending=0").fetchone()[0])
    finally:
        connection.close()
    return {"sent": 0 if error else len(items), "failed": int(bool(error)), "pending": pending}


def poller_status_url(event_url: str) -> str:
    parts = urlsplit(event_url)
    return urlunsplit((parts.scheme, parts.netloc, "/internal/nijidb/poller-status", "", ""))


async def send_poller_heartbeat(
    config: Any,
    client: httpx.AsyncClient,
    status: dict[str, Any],
) -> bool:
    if not config.bot_event_url or not config.bot_event_secret:
        return False
    raw = _json(status).encode("utf-8")
    try:
        async with client.stream(
            "POST",
            poller_status_url(config.bot_event_url),
            content=raw,
            headers=_signed_headers(config.bot_event_secret, raw),
        ) as response:
            body = await _read_limited(response, MAX_BOT_RESPONSE_BYTES)
            if response.status_code not in {200, 202}:
                return False
            result = json.loads(body)
            return isinstance(result, dict) and result.get("accepted") is True
    except (httpx.HTTPError, RuntimeError, ValueError, json.JSONDecodeError, RecursionError):
        return False
