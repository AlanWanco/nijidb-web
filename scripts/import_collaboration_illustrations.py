#!/usr/bin/env python3
"""Import the collaboration illustration index into the local frontend data file."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from bs4 import BeautifulSoup

try:
    from .collaboration_urls import page_identity
except ImportError:  # pragma: no cover - supports running this file directly
    from collaboration_urls import page_identity

SOURCE_URL = (
    "https://wiki.odaiba-objet.tokyo/d/"
    "%c6%fa%a5%f6%ba%e9%c9%c1%a4%ad%b2%bc%a4%ed%a4%b7%a5%a4%a5%e9%a5%b9%a5%c8%b0%ec%cd%f7"
)
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "frontend/src/content/collaborationIllustrations.json"
MAX_SOURCE_BYTES = 8 * 1024 * 1024


def fetch_source() -> bytes:
    request = Request(
        SOURCE_URL,
        headers={
            "Accept": "text/html",
            "User-Agent": "nijidb-collaboration-import/1.0",
        },
    )
    class NoRedirectHandler(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    with build_opener(ProxyHandler({}), NoRedirectHandler()).open(request, timeout=30) as response:
        try:
            announced_length = int(response.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError, OverflowError):
            announced_length = 0
        if announced_length > MAX_SOURCE_BYTES:
            raise ValueError("来源页面超过 8 MB 限制")
        content = response.read(MAX_SOURCE_BYTES + 1)
        if len(content) > MAX_SOURCE_BYTES:
            raise ValueError("来源页面超过 8 MB 限制")
        return content


def normalize_lines(cell) -> list[str]:
    for br in cell.find_all("br"):
        br.replace_with("\n")
    lines = []
    for line in cell.get_text().splitlines():
        normalized = re.sub(r"[ \t\xa0]+", " ", line).strip()
        if normalized:
            lines.append(normalized)
    return lines


def normalize_text(cell) -> str:
    return "\n".join(normalize_lines(cell))


def normalize_single_line(cell) -> str:
    return " ".join(normalize_lines(cell))


def dedupe_official_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop equivalent official page aliases while retaining the first source link."""
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in links:
        identity = page_identity(link["url"])
        if identity in seen:
            continue
        seen.add(identity)
        result.append(link)
    return result


def parse_updated_at(soup: BeautifulSoup) -> tuple[str, str]:
    update = soup.select_one("#page-header .update")
    if not update:
        return "", ""
    author_link = update.find("a", href=re.compile(r"/r/"))
    author = author_link.get_text(strip=True) if author_link else ""
    update_text = update.get_text(" ", strip=True)
    match = re.search(r"(\d{4})年(\d{2})月(\d{2})日.*?(\d{2}):(\d{2}):(\d{2})", update_text)
    if not match:
        return "", author
    year, month, day, hour, minute, second = match.groups()
    return f"{year}-{month}-{day}T{hour}:{minute}:{second}+09:00", author


def parse_page(raw: bytes) -> dict:
    soup = BeautifulSoup(raw.decode("euc_jis_2004"), "html.parser")
    title = soup.select_one("#page-header .title h2")
    page_title = title.get_text(" ", strip=True).replace("部分編集", "").strip() if title else ""
    updated_at, updated_by = parse_updated_at(soup)

    years = []
    for section in soup.select("#page-body .wiki-section-1"):
        heading = section.select_one(".title-1 h3")
        table = section.select_one("table")
        if not heading or not table:
            continue
        heading_text = heading.get_text(" ", strip=True)
        match = re.match(r"^(\d{4})年\s*\((\d+)件\)", heading_text)
        if not match:
            raise ValueError(f"无法解析年份标题：{heading_text}")
        year, declared_count = int(match.group(1)), int(match.group(2))

        items = []
        for row in table.select("tr")[1:]:
            cells = row.select("td")
            if len(cells) != 6:
                raise ValueError(f"{year} 年存在异常列数：{len(cells)}")
            links = dedupe_official_links([
                {
                    "title": " ".join(anchor.get_text(" ", strip=True).split()),
                    "url": urljoin(SOURCE_URL, anchor.get("href", "")),
                }
                for anchor in cells[4].select("a[href]")
            ])
            item = {
                "first_seen": normalize_single_line(cells[0]),
                "title": normalize_text(cells[1]),
                "collaboration": normalize_lines(cells[2]),
                "credit": normalize_single_line(cells[3]),
                "official_links": links,
                "note": normalize_text(cells[5]),
            }
            identity = "\n".join(
                [item["first_seen"], item["title"], "\n".join(item["collaboration"]), item["credit"]]
            )
            item["id"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["first_seen"]):
                raise ValueError(f"日期格式异常：{item['first_seen']}")
            if not item["official_links"]:
                raise ValueError(f"缺少官方链接：{item['title']}")
            items.append(item)

        if len(items) != declared_count:
            raise ValueError(f"{year} 年条目数量不一致：页面声明 {declared_count}，实际 {len(items)}")
        years.append({"year": year, "count": len(items), "items": items})

    if not years:
        raise ValueError("没有找到年度条目")

    intro = soup.select_one("#page-body .user-area")
    description = [
        "虹ヶ咲の描き下ろしイラストをまとめているページです。",
        "作品展開の振り返りやファンアート用資料探しなどにご活用ください。",
    ]
    scope = [
        (
            "作品、イベント、コラボに合わせて描かれた全身イラストが"
            "含まれるものを対象としています。"
        ),
        "※一部それ以外の情報も入っていますが、メインはあくまで上記です。",
    ]
    notes = [
        "まとめサイト等への転載はご遠慮ください。",
        "元々手作業でまとめており、ところどころ情報の粒度に差があります。",
        "また、不足してる描き下ろしの情報提供ありがとうございます。(*´꒳`人)",
        (
            "抜け・漏れや誤りにお気づきの際は、X(@Zacharylion)へお知らせください。"
            "（2026年分は随時追加します）"
        ),
    ]
    if intro and "まとめサイト等への転載はご遠慮ください。" not in intro.get_text(" ", strip=True):
        notes.insert(0, "原页面的转载说明发生了变化，请以原页面为准。")

    return {
        "schema_version": 1,
        "source": {
            "title": page_title,
            "url": SOURCE_URL,
            "site": "東京・お台場ライブラリ",
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_updated_at": updated_at,
            "last_updated_by": updated_by,
            "description": description,
            "scope": scope,
            "notes": notes,
        },
        "years": years,
        "total_count": sum(year["count"] for year in years),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    payload = parse_page(fetch_source())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"写入 {args.output}: {payload['total_count']} 条")


if __name__ == "__main__":
    main()
