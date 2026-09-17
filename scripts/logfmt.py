"""统一的日志时间格式：带日期，默认 UTC+8。

容器默认是 UTC 时区，直接 `datetime.now()` 打出来的时间会差 8 小时；这里显式使用固定偏移，
不依赖容器时区。偏移可用 `NIJIDB_LOG_TZ_OFFSET_HOURS` 覆盖（例如 `0` 表示 UTC）。

用法：

    from logfmt import format_message

    print(format_message("启动完成"), flush=True)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

DEFAULT_OFFSET_HOURS = 8.0
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _offset_hours() -> float:
    raw = os.getenv("NIJIDB_LOG_TZ_OFFSET_HOURS", "").strip()
    if not raw:
        return DEFAULT_OFFSET_HOURS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_OFFSET_HOURS


OFFSET_HOURS = _offset_hours()
LOG_TZ = timezone(timedelta(hours=OFFSET_HOURS))


def timestamp() -> str:
    """当前时间（带日期，默认 UTC+8）。"""
    return datetime.now(LOG_TZ).strftime(TIME_FORMAT)


def format_message(message: str) -> str:
    return f"[{timestamp()}] {message}"
