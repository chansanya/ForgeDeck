from __future__ import annotations

from devops.runner.metrics import (
    CPUCounters,
    cpu_percent,
    parse_df_bytes,
    parse_proc_meminfo,
    parse_proc_net_dev,
    parse_proc_stat,
)


def test_linux_metric_parsers() -> None:
    cpu = parse_proc_stat("cpu  100 0 50 800 10 0 0 0\ncpu0 1 2 3 4")
    assert cpu == CPUCounters(total=960, idle=810)
    assert cpu_percent(CPUCounters(1160, 910), cpu) == 50.0

    total, used = parse_proc_meminfo(
        "MemTotal: 1000 kB\nMemAvailable: 250 kB\nMemFree: 100 kB\n"
    )
    assert total == 1_024_000
    assert used == 768_000

    disk_total, disk_used = parse_df_bytes(
        "Filesystem 1-blocks Used Available Capacity Mounted on\n"
        "/dev/sda1 1000 400 600 40% /\n"
        "/dev/sda1 1000 400 600 40% /bind\n"
        "/dev/sdb1 2000 500 1500 25% /data\n"
    )
    assert (disk_total, disk_used) == (3000, 900)

    rx, tx = parse_proc_net_dev(
        "Inter-| Receive | Transmit\n"
        " lo: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0\n"
        "eth0: 300 0 0 0 0 0 0 0 500 0 0 0 0 0 0 0\n"
    )
    assert (rx, tx) == (300, 500)
