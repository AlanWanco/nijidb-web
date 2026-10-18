"""Pure parsing and comparison helpers for the official Nijigasaki music archive."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.news_fetch import is_allowed_official_image_url

SOURCE_URL = "https://www.lovelive-anime.jp/nijigasaki/cd.php"
RELEASE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def clean_text(node: Any) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def parse_plain_tracks(spec: Any) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    blocks = spec.select("p.list")
    if not blocks and getattr(spec, "name", "") == "dd":
        blocks = [spec]
    for block in blocks:
        current: dict[str, Any] | None = None
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


def parse_extras(content: Any, source_url: str = SOURCE_URL) -> list[dict[str, Any]]:
    extras: list[dict[str, Any]] = []
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
        url = urljoin(source_url, link["href"])
        label = clean_text(link)
        if not label or url.startswith("#") or any(extra.get("url") == url for extra in extras):
            continue
        extras.append({"type": "link", "title": label, "url": url})
    return extras


def parse_release(
    release_id: str,
    content: Any,
    entry_image: Any = None,
    *,
    source_url: str = SOURCE_URL,
) -> dict[str, str]:
    release_id = str(release_id or "").strip()
    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise ValueError("官网音乐发行 ID 格式无效")
    title = content.select_one(".title") if content else None
    subtitle = title.find("span") if title else None
    entry_title = clean_text(title) or (entry_image.get("alt", "").strip() if entry_image else "") or release_id
    specs = content.select(".spec") if content else []
    fields: dict[str, str] = {}
    tracks: list[dict[str, Any]] = []
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
        detail_image["src"] = urljoin(source_url, detail_image["src"])
        detail_image["loading"] = "lazy"
    detail_html = str(detail_copy) if content else ""
    extras = parse_extras(content, source_url) if content else []
    cover_url = urljoin(source_url, image.get("src", "")) if image else ""
    record = {
        "id": release_id,
        "title": entry_title,
        "subtitle": clean_text(subtitle),
        "artist": fields.get("アーティスト", ""),
        "release_date": fields.get("発売日") or fields.get("一般発売日") or fields.get("劇場先行発売日", ""),
        "price": fields.get("価格", ""),
        "cover_url": cover_url if is_allowed_official_image_url(cover_url) else "",
        "cover_source_url": cover_url if is_allowed_official_image_url(cover_url) else "",
        "detail_html": detail_html,
        "source_url": f"{source_url}#{release_id}",
        "tracks_json": json.dumps(tracks, ensure_ascii=False),
        "spec_json": json.dumps(fields, ensure_ascii=False),
        "extras_json": json.dumps(extras, ensure_ascii=False),
    }
    record["fingerprint"] = release_fingerprint(record)
    return record


def music_content_signature(record: dict[str, Any]) -> str:
    """Compare normalized source content, ignoring only local/R2 cover rewrites."""
    detail = BeautifulSoup(str(record.get("detail_html") or ""), "html.parser")
    sources = []
    for image in detail.select("img"):
        source = str(image.get("data-music-source-url") or image.get("src") or "").strip()
        if source:
            sources.append(urljoin(SOURCE_URL, source))
        image.attrs.pop("src", None)
        image.attrs.pop("srcset", None)
        image.attrs.pop("loading", None)
        image.attrs.pop("data-music-source-url", None)

    def stable_json(json_key: str, api_key: str, default: str) -> str:
        value = record.get(json_key)
        if value is None:
            value = record.get(api_key, default)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    payload = {
        "id": str(record.get("id") or ""),
        "title": str(record.get("title") or ""),
        "subtitle": str(record.get("subtitle") or ""),
        "artist": str(record.get("artist") or ""),
        "release_date": str(record.get("release_date") or ""),
        "price": str(record.get("price") or ""),
        "cover_source_url": str(record.get("cover_source_url") or record.get("cover_url") or ""),
        "detail_html": str(detail),
        "detail_image_sources": sources,
        "tracks_json": stable_json("tracks_json", "tracks", "[]"),
        "spec_json": stable_json("spec_json", "specs", "{}"),
        "extras_json": stable_json("extras_json", "extras", "[]"),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def is_r2_music_url(value: str) -> bool:
    return "/images/" in value and "/music/" in value


def release_fingerprint(record: dict[str, Any]) -> str:
    return hashlib.sha256(
        (
            str(record.get("title") or "")
            + str(record.get("detail_html") or "")
            + str(record.get("cover_url") or "")
            + str(record.get("tracks_json") or "[]")
            + str(record.get("extras_json") or "[]")
        ).encode()
    ).hexdigest()


def parse_music_listing(html: str) -> tuple[list[tuple[str, Any, Any]], list[str]]:
    """Return validated (release id, detail box, listing image) entries."""
    soup = BeautifulSoup(html, "html.parser")
    source_list = soup.select_one("ul.list")
    if not source_list:
        raise ValueError("官网音乐目录无法解析")
    boxes = {box.get("id"): box for box in soup.select(".box[id]")}
    entries: list[tuple[str, Any, Any]] = []
    missing_details: list[str] = []
    seen: set[str] = set()
    for entry in source_list.find_all("li", recursive=False):
        link = entry.find("a", href=True)
        href = str(link.get("href") or "") if link else ""
        if not link or not href.startswith("#") or len(href) <= 1:
            continue
        release_id = href[1:].strip()
        if not RELEASE_ID_PATTERN.fullmatch(release_id) or release_id in seen:
            continue
        seen.add(release_id)
        box = boxes.get(release_id)
        entry_image = entry.find("img", src=True)
        entries.append((release_id, box, entry_image))
        if not box or not box.select_one(".title"):
            missing_details.append(release_id)
    if not entries:
        raise ValueError("官网音乐目录无法解析")
    return entries, list(dict.fromkeys(missing_details))
