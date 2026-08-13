"""按单调时钟探测 DevOps API 健康端点，并将结果追加为 JSONL。

本脚本只负责连通性观测，不修改 API 服务；使用 stop 文件可安全提前结束监测。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from urllib.error import URLError
from urllib.request import Request, urlopen


def _record(writer, payload: dict[str, object]) -> None:
    """写入带 UTC 时间戳的单行 JSON，并立即 flush 保证进程中断后可恢复。"""
    payload = {"timestamp": datetime.now(timezone.utc).isoformat(), **payload}
    writer.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    writer.flush()


def _probe(uri: str, timeout: float) -> tuple[bool, int | None, float, str]:
    """请求健康端点并校验 HTTP 状态及 JSON status 字段，返回观测结果。"""
    started = time.monotonic()
    status_code: int | None = None
    try:
        request = Request(uri, headers={"Accept": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            status_code = response.status
            body = response.read(1024 * 1024)
        payload = json.loads(body.decode("utf-8"))
        ok = 200 <= status_code < 300 and isinstance(payload, dict) and payload.get("status") == "ok"
        detail = "healthy" if ok else "unexpected_health_response"
    except (OSError, URLError, TimeoutError, ValueError, UnicodeError) as exc:
        ok = False
        detail = f"{type(exc).__name__}: {exc}"[:500]
    return ok, status_code, round((time.monotonic() - started) * 1000, 2), detail


def monitor(
    *,
    uri: str,
    interval: float,
    deadline: float,
    log_path: Path,
    stop_path: Path,
    pid_path: Path,
    timeout: float = 10.0,
) -> None:
    """按固定的单调时钟节拍运行监测，并记录成功率与连续失败次数。"""
    if interval <= 0 or timeout <= 0:
        raise ValueError("interval and timeout must be positive")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    stop_event = Event()
    total = successes = failures = consecutive_failures = max_consecutive_failures = 0
    reason = "deadline"
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        with log_path.open("a", encoding="utf-8", buffering=1) as writer:
            _record(writer, {"event": "started", "uri": uri, "interval_seconds": interval, "deadline_epoch": deadline, "pid": os.getpid()})
            next_probe_at = time.monotonic()
            while time.time() < deadline:
                if stop_path.exists():
                    reason = "manual_stop"
                    break
                wait_seconds = max(0.0, next_probe_at - time.monotonic())
                if wait_seconds and stop_event.wait(wait_seconds):
                    reason = "manual_stop"
                    break
                if stop_path.exists():
                    reason = "manual_stop"
                    break
                total += 1
                ok, status_code, duration_ms, detail = _probe(uri, timeout)
                if ok:
                    successes += 1
                    consecutive_failures = 0
                else:
                    failures += 1
                    consecutive_failures += 1
                    max_consecutive_failures = max(max_consecutive_failures, consecutive_failures)
                _record(writer, {"event": "probe", "probe": total, "result": "ok" if ok else "fail", "status_code": status_code, "duration_ms": duration_ms, "successes": successes, "failures": failures, "availability_percent": round(successes / total * 100, 2), "consecutive_failures": consecutive_failures, "max_consecutive_failures": max_consecutive_failures, "detail": detail})
                # 以绝对节拍推进，避免请求耗时导致长期漂移，也避免短间隔忙循环。
                next_probe_at += interval
                if next_probe_at < time.monotonic():
                    next_probe_at = time.monotonic() + interval
            _record(writer, {"event": "stopped" if reason == "manual_stop" else "finished", "reason": reason, "probes": total, "successes": successes, "failures": failures, "availability_percent": round(successes / total * 100, 2) if total else 0, "max_consecutive_failures": max_consecutive_failures})
    finally:
        pid_path.unlink(missing_ok=True)


def _parse_args() -> argparse.Namespace:
    """解析命令行参数，保持正式监测和短间隔测试使用同一实现。"""
    parser = argparse.ArgumentParser(description="DevOps API connectivity monitor")
    parser.add_argument("--uri", required=True)
    parser.add_argument("--interval", type=float, required=True)
    parser.add_argument("--deadline", type=float, required=True, help="Unix timestamp")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--stop", type=Path, required=True)
    parser.add_argument("--pid", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser.parse_args()


def main() -> None:
    """启动命令行监测入口。"""
    args = _parse_args()
    monitor(uri=args.uri, interval=args.interval, deadline=args.deadline, log_path=args.log, stop_path=args.stop, pid_path=args.pid, timeout=args.timeout)


if __name__ == "__main__":
    main()
