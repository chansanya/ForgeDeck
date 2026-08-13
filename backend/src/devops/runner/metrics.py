"""通过受控 SSH 命令采集并解析 CPU、内存、磁盘和网络指标。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from devops.runner.ssh import SSHSession


@dataclass(frozen=True, slots=True)
class CPUCounters:
    total: int
    idle: int


@dataclass(frozen=True, slots=True)
class HostMetrics:
    server_id: str
    cpu_cores: int
    cpu_percent: float
    memory_total: int
    memory_used: int
    disk_total: int
    disk_used: int
    network_rx: int
    network_tx: int
    collected_at: datetime


class HostMetricsStore(Protocol):
    async def save(self, metrics: HostMetrics) -> None:
        """持久化一条主机指标样本，由具体数据库适配器实现。"""
        ...


class HostMetricsCollector:
    def __init__(self) -> None:
        """初始化按服务器隔离的 CPU 基线和并发保护锁。"""
        self._previous_cpu: dict[str, CPUCounters] = {}
        self._lock = asyncio.Lock()

    async def collect(self, server_id: str, session: SSHSession) -> HostMetrics:
        """并行采集主机资源文件，并用同一服务器的前后 CPU 计数计算利用率。"""
        cpu_result, cores_result, memory_result, disk_result, network_result = await asyncio.gather(
            session.run(("cat", "/proc/stat"), check=True),
            session.run(("getconf", "_NPROCESSORS_ONLN"), check=True),
            session.run(("cat", "/proc/meminfo"), check=True),
            session.run(("df", "-B1", "-P", "-x", "tmpfs", "-x", "devtmpfs"), check=True),
            session.run(("cat", "/proc/net/dev"), check=True),
        )
        current_cpu = parse_proc_stat(cpu_result.stdout)
        async with self._lock:
            previous_cpu = self._previous_cpu.get(server_id)
            self._previous_cpu[server_id] = current_cpu
        memory_total, memory_used = parse_proc_meminfo(memory_result.stdout)
        disk_total, disk_used = parse_df_bytes(disk_result.stdout)
        network_rx, network_tx = parse_proc_net_dev(network_result.stdout)
        try:
            cpu_cores = int(cores_result.stdout.strip())
        except ValueError as exc:
            raise ValueError("invalid CPU core count returned by getconf") from exc
        if cpu_cores <= 0:
            raise ValueError("CPU core count must be positive")
        return HostMetrics(
            server_id=server_id,
            cpu_cores=cpu_cores,
            cpu_percent=cpu_percent(current_cpu, previous_cpu),
            memory_total=memory_total,
            memory_used=memory_used,
            disk_total=disk_total,
            disk_used=disk_used,
            network_rx=network_rx,
            network_tx=network_tx,
            collected_at=datetime.now(UTC),
        )


def parse_proc_stat(value: bytes | str) -> CPUCounters:
    """解析 Linux /proc/stat 聚合 CPU 计数器，缺字段时拒绝不完整样本。"""
    text = value.decode(errors="replace") if isinstance(value, bytes) else value
    first_line = next((line for line in text.splitlines() if line.startswith("cpu ")), None)
    if first_line is None:
        raise ValueError("/proc/stat does not contain aggregate CPU counters")
    fields = first_line.split()[1:]
    try:
        counters = [int(field) for field in fields]
    except ValueError as exc:
        raise ValueError("invalid CPU counters in /proc/stat") from exc
    if len(counters) < 4:
        raise ValueError("incomplete CPU counters in /proc/stat")
    total = sum(counters)
    idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
    return CPUCounters(total=total, idle=idle)


def cpu_percent(current: CPUCounters, previous: CPUCounters | None) -> float:
    """根据两次累计计数计算并限制 0～100 的 CPU 使用率。"""
    if previous is None:
        total_delta = current.total
        idle_delta = current.idle
    else:
        total_delta = current.total - previous.total
        idle_delta = current.idle - previous.idle
    if total_delta <= 0:
        return 0.0
    busy = max(0, total_delta - max(0, idle_delta))
    return round(min(100.0, max(0.0, busy * 100.0 / total_delta)), 2)


def parse_proc_meminfo(value: bytes | str) -> tuple[int, int]:
    """解析 /proc/meminfo，并优先使用 MemAvailable 估算已用内存。"""
    text = value.decode(errors="replace") if isinstance(value, bytes) else value
    fields: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, raw = line.split(":", 1)
        parts = raw.split()
        if not parts:
            continue
        try:
            amount = int(parts[0])
        except ValueError:
            continue
        multiplier = 1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1
        fields[name] = amount * multiplier
    total = fields.get("MemTotal")
    if total is None or total <= 0:
        raise ValueError("/proc/meminfo does not contain a valid MemTotal")
    available = fields.get("MemAvailable")
    if available is None:
        available = sum(fields.get(name, 0) for name in ("MemFree", "Buffers", "Cached"))
    used = max(0, min(total, total - available))
    return total, used


def parse_df_bytes(value: bytes | str) -> tuple[int, int]:
    """解析 POSIX df 字节输出，按设备去重后汇总磁盘容量和使用量。"""
    text = value.decode(errors="replace") if isinstance(value, bytes) else value
    devices: dict[str, tuple[int, int]] = {}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 6:
            continue
        device = fields[0]
        try:
            total = int(fields[1])
            used = int(fields[2])
        except ValueError:
            continue
        previous = devices.get(device)
        if previous is None or total > previous[0]:
            devices[device] = (total, used)
    if not devices:
        raise ValueError("df returned no usable filesystems")
    return sum(item[0] for item in devices.values()), sum(item[1] for item in devices.values())


def parse_proc_net_dev(value: bytes | str) -> tuple[int, int]:
    """解析 /proc/net/dev，排除 loopback 后汇总收发字节数。"""
    text = value.decode(errors="replace") if isinstance(value, bytes) else value
    received = 0
    transmitted = 0
    found = False
    for line in text.splitlines():
        if ":" not in line:
            continue
        interface, raw = line.split(":", 1)
        if interface.strip() == "lo":
            continue
        fields = raw.split()
        if len(fields) < 16:
            continue
        try:
            received += int(fields[0])
            transmitted += int(fields[8])
        except ValueError:
            continue
        found = True
    if not found:
        return 0, 0
    return received, transmitted
