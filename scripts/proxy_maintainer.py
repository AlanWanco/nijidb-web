#!/usr/bin/env python3
"""容器内的定时任务托管器：把多个「自己会循环」的脚本放进同一个容器。

容器数量是按「容器」计数的，而每个小循环单独开一个容器很浪费；这里把
节点健康检查、各栈的订阅更新都收进一个容器，各自仍是独立进程（互不影响、
可以单独重启），并在退出时被自动拉起。

任务定义来自 ``MAINTAINER_CONFIG`` 指向的 JSON：

```json
{
  "tasks": [
    {"name": "news-health", "script": "proxy_node_health.py",
     "env": {"NODE_HEALTH_CONFIG": "/config/news/config.yaml"}}
  ]
}
```

``script`` 相对本文件所在目录解析；``env`` 会覆盖容器环境变量。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

RESTART_MIN_INTERVAL = 15.0
SUPERVISE_INTERVAL = 5.0


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [maintainer] {message}", flush=True)


class Task:
    """一个子进程任务：启动后长期运行，意外退出时按最小间隔重启。"""

    def __init__(self, spec: dict[str, Any], script_dir: Path):
        name = str(spec.get("name") or "").strip()
        script = str(spec.get("script") or "").strip()
        if not name or not script:
            raise ValueError("任务必须包含 name 与 script")
        self.name = name
        self.script = (script_dir / script).resolve()
        if not self.script.exists():
            raise ValueError(f"任务脚本不存在：{self.script}")
        env = spec.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError(f"任务 {name} 的 env 必须是对象")
        self.env = {str(key): str(value) for key, value in env.items()}
        self.process: subprocess.Popen[bytes] | None = None
        self.last_start = 0.0
        self.restarts = 0

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        environment = dict(os.environ)
        environment.update(self.env)
        self.process = subprocess.Popen(
            [sys.executable, str(self.script)],
            env=environment,
            cwd=str(self.script.parent.parent),
        )
        self.last_start = time.monotonic()
        log(f"已启动任务 {self.name}（pid={self.process.pid}）")

    def supervise(self) -> None:
        if self.running():
            return
        if self.process is not None:
            code = self.process.returncode
            elapsed = time.monotonic() - self.last_start
            if elapsed < RESTART_MIN_INTERVAL:
                # 启动即退出（例如配置缺失）时不要疯狂重启
                time.sleep(RESTART_MIN_INTERVAL - elapsed)
            self.restarts += 1
            log(f"任务 {self.name} 已退出（code={code}），第 {self.restarts} 次重启")
        self.start()

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
    parser = argparse.ArgumentParser(description="在一个容器里托管多个定时任务")
    parser.parse_args(argv)
    config_path = Path(os.getenv("MAINTAINER_CONFIG", "/app/maintainer.json"))
    script_dir = Path(__file__).resolve().parent
    tasks = load_tasks(config_path, script_dir)
    log(f"托管 {len(tasks)} 个任务：{', '.join(task.name for task in tasks)}")
    start_delay = float(os.getenv("MAINTAINER_START_DELAY_SECONDS", "15") or 15)
    if start_delay > 0:
        log(f"等待 {start_delay:g} 秒让依赖容器就绪")
        time.sleep(start_delay)

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
