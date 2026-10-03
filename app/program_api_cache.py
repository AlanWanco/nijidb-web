"""Bounded public-program response cache; no persisted or database-derived state is mutated."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.encoders import jsonable_encoder


@dataclass(frozen=True)
class ProgramApiResult:
    body: bytes | None
    etag: str | None
    status: int = 200


def database_revision(path: Path, context: tuple[Any, ...], minute: int | None = None) -> str | None:
    """Observe any DB replacement/commit, including SQLite WAL and external writers.

    Minute granularity also invalidates aired/update status without a DB edit.
    This deliberately over-invalidates on unrelated writes rather than relying
    on every mutation endpoint to remember to invalidate a cache.
    """
    files = []
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-journal")):
        try:
            info = candidate.stat()
        except FileNotFoundError:
            if candidate == path:
                return None
            files.append(None)
        else:
            files.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    stamp = (str(path.resolve()), files, int(time.time() // 60) if minute is None else minute, context)
    return hashlib.sha256(repr(stamp).encode()).hexdigest()


def matches_etag(value: str, etag: str) -> bool:
    expected = etag.removeprefix("W/")
    return any(token.strip() == "*" or token.strip().removeprefix("W/") == expected for token in value.split(","))


class PublicProgramCache:
    def __init__(self, max_entries: int = 8, ttl: float = 60, max_body_bytes: int = 2 * 1024 * 1024):
        self.max_entries = max_entries
        self.ttl = ttl
        self.max_body_bytes = max_body_bytes
        self.entries: OrderedDict[str, tuple[str, float, bytes]] = OrderedDict()
        self.lock = threading.Lock()
        self.build_locks = [threading.Lock() for _ in range(8)]

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()

    def get(self, key: str, etag: str) -> bytes | None:
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            tag, created, body = entry
            if tag != etag or time.monotonic() - created >= self.ttl:
                self.entries.pop(key)
                return None
            self.entries.move_to_end(key)
            return body

    def response(
        self,
        key: str,
        revision: Callable[[], str | None],
        build: Callable[[], dict[str, Any]],
        if_none_match: str = "",
    ) -> ProgramApiResult:
        stamp = revision()
        etag = f'W/"{hashlib.sha256((key + (stamp or "")).encode()).hexdigest()}"' if stamp else None
        # Check the revision before fetching or constructing occurrence records.
        if etag and matches_etag(if_none_match, etag):
            return ProgramApiResult(None, etag, 304)
        body = self.get(key, etag) if etag else None
        if body is not None:
            return ProgramApiResult(body, etag)
        # Serialize duplicate builds without holding the cache lookup lock. A
        # cached list response must not wait behind an unrelated cold calendar.
        with self.build_locks[hash(key) % len(self.build_locks)]:
            body = self.get(key, etag) if etag else None
            if body is not None:
                return ProgramApiResult(body, etag)
            body = json.dumps(
                jsonable_encoder(build()), ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
            if etag and revision() == stamp:
                if len(body) > self.max_body_bytes:
                    return ProgramApiResult(body, etag)
                with self.lock:
                    self.entries[key] = (etag, time.monotonic(), body)
                    self.entries.move_to_end(key)
                    while len(self.entries) > self.max_entries:
                        self.entries.popitem(last=False)
                return ProgramApiResult(body, etag)
            # Do not label a response with a revision if a write occurred while
            # reading/building it. The next request must fetch a fresh snapshot.
            return ProgramApiResult(body, None)
