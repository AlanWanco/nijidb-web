#!/usr/bin/env python3
"""容器内的任务托管器：一个容器里跑「常驻任务 + 定时任务」。

按「容器」计数时，把多个小循环拆成一堆容器很不直观；这里用一个容器托管全部：

* ``mode: "loop"`` —— 常驻任务（例如新闻轮询），退出后立即按最小间隔重启；
* ``mode: "once"`` —— 定时任务（例如节点健康检查、订阅更新），
  启动后先跑一次，之后按 ``interval_minutes`` 定时拉起，跑完即退出，不占常驻内存。

任务定义来自 ``MAINTAINER_CONFIG`` 指向的 JSON：

```json
{
  "tasks": [
    {"name": "news-poller", "script": "local_official_news_poller.py", "mode": "loop"},
    {"name": "node-health", "script": "proxy_node_health.py", "mode": "once",
     "interval_minutes": 30, "args": ["--once"]}
  ]
}
```

``script`` 相对本文件所在目录解析；``env`` 覆盖容器环境变量；``args`` 追加命令行参数。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from logfmt import format_message  # noqa: E402

RESTART_MIN_INTERVAL = 15.0
SUPERVISE_INTERVAL = 5.0
ONCE_MIN_INTERVAL_SECONDS = 60.0
# 定时任务失败后先按这个间隔重试，避免要等满一个周期
FAILURE_RETRY_SECONDS = 600.0


def log(message: str) -> None:
    print(format_message(f"[maintainer] {message}"), flush=True)


class Task:
    """一个受托管的任务：常驻循环，或按间隔定时执行一次。"""

    def __init__(self, spec: dict[str, Any], script_dir: Path):
        name = str(spec.get("name") or "").strip()
        script = str(spec.get("script") or "").strip()
        if not name or not script:
            raise ValueError("任务必须包含 name 与 script")
        self.name = name
        self.script = (script_dir / script).resolve()
        if not self.script.exists():
            raise ValueError(f"任务脚本不存在：{self.script}")
        self.mode = str(spec.get("mode") or "loop").strip()
        if self.mode not in {"loop", "once"}:
            raise ValueError(f"任务 {name} 的 mode 只能是 loop 或 once")
        env = spec.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError(f"任务 {name} 的 env 必须是对象")
        self.env = {str(key): str(value) for key, value in env.items()}
        args = spec.get("args") or []
        if not isinstance(args, list):
            raise ValueError(f"任务 {name} 的 args 必须是数组")
        self.args = [str(item) for item in args]
        if self.mode == "once" and "--once" not in self.args:
            self.args.append("--once")
        interval_minutes = float(spec.get("interval_minutes") or 0)
        self.interval_seconds = max(ONCE_MIN_INTERVAL_SECONDS, interval_minutes * 60)
        self.process: subprocess.Popen[bytes] | None = None
        self.last_start = 0.0
        self.restarts = 0
        self.runs = 0
        self.next_run_at = 0.0

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def spawn(self) -> None:
        environment = dict(os.environ)
        environment.update(self.env)
        self.process = subprocess.Popen(
            [sys.executable, str(self.script), *self.args],
            env=environment,
            cwd=str(self.script.parent.parent),
        )
        self.last_start = time.monotonic()
        self.runs += 1

    def supervise(self) -> None:
        """loop 任务：退出就重启；once 任务：到点跑一次，跑完等下一轮。"""
        if self.mode == "once":
            if self.running():
                return
            if self.process is not None:
                code = self.process.returncode
                self.process = None
                if code == 0:
                    self.next_run_at = time.monotonic() + self.interval_seconds
                    log(f"定时任务 {self.name} 执行完成，{self.interval_seconds / 60:g} 分钟后再次运行")
                else:
                    self.next_run_at = time.monotonic() + FAILURE_RETRY_SECONDS
                    log(f"定时任务 {self.name} 失败（code={code}），{FAILURE_RETRY_SECONDS / 60:g} 分钟后重试")
                return
            if time.monotonic() >= self.next_run_at:
                log(f"启动定时任务 {self.name}")
                self.spawn()
            return

        if self.running():
            return
        if self.process is not None:
            elapsed = time.monotonic() - self.last_start
            if elapsed < RESTART_MIN_INTERVAL:
                # 启动即退出（例如配置缺失）时不要疯狂重启
                time.sleep(RESTART_MIN_INTERVAL - elapsed)
            self.restarts += 1
            log(f"常驻任务 {self.name} 已退出（code={self.process.returncode}），第 {self.restarts} 次重启")
        self.spawn()

    def stop(self) -> None:
        if not self.running() or self.process is None:
            return
        log(f"正在停止任务 {self.name}（pid={self.process.pid}）")
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def load_tasks(path: Path, script_dir: Path) -> list[Task]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取任务定义：{path}") from exc
    specs = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(specs, list) or not specs:
        raise SystemExit(f"任务定义里没有 tasks 列表：{path}")
    tasks: list[Task] = []
    for spec in specs:
        if not isinstance(spec, dict):
            raise SystemExit(f"任务定义格式无效：{spec!r}")
        try:
            tasks.append(Task(spec, script_dir))
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    return tasks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在一个容器里托管常驻与定时任务")
    parser.parse_args(argv)
    config_path = Path(os.getenv("MAINTAINER_CONFIG", "/app/maintainer.json"))
    script_dir = Path(__file__).resolve().parent
    tasks = load_tasks(config_path, script_dir)
    summary = "，".join(
        f"{task.name}({'常驻' if task.mode == 'loop' else f'每 {task.interval_seconds / 60:g} 分钟'})"
        for task in tasks
    )
    log(f"托管 {len(tasks)} 个任务：{summary}")

    stopping = {"value": False}

    def request_stop(signum, frame):  # type: ignore[no-untyped-def]
        if not stopping["value"]:
            stopping["value"] = True
            log(f"收到停止信号 {signum}")

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        while not stopping["value"]:
            for task in tasks:
                if not stopping["value"]:
                    task.supervise()
            deadline = time.monotonic() + SUPERVISE_INTERVAL
            while not stopping["value"] and time.monotonic() < deadline:
                time.sleep(0.5)
    finally:
        for task in tasks:
            task.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
