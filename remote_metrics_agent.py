#!/usr/bin/env python3
"""Stream Linux performance measurements to Live Performance Dashboard."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from typing import Dict, Iterable, List, Optional, Tuple


CpuTimes = Tuple[int, int]
BlockDevice = Tuple[int, int, str]
DiskCounters = Tuple[int, int, int]


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def parse_cpu_times(lines: Iterable[str]) -> Dict[int, CpuTimes]:
    result: Dict[int, CpuTimes] = {}
    for line in lines:
        fields = line.split()
        if not fields or not fields[0].startswith("cpu") or fields[0] == "cpu":
            continue
        suffix = fields[0][3:]
        if not suffix.isdigit():
            continue
        values = [int(value) for value in fields[1:]]
        if len(values) < 4:
            continue
        total = sum(values[:8])
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        result[int(suffix)] = total, idle
    return result


def read_cpu_times() -> Dict[int, CpuTimes]:
    with open("/proc/stat", "r", encoding="ascii") as source:
        return parse_cpu_times(source)


class CpuSampler:
    def __init__(self) -> None:
        self.previous = read_cpu_times()

    def sample(self) -> List[float]:
        current = read_cpu_times()
        loads: List[float] = []
        for index in sorted(current):
            total, idle = current[index]
            old_total, old_idle = self.previous.get(index, (total, idle))
            total_delta = total - old_total
            idle_delta = idle - old_idle
            loads.append(
                0.0 if total_delta <= 0 else clamp01(1.0 - idle_delta / total_delta)
            )
        self.previous = current
        return loads


def read_memory() -> Dict[str, float]:
    values: Dict[str, int] = {}
    with open("/proc/meminfo", "r", encoding="ascii") as source:
        for line in source:
            name, separator, value = line.partition(":")
            fields = value.split()
            if separator and fields and fields[0].isdigit():
                values[name] = int(fields[0])

    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    return {
        "usage": 0.0 if total <= 0 else clamp01((total - available) / total),
        "total_bytes": total * 1000.0,
        "swap_usage": (
            0.0
            if swap_total <= 0
            else clamp01((swap_total - swap_free) / swap_total)
        ),
        "swap_total_bytes": swap_total * 1000.0,
    }


def root_block_device() -> Optional[BlockDevice]:
    with open("/proc/self/mountinfo", "r", encoding="utf-8") as source:
        for line in source:
            fields = line.split()
            if len(fields) < 10 or fields[4] != "/" or "-" not in fields:
                continue
            try:
                major_text, minor_text = fields[2].split(":", 1)
                separator = fields.index("-")
                device_name = os.path.basename(fields[separator + 2])
                return int(major_text), int(minor_text), device_name
            except (ValueError, IndexError):
                continue
    return None


def read_disk_counters(device: Optional[BlockDevice]) -> Optional[DiskCounters]:
    if device is None:
        return None
    with open("/proc/diskstats", "r", encoding="ascii") as source:
        for line in source:
            fields = line.split()
            if len(fields) < 13:
                continue
            try:
                if int(fields[0]) == device[0] and int(fields[1]) == device[1]:
                    return int(fields[5]), int(fields[9]), int(fields[12])
            except ValueError:
                continue
    return None


class DiskSampler:
    def __init__(self) -> None:
        self.device = root_block_device()
        self.previous = read_disk_counters(self.device)
        self.previous_time = time.monotonic()

    def sample(self) -> Dict[str, object]:
        now = time.monotonic()
        current = read_disk_counters(self.device)
        elapsed = now - self.previous_time
        if current is None or self.previous is None or elapsed <= 0.0:
            usage = read_rate = write_rate = 0.0
        else:
            usage = clamp01((current[2] - self.previous[2]) / (elapsed * 1000.0))
            read_rate = max(0, current[0] - self.previous[0]) * 512.0 / elapsed
            write_rate = max(0, current[1] - self.previous[1]) * 512.0 / elapsed
        self.previous = current
        self.previous_time = now
        return {
            "usage": usage,
            "read_bytes_per_second": read_rate,
            "write_bytes_per_second": write_rate,
            "device": self.device[2] if self.device else "NO ROOT DISK",
        }


def default_route_interface() -> Optional[str]:
    candidates: List[Tuple[int, str]] = []
    with open("/proc/net/route", "r", encoding="ascii") as source:
        for line in source:
            fields = line.split()
            if len(fields) < 8 or fields[0] == "Iface" or fields[1] != "00000000":
                continue
            try:
                flags = int(fields[3], 16)
                metric = int(fields[6])
            except ValueError:
                continue
            if flags & 0x1 and fields[0] != "lo":
                candidates.append((metric, fields[0]))
    return min(candidates)[1] if candidates else None


def read_network_bytes(interface: str) -> Optional[Tuple[int, int]]:
    with open("/proc/net/dev", "r", encoding="ascii") as source:
        for line in source:
            if ":" not in line:
                continue
            name, counters = line.split(":", 1)
            fields = counters.split()
            if name.strip() == interface and len(fields) >= 9:
                return int(fields[0]), int(fields[8])
    return None


def read_link_speed(interface: Optional[str]) -> Optional[float]:
    if not interface:
        return None
    try:
        with open(
            os.path.join("/sys/class/net", interface, "speed"),
            "r",
            encoding="ascii",
        ) as source:
            speed_mbps = int(source.read().strip())
            return speed_mbps * 1_000_000.0 if speed_mbps > 0 else None
    except (OSError, ValueError):
        return None


class NetworkSampler:
    def __init__(self, requested_interface: Optional[str]) -> None:
        self.interface = requested_interface or default_route_interface()
        self.previous = read_network_bytes(self.interface) if self.interface else None
        self.previous_time = time.monotonic()

    def sample(self) -> Dict[str, object]:
        if not self.interface:
            self.interface = default_route_interface()
        now = time.monotonic()
        current = read_network_bytes(self.interface) if self.interface else None
        elapsed = now - self.previous_time
        if current is None or self.previous is None or elapsed <= 0.0:
            download = upload = 0.0
        else:
            download = max(0, current[0] - self.previous[0]) * 8.0 / elapsed
            upload = max(0, current[1] - self.previous[1]) * 8.0 / elapsed
        self.previous = current
        self.previous_time = now
        return {
            "interface": self.interface or "NO DEFAULT ROUTE",
            "download_bits_per_second": download,
            "upload_bits_per_second": upload,
            "link_bits_per_second": read_link_speed(self.interface),
        }


class SnapshotSampler:
    def __init__(self, interface: Optional[str]) -> None:
        self.hostname = socket.gethostname()
        self.cpu = CpuSampler()
        self.disk = DiskSampler()
        self.network = NetworkSampler(interface)

    def sample(self) -> Dict[str, object]:
        return {
            "version": 1,
            "hostname": self.hostname,
            "timestamp": time.time(),
            "cpu": self.cpu.sample(),
            "memory": read_memory(),
            "disk": self.disk.sample(),
            "network": self.network.sample(),
        }


def parse_endpoint(value: str) -> Tuple[str, int]:
    host, separator, port_text = value.rpartition(":")
    if not separator or not host:
        raise argparse.ArgumentTypeError("expected HOST:PORT")
    try:
        port = int(port_text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return host, port


def run_self_test() -> None:
    assert parse_endpoint("192.0.2.10:9177") == ("192.0.2.10", 9177)
    parsed = parse_cpu_times(("cpu0 10 0 5 35 0 0 0 0 0 0\n",))
    assert parsed == {0: (50, 35)}
    print("Remote agent self-test passed")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", type=parse_endpoint, help="dashboard HOST:PORT")
    parser.add_argument("--interface", help="network interface; default route if omitted")
    parser.add_argument("--interval-ms", type=float, default=50.0)
    parser.add_argument("--reconnect-seconds", type=float, default=2.0)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.self_test:
        run_self_test()
        return 0
    if arguments.dashboard is None:
        raise SystemExit("--dashboard HOST:PORT is required")
    if arguments.interval_ms < 20.0 or arguments.reconnect_seconds <= 0.0:
        raise SystemExit("interval must be at least 20 ms and reconnect must be positive")

    sampler = SnapshotSampler(arguments.interface)
    while True:
        try:
            with socket.create_connection(arguments.dashboard, timeout=5.0) as connection:
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                print(f"Connected to {arguments.dashboard[0]}:{arguments.dashboard[1]}")
                while True:
                    payload = json.dumps(sampler.sample(), separators=(",", ":"))
                    connection.sendall(payload.encode("utf-8") + b"\n")
                    time.sleep(arguments.interval_ms / 1000.0)
        except (ConnectionError, OSError) as error:
            print(f"Connection unavailable: {error}; retrying", file=sys.stderr)
            time.sleep(arguments.reconnect_seconds)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
