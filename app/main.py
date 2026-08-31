from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

SOURCE_URL = "https://www.lovelive-anime.jp/nijigasaki/cd.php"
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("DATA_DIR", "/data")) / "nijidb.sqlite3"
MEDIA_DIR = DB_PATH.parent / "images"
FRONTEND_DIST = ROOT / "frontend" / "dist"
PASSWORD_ITERATIONS = 310_000
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
stop_event = asyncio.Event()
sync_lock = asyncio.Lock()


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


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
        """)
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


async def notify(changed: list[dict[str, str]], config: dict[str, str], category: str) -> None:
    if not changed or not config.get("onebot_url") or not config.get("onebot_target"):
        return
    lines = [f"[{category}]", f"虹咲音乐资料有 {len(changed)} 项更新："]
    lines.extend(f"• {item['title']}" for item in changed[:10])
    if len(changed) > 10:
        lines.append(f"以及其他 {len(changed) - 10} 项")
    await send_onebot("\n".join(lines), config)


async def cache_cover(item: dict[str, str], client: httpx.AsyncClient) -> None:
    if not item["cover_url"]:
        return
    extension = Path(urlparse(item["cover_url"]).path).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        extension = ".jpg"
    target = MEDIA_DIR / f"{item['id']}{extension}"
    if not target.exists():
        response = await client.get(item["cover_url"])
        response.raise_for_status()
        target.write_bytes(response.content)
    source_url = item["cover_url"]
    item["cover_url"] = f"/media/{target.name}"
    item["detail_html"] = item["detail_html"].replace(source_url, item["cover_url"])


async def store_records(records: list[dict[str, str]], assign_positions: bool = False) -> list[dict[str, str]]:
    changed: list[dict[str, str]] = []
    with db() as conn:
        existing_count = conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
        for position, item in enumerate(records):
            if assign_positions:
                item["position"] = position
            old = conn.execute("SELECT fingerprint FROM releases WHERE id = ?", (item["id"],)).fetchone()
            if old and old["fingerprint"] != item["fingerprint"]:
                changed.append(item)
            elif not old and existing_count > 0:
                changed.append(item)
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
            image_headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
                "Referer": SOURCE_URL,
            }
            image_errors = 0
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=image_headers) as image_client:
                for item in records:
                    try:
                        await cache_cover(item, image_client)
                    except Exception as exc:
                        image_errors += 1
                        print(f"[sync] cover failed {item['id']}: {exc}", flush=True)
            changed = await store_records(records, assign_positions=True)
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
            image_headers = {"User-Agent": headers["User-Agent"], "Referer": SOURCE_URL}
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=image_headers) as image_client:
                for item in records:
                    try:
                        await cache_cover(item, image_client)
                    except Exception as exc:
                        image_errors += 1
                        print(f"[detail-sync] cover failed {item['id']}: {exc}", flush=True)
            changed = await store_records(records)
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
async def api_get_settings(request: Request) -> dict[str, dict[str, str]]:
    require_api_admin(request)
    return {"settings": public_settings()}


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
    return {"changed_count": changed, "error": error}


@app.get("/rainbow.svg", include_in_schema=False)
async def rainbow_favicon():
    return FileResponse(FRONTEND_DIST / "rainbow.svg", media_type="image/svg+xml")


@app.get("/{path:path}")
async def frontend(path: str):
    index = FRONTEND_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(503, "Vue 前端尚未构建，请运行 npm install && npm run build")
    return FileResponse(index)
