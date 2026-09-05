#!/usr/bin/env python3
"""Live Linux performance dashboard for an X11 window under Sway."""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


Color = Tuple[float, float, float]
CpuTimes = Tuple[int, int]
NetworkBytes = Tuple[int, int]
BlockDevice = Tuple[int, int, str]
DiskCounters = Tuple[int, int, int]
MemoryStats = Tuple[float, float, float, float]

HEAT_STOPS: Sequence[Tuple[float, Color]] = (
    (0.00, (0.000, 0.000, 0.000)),
    (0.10, (0.000, 0.000, 0.031)),
    (0.28, (0.000, 0.027, 0.235)),
    (0.48, (0.039, 0.098, 0.588)),
    (0.66, (0.294, 0.000, 0.569)),
    (0.82, (0.706, 0.000, 0.255)),
    (1.00, (1.000, 0.071, 0.000)),
)


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def heat_color(load: float) -> Color:
    load = clamp01(load)
    for index in range(1, len(HEAT_STOPS)):
        high_position, high_color = HEAT_STOPS[index]
        if load <= high_position:
            low_position, low_color = HEAT_STOPS[index - 1]
            blend = (load - low_position) / (high_position - low_position)
            return tuple(
                low + (high - low) * blend
                for low, high in zip(low_color, high_color)
            )  # type: ignore[return-value]
    return HEAT_STOPS[-1][1]


def bar_heat_color(usage: float) -> Color:
    """Use the same minimum visible heat level as low-rate network text."""
    return heat_color(max(0.36, clamp01(usage)))


def choose_grid(cpu_count: int, width: int, height: int) -> Tuple[int, int]:
    """Choose a compact grid whose cells make the largest possible circles."""
    if cpu_count <= 0:
        return 1, 1

    candidates = []
    for columns in range(1, cpu_count + 1):
        rows = math.ceil(cpu_count / columns)
        unused = columns * rows - cpu_count
        cell_diameter = min(width / columns, height / rows)
        candidates.append((unused, -cell_diameter, columns, rows))

    _, _, columns, rows = min(candidates)
    return columns, rows


def parse_proc_stat(lines: Iterable[str]) -> Dict[int, CpuTimes]:
    """Return {cpu_index: (total_ticks, idle_ticks)} from /proc/stat text."""
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

        # Linux reports guest time inside user/nice already. Summing the first
        # eight fields avoids double-counting guest and guest_nice.
        total = sum(values[:8])
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        result[int(suffix)] = (total, idle)
    return result


def read_proc_stat(path: str = "/proc/stat") -> Dict[int, CpuTimes]:
    with open(path, "r", encoding="ascii") as proc_stat:
        return parse_proc_stat(proc_stat)


class CpuLoadSampler:
    def __init__(self) -> None:
        self.previous = read_proc_stat()

    def sample(self) -> List[float]:
        current = read_proc_stat()
        loads: List[float] = []

        for cpu_index in sorted(current):
            total, idle = current[cpu_index]
            old_total, old_idle = self.previous.get(cpu_index, (total, idle))
            total_delta = total - old_total
            idle_delta = idle - old_idle
            load = 0.0 if total_delta <= 0 else 1.0 - idle_delta / total_delta
            loads.append(clamp01(load))

        self.previous = current
        return loads


def parse_default_route(lines: Iterable[str]) -> Optional[str]:
    """Return the lowest-metric active IPv4 default-route interface."""
    candidates: List[Tuple[int, str]] = []
    for line in lines:
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


def default_route_interface(path: str = "/proc/net/route") -> Optional[str]:
    try:
        with open(path, "r", encoding="ascii") as route_file:
            return parse_default_route(route_file)
    except OSError:
        return None


def parse_network_bytes(lines: Iterable[str], interface: str) -> Optional[NetworkBytes]:
    """Return received/transmitted byte counters for one interface."""
    for line in lines:
        if ":" not in line:
            continue
        name, counters = line.split(":", 1)
        if name.strip() != interface:
            continue
        fields = counters.split()
        if len(fields) < 9:
            return None
        try:
            return int(fields[0]), int(fields[8])
        except ValueError:
            return None
    return None


def read_network_bytes(interface: str, path: str = "/proc/net/dev") -> Optional[NetworkBytes]:
    try:
        with open(path, "r", encoding="ascii") as network_file:
            return parse_network_bytes(network_file, interface)
    except OSError:
        return None


def read_link_capacity(
    interface: Optional[str], sysfs_root: str = "/sys/class/net"
) -> Optional[float]:
    """Return negotiated interface capacity in bits/s when reported."""
    if interface:
        try:
            with open(
                os.path.join(sysfs_root, interface, "speed"), "r", encoding="ascii"
            ) as speed_file:
                speed_mbps = int(speed_file.read().strip())
                if speed_mbps > 0:
                    return speed_mbps * 1_000_000.0
        except (OSError, ValueError):
            pass
    return None


class NetworkRateSampler:
    def __init__(self, requested_interface: Optional[str]) -> None:
        self.interface = requested_interface or default_route_interface()
        self.negotiated_capacity = read_link_capacity(self.interface)
        self.link_capacity = self.negotiated_capacity or 1_000_000_000.0
        self.previous_time = time.monotonic()
        self.previous = read_network_bytes(self.interface) if self.interface else None

    def sample(self) -> Tuple[float, float]:
        if not self.interface:
            self.interface = default_route_interface()
            self.negotiated_capacity = read_link_capacity(self.interface)
            self.link_capacity = self.negotiated_capacity or 1_000_000_000.0
        if not self.interface:
            return 0.0, 0.0

        now = time.monotonic()
        current = read_network_bytes(self.interface)
        elapsed = now - self.previous_time
        if current is None or self.previous is None or elapsed <= 0.0:
            self.previous = current
            self.previous_time = now
            return 0.0, 0.0

        received_delta = max(0, current[0] - self.previous[0])
        transmitted_delta = max(0, current[1] - self.previous[1])
        self.previous = current
        self.previous_time = now
        return received_delta * 8.0 / elapsed, transmitted_delta * 8.0 / elapsed


def network_heat_color(bits_per_second: float, link_capacity: float) -> Color:
    """Map link activity into the common heat palette with log-like visibility."""
    if bits_per_second <= 0.0 or link_capacity <= 0.0:
        return 0.20, 0.22, 0.28
    utilization = clamp01(bits_per_second / link_capacity)
    visible_level = 0.30 + 0.70 * utilization ** 0.25
    return heat_color(visible_level)


def parse_memory_stats(lines: Iterable[str]) -> MemoryStats:
    values: Dict[str, int] = {}
    for line in lines:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        fields = value.split()
        if fields and fields[0].isdigit():
            values[name] = int(fields[0])
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    usage = 0.0 if total <= 0 else clamp01((total - available) / total)
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    swap_usage = (
        0.0 if swap_total <= 0 else clamp01((swap_total - swap_free) / swap_total)
    )
    return usage, total * 1000.0, swap_usage, swap_total * 1000.0


def parse_memory_usage(lines: Iterable[str]) -> float:
    return parse_memory_stats(lines)[0]


def read_memory_stats(path: str = "/proc/meminfo") -> MemoryStats:
    try:
        with open(path, "r", encoding="ascii") as memory_file:
            return parse_memory_stats(memory_file)
    except OSError:
        return 0.0, 0.0, 0.0, 0.0


def read_memory_usage(path: str = "/proc/meminfo") -> float:
    return read_memory_stats(path)[0]


def parse_root_block_device(lines: Iterable[str]) -> Optional[BlockDevice]:
    """Return (major, minor, device name) for the root filesystem."""
    for line in lines:
        fields = line.split()
        if len(fields) < 10 or fields[4] != "/" or "-" not in fields:
            continue
        try:
            major_text, minor_text = fields[2].split(":", 1)
            separator = fields.index("-")
            source = fields[separator + 2]
            return int(major_text), int(minor_text), os.path.basename(source)
        except (ValueError, IndexError):
            continue
    return None


def root_block_device(path: str = "/proc/self/mountinfo") -> Optional[BlockDevice]:
    try:
        with open(path, "r", encoding="utf-8") as mount_file:
            return parse_root_block_device(mount_file)
    except OSError:
        return None


def parse_disk_counters(
    lines: Iterable[str], major: int, minor: int
) -> Optional[DiskCounters]:
    """Return read sectors, written sectors, and cumulative busy milliseconds."""
    for line in lines:
        fields = line.split()
        if len(fields) < 13:
            continue
        try:
            if int(fields[0]) == major and int(fields[1]) == minor:
                return int(fields[5]), int(fields[9]), int(fields[12])
        except ValueError:
            continue
    return None


def read_disk_counters(
    device: Optional[BlockDevice], path: str = "/proc/diskstats"
) -> Optional[DiskCounters]:
    if device is None:
        return None
    try:
        with open(path, "r", encoding="ascii") as disk_file:
            return parse_disk_counters(disk_file, device[0], device[1])
    except OSError:
        return None


class SystemUsageSampler:
    def __init__(self) -> None:
        self.device = root_block_device()
        (
            self.memory_usage,
            self.memory_total_bytes,
            self.swap_usage,
            self.swap_total_bytes,
        ) = read_memory_stats()
        self.previous_disk = read_disk_counters(self.device)
        self.previous_time = time.monotonic()

    @property
    def device_name(self) -> str:
        return self.device[2] if self.device else "NO ROOT DISK"

    def sample(self) -> Tuple[float, float, float, float, float]:
        (
            self.memory_usage,
            self.memory_total_bytes,
            self.swap_usage,
            self.swap_total_bytes,
        ) = read_memory_stats()
        now = time.monotonic()
        disk = read_disk_counters(self.device)
        elapsed = now - self.previous_time
        elapsed_ms = elapsed * 1000.0
        if disk is None or self.previous_disk is None or elapsed <= 0.0:
            disk_usage = 0.0
            read_bytes_per_second = 0.0
            write_bytes_per_second = 0.0
        else:
            disk_usage = clamp01((disk[2] - self.previous_disk[2]) / elapsed_ms)
            # Linux diskstats sector counters are expressed in 512-byte sectors.
            read_bytes_per_second = max(0, disk[0] - self.previous_disk[0]) * 512.0 / elapsed
            write_bytes_per_second = max(0, disk[1] - self.previous_disk[1]) * 512.0 / elapsed
        self.previous_disk = disk
        self.previous_time = now
        return (
            self.memory_usage,
            self.swap_usage,
            disk_usage,
            read_bytes_per_second,
            write_bytes_per_second,
        )


def format_disk_rate(bytes_per_second: float) -> str:
    megabytes_per_second = max(0.0, bytes_per_second) / 1_000_000.0
    if megabytes_per_second < 10.0:
        return f"{megabytes_per_second:.2f} MB/s"
    if megabytes_per_second < 100.0:
        return f"{megabytes_per_second:.1f} MB/s"
    return f"{megabytes_per_second:.0f} MB/s"


def format_memory_capacity(usage: float, total_bytes: float) -> str:
    total_gb = max(0.0, total_bytes) / 1_000_000_000.0
    used_gb = clamp01(usage) * total_gb
    return f"{used_gb:.1f} / {total_gb:.1f} GB"


def format_bit_rate(bits_per_second: float) -> str:
    rate = max(0.0, bits_per_second)
    if rate < 1_000_000.0:
        kbps = rate / 1_000.0
        return f"{kbps:.1f} kbps" if kbps < 100.0 else f"{kbps:.0f} kbps"
    mbps = rate / 1_000_000.0
    return f"{mbps:.1f} Mbps" if mbps < 100.0 else f"{mbps:.0f} Mbps"


def format_link_speed(bits_per_second: Optional[float]) -> str:
    if bits_per_second is None or bits_per_second <= 0.0:
        return "UNKNOWN"
    if bits_per_second >= 1_000_000_000.0:
        gbps = bits_per_second / 1_000_000_000.0
        value = f"{gbps:.0f}" if gbps.is_integer() else f"{gbps:.1f}"
        return f"{value} Gbps"
    return f"{bits_per_second / 1_000_000.0:.0f} Mbps"


def draw_centered_text(
    context: cairo.Context,
    text: str,
    center_x: float,
    baseline_y: float,
    size: float,
    color: Color,
    weight: int = cairo.FONT_WEIGHT_NORMAL,
) -> None:
    context.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, weight)
    context.set_font_size(size)
    x_bearing, _, text_width, _, _, _ = context.text_extents(text)
    context.set_source_rgb(*color)
    context.move_to(center_x - text_width / 2.0 - x_bearing, baseline_y)
    context.show_text(text)


def discover_sway_socket() -> Optional[str]:
    configured = os.environ.get("SWAYSOCK")
    if configured and os.path.exists(configured):
        return configured

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    sockets = sorted(glob.glob(os.path.join(runtime_dir, f"sway-ipc.{os.getuid()}.*.sock")))
    return sockets[-1] if sockets else None


def sway_outputs(environment: Dict[str, str]) -> List[dict]:
    try:
        completed = subprocess.run(
            ["swaymsg", "-t", "get_outputs", "-r"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=2,
        )
        return [output for output in json.loads(completed.stdout) if output.get("active")]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []


def choose_target_output(outputs: Sequence[dict], requested: Optional[str]) -> Optional[dict]:
    if requested:
        return next((output for output in outputs if output.get("name") == requested), None)

    if len(outputs) < 2:
        return None

    # Prefer the planned ultrawide panel regardless of connector name.
    for output in outputs:
        rect = output.get("rect", {})
        mode = output.get("current_mode", {})
        width = mode.get("width", rect.get("width"))
        height = mode.get("height", rect.get("height"))
        if width == 1920 and height == 720:
            return output

    # Otherwise use a non-focused display as the natural secondary output.
    return next((output for output in outputs if not output.get("focused")), outputs[-1])


@dataclass
class Settings:
    output: Optional[str]
    network_interface: Optional[str]
    windowed: bool
    fps: float
    sample_seconds: float
    heating_seconds: float
    cooling_seconds: float
    circle_scale: float


class HeatmapWindow(Gtk.Window):
    def __init__(self, settings: Settings) -> None:
        super().__init__(title="Live Performance Dashboard")
        self.settings = settings
        self.set_wmclass("live-performance-dashboard", "LivePerformanceDashboard")
        self.set_decorated(False)
        self.set_default_size(960, 360)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self.on_key_press)

        self.canvas = Gtk.DrawingArea()
        self.canvas.connect("draw", self.on_draw)
        self.add(self.canvas)

        self.sampler = CpuLoadSampler()
        self.network_sampler = NetworkRateSampler(settings.network_interface)
        self.system_sampler = SystemUsageSampler()
        cpu_count = max(1, len(self.sampler.previous))
        self.target_loads = [0.0] * cpu_count
        self.displayed_loads = [0.0] * cpu_count
        self.target_download = 0.0
        self.target_upload = 0.0
        self.displayed_download = 0.0
        self.displayed_upload = 0.0
        self.target_memory_usage = self.system_sampler.memory_usage
        self.target_swap_usage = self.system_sampler.swap_usage
        self.target_disk_usage = 0.0
        self.target_disk_read = 0.0
        self.target_disk_write = 0.0
        self.displayed_memory_usage = self.target_memory_usage
        self.displayed_swap_usage = self.target_swap_usage
        self.displayed_disk_usage = 0.0
        self.displayed_disk_read = 0.0
        self.displayed_disk_write = 0.0
        self.last_frame_time = time.monotonic()
        self.placed_output: Optional[str] = None

        self.sway_environment = os.environ.copy()
        sway_socket = discover_sway_socket()
        if sway_socket:
            self.sway_environment["SWAYSOCK"] = sway_socket

        frame_interval_ms = max(8, round(1000.0 / settings.fps))
        sample_interval_ms = max(20, round(1000.0 * settings.sample_seconds))
        GLib.timeout_add(frame_interval_ms, self.animate)
        GLib.timeout_add(sample_interval_ms, self.sample_cpu_load)
        GLib.timeout_add(300, self.place_on_output)
        GLib.timeout_add_seconds(2, self.place_on_output)

    def on_key_press(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval in (Gdk.KEY_Escape, Gdk.KEY_q, Gdk.KEY_Q):
            self.close()
            return True
        return False

    def sample_cpu_load(self) -> bool:
        sampled = self.sampler.sample()
        if len(sampled) != len(self.target_loads):
            self.target_loads = sampled
            self.displayed_loads = [0.0] * len(sampled)
        else:
            self.target_loads = sampled
        self.target_download, self.target_upload = self.network_sampler.sample()
        (
            self.target_memory_usage,
            self.target_swap_usage,
            self.target_disk_usage,
            self.target_disk_read,
            self.target_disk_write,
        ) = self.system_sampler.sample()
        return True

    def animate(self) -> bool:
        now = time.monotonic()
        elapsed = min(1.0, now - self.last_frame_time)
        self.last_frame_time = now

        for index, target in enumerate(self.target_loads):
            current = self.displayed_loads[index]
            time_constant = (
                self.settings.heating_seconds
                if target >= current
                else self.settings.cooling_seconds
            )
            blend = 1.0 - math.exp(-elapsed / time_constant)
            self.displayed_loads[index] = current + (target - current) * blend

        network_blend = 1.0 - math.exp(-elapsed / 0.25)
        self.displayed_download += (
            self.target_download - self.displayed_download
        ) * network_blend
        self.displayed_upload += (
            self.target_upload - self.displayed_upload
        ) * network_blend

        system_blend = 1.0 - math.exp(-elapsed / 0.35)
        self.displayed_memory_usage += (
            self.target_memory_usage - self.displayed_memory_usage
        ) * system_blend
        self.displayed_swap_usage += (
            self.target_swap_usage - self.displayed_swap_usage
        ) * system_blend
        self.displayed_disk_usage += (
            self.target_disk_usage - self.displayed_disk_usage
        ) * system_blend
        self.displayed_disk_read += (
            self.target_disk_read - self.displayed_disk_read
        ) * system_blend
        self.displayed_disk_write += (
            self.target_disk_write - self.displayed_disk_write
        ) * system_blend

        self.canvas.queue_draw()
        return True

    def on_draw(self, widget: Gtk.DrawingArea, context: cairo.Context) -> bool:
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        context.set_source_rgb(0.0, 0.0, 0.0)
        context.paint()
        context.set_antialias(cairo.ANTIALIAS_BEST)

        # Preserve 57% for the CPU heatmap. The right-hand 43% becomes three
        # full-width landscape rows for memory, disk I/O, and network.
        metrics_width = width * 0.43
        heatmap_width = max(1.0, width - metrics_width)
        metric_row_height = height / 3.0

        cpu_count = len(self.displayed_loads)
        columns, rows = choose_grid(cpu_count, int(heatmap_width), height)
        cell_width = heatmap_width / columns
        cell_height = height / rows
        radius = min(cell_width, cell_height) * self.settings.circle_scale / 2.0

        for index, load in enumerate(self.displayed_loads):
            column = index % columns
            row = index // columns
            center_x = (column + 0.5) * cell_width
            center_y = (row + 0.5) * cell_height
            red, green, blue = heat_color(load)
            context.set_source_rgb(red, green, blue)
            context.arc(center_x, center_y, radius, 0.0, math.tau)
            context.fill()

        context.set_source_rgb(0.16, 0.18, 0.23)
        context.rectangle(heatmap_width, 0.0, 1.0, height)
        context.rectangle(heatmap_width, metric_row_height, metrics_width, 1.0)
        context.rectangle(
            heatmap_width, metric_row_height * 2.0, metrics_width, 1.0
        )
        context.fill()

        self.draw_memory_panel(
            context, heatmap_width, 0.0, metrics_width, metric_row_height
        )
        self.draw_disk_panel(
            context,
            heatmap_width,
            metric_row_height,
            metrics_width,
            metric_row_height,
        )
        self.draw_network_panel(
            context,
            heatmap_width,
            metric_row_height * 2.0,
            metrics_width,
            metric_row_height,
        )

        return False

    def draw_usage_bar(
        self,
        context: cairo.Context,
        x: float,
        y: float,
        width: float,
        height: float,
        usage: float,
        active_color: Color,
        idle_color: Color,
    ) -> None:
        usage = clamp01(usage)
        context.set_source_rgb(*idle_color)
        context.rectangle(x, y, width, height)
        context.fill()
        context.set_source_rgb(*active_color)
        context.rectangle(x, y, width * usage, height)
        context.fill()

    def draw_memory_panel(
        self,
        context: cairo.Context,
        panel_x: float,
        panel_y: float,
        panel_width: float,
        panel_height: float,
    ) -> None:
        label_size = min(24.0, panel_height * 0.10)
        value_size = min(30.0, panel_height * 0.13)
        detail_size = min(18.0, panel_height * 0.075)
        label_x = panel_x + panel_width * 0.10
        value_x = panel_x + panel_width * 0.84
        bar_x = panel_x + panel_width * 0.20
        bar_width = panel_width * 0.47
        bar_height = panel_height * 0.20

        draw_centered_text(
            context, "MEMORY", label_x, panel_y + panel_height * 0.31,
            label_size, (0.55, 0.59, 0.68), cairo.FONT_WEIGHT_BOLD,
        )
        self.draw_usage_bar(
            context, bar_x, panel_y + panel_height * 0.19, bar_width, bar_height,
            self.displayed_memory_usage, bar_heat_color(self.displayed_memory_usage),
            (0.065, 0.070, 0.082),
        )
        draw_centered_text(
            context, f"{self.displayed_memory_usage * 100.0:.0f}%", value_x,
            panel_y + panel_height * 0.28, value_size, (0.55, 0.59, 0.68),
            cairo.FONT_WEIGHT_BOLD,
        )
        draw_centered_text(
            context,
            format_memory_capacity(
                self.displayed_memory_usage, self.system_sampler.memory_total_bytes
            ),
            value_x, panel_y + panel_height * 0.44, detail_size,
            (0.42, 0.46, 0.54),
        )

        draw_centered_text(
            context, "SWAP", label_x, panel_y + panel_height * 0.76,
            label_size, (0.55, 0.59, 0.68), cairo.FONT_WEIGHT_BOLD,
        )
        self.draw_usage_bar(
            context, bar_x, panel_y + panel_height * 0.64, bar_width, bar_height,
            self.displayed_swap_usage, bar_heat_color(self.displayed_swap_usage),
            (0.065, 0.070, 0.082),
        )
        draw_centered_text(
            context, f"{self.displayed_swap_usage * 100.0:.0f}%", value_x,
            panel_y + panel_height * 0.73, value_size, (0.55, 0.59, 0.68),
            cairo.FONT_WEIGHT_BOLD,
        )
        draw_centered_text(
            context,
            format_memory_capacity(
                self.displayed_swap_usage, self.system_sampler.swap_total_bytes
            ),
            value_x, panel_y + panel_height * 0.89, detail_size,
            (0.42, 0.46, 0.54),
        )

    def draw_disk_panel(
        self,
        context: cairo.Context,
        panel_x: float,
        panel_y: float,
        panel_width: float,
        panel_height: float,
    ) -> None:
        label_size = min(24.0, panel_height * 0.10)
        value_size = min(30.0, panel_height * 0.13)
        detail_size = min(18.0, panel_height * 0.075)
        small_size = min(15.0, panel_height * 0.063)
        device_size = min(17.0, panel_height * 0.071)
        label_x = panel_x + panel_width * 0.10
        bar_x = panel_x + panel_width * 0.20
        bar_width = panel_width * 0.34

        draw_centered_text(
            context, "DISK I/O", label_x, panel_y + panel_height * 0.39,
            label_size, (0.55, 0.59, 0.68), cairo.FONT_WEIGHT_BOLD,
        )
        draw_centered_text(
            context, self.system_sampler.device_name, label_x,
            panel_y + panel_height * 0.61, device_size, (0.32, 0.37, 0.46),
        )
        self.draw_usage_bar(
            context, bar_x, panel_y + panel_height * 0.29, bar_width,
            panel_height * 0.22, self.displayed_disk_usage,
            bar_heat_color(self.displayed_disk_usage), (0.065, 0.070, 0.082),
        )
        draw_centered_text(
            context, f"{self.displayed_disk_usage * 100.0:.0f}%",
            panel_x + panel_width * 0.59, panel_y + panel_height * 0.46,
            value_size, (0.55, 0.59, 0.68), cairo.FONT_WEIGHT_BOLD,
        )

        for title, rate, center_ratio in (
            ("READ", self.displayed_disk_read, 0.74),
            ("WRITE", self.displayed_disk_write, 0.91),
        ):
            center_x = panel_x + panel_width * center_ratio
            draw_centered_text(
                context, title, center_x, panel_y + panel_height * 0.34,
                small_size, (0.40, 0.44, 0.52), cairo.FONT_WEIGHT_BOLD,
            )
            draw_centered_text(
                context, format_disk_rate(rate), center_x,
                panel_y + panel_height * 0.57, detail_size,
                (0.52, 0.56, 0.65), cairo.FONT_WEIGHT_BOLD,
            )

    def draw_network_panel(
        self,
        context: cairo.Context,
        panel_x: float,
        panel_y: float,
        panel_width: float,
        panel_height: float,
    ) -> None:
        label_size = min(24.0, panel_height * 0.10)
        rate_size = min(44.0, panel_height * 0.18)
        link_rate_size = rate_size * 0.72
        device_size = min(17.0, panel_height * 0.071)
        upload_color = network_heat_color(
            self.displayed_upload, self.network_sampler.link_capacity
        )
        download_color = network_heat_color(
            self.displayed_download, self.network_sampler.link_capacity
        )

        for title, rate, color, center_ratio in (
            ("UPLINK", self.displayed_upload, upload_color, 0.18),
            ("DOWNLINK", self.displayed_download, download_color, 0.50),
        ):
            center_x = panel_x + panel_width * center_ratio
            draw_centered_text(
                context, title, center_x, panel_y + panel_height * 0.36,
                label_size, (0.55, 0.59, 0.68), cairo.FONT_WEIGHT_BOLD,
            )
            draw_centered_text(
                context, format_bit_rate(rate), center_x,
                panel_y + panel_height * 0.64, rate_size, color,
                cairo.FONT_WEIGHT_BOLD,
            )

        link_center_x = panel_x + panel_width * 0.82
        interface = self.network_sampler.interface or "NO DEFAULT ROUTE"
        draw_centered_text(
            context, "LINK SPEED", link_center_x,
            panel_y + panel_height * 0.36, label_size,
            (0.55, 0.59, 0.68), cairo.FONT_WEIGHT_BOLD,
        )
        draw_centered_text(
            context, format_link_speed(self.network_sampler.negotiated_capacity),
            link_center_x, panel_y + panel_height * 0.64, link_rate_size,
            (0.50, 0.50, 0.50), cairo.FONT_WEIGHT_BOLD,
        )
        draw_centered_text(
            context, interface, link_center_x, panel_y + panel_height * 0.86,
            device_size,
            (0.32, 0.37, 0.46),
        )

    def place_on_output(self) -> bool:
        if self.settings.windowed or self.placed_output:
            return True

        outputs = sway_outputs(self.sway_environment)
        target = choose_target_output(outputs, self.settings.output)
        if not target:
            return True

        output_name = str(target["name"]).replace('"', "")
        criteria = '[class="^LivePerformanceDashboard$"]'
        commands = (
            f'{criteria} move container to output "{output_name}"',
            f"{criteria} border none",
            f"{criteria} fullscreen enable",
        )
        try:
            # Keep the heatmap panel in its native wide landscape orientation.
            subprocess.run(
                ["swaymsg", "output", output_name, "transform", "normal"],
                check=True,
                capture_output=True,
                text=True,
                env=self.sway_environment,
                timeout=2,
            )
            for command in commands:
                subprocess.run(
                    ["swaymsg", command],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=self.sway_environment,
                    timeout=2,
                )
            self.placed_output = output_name
            print(f"Heatmap placed fullscreen on {output_name}", flush=True)
        except (OSError, subprocess.SubprocessError):
            pass
        return True


def run_self_test() -> None:
    assert choose_grid(24, 1920, 720) == (8, 3)
    assert choose_grid(4, 1920, 720) == (4, 1)
    sample = parse_proc_stat(
        (
            "cpu  20 0 10 70 0 0 0 0 0 0\n",
            "cpu0 10 0 5 35 0 0 0 0 0 0\n",
            "cpu1 10 0 5 35 0 0 0 0 0 0\n",
        )
    )
    assert sample == {0: (50, 35), 1: (50, 35)}
    assert parse_default_route(
        (
            "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n",
            "eth0 00000000 0101A8C0 0003 0 0 100 00000000 0 0 0\n",
        )
    ) == "eth0"
    assert parse_network_bytes(
        ("  eth0: 1200 1 0 0 0 0 0 0 3400 2 0 0 0 0 0 0\n",), "eth0"
    ) == (1200, 3400)
    assert parse_memory_usage(
        ("MemTotal: 1000 kB\n", "MemAvailable: 350 kB\n")
    ) == 0.65
    assert parse_memory_stats(
        (
            "MemTotal: 1000 kB\n",
            "MemAvailable: 350 kB\n",
            "SwapTotal: 500 kB\n",
            "SwapFree: 400 kB\n",
        )
    ) == (0.65, 1_000_000.0, 0.2, 500_000.0)
    assert parse_root_block_device(
        ("1 0 259:2 / / rw - ext4 /dev/nvme0n1p2 rw\n",)
    ) == (259, 2, "nvme0n1p2")
    assert parse_disk_counters(
        ("259 2 nvme0n1p2 10 0 100 5 20 0 200 7 0 42 60\n",), 259, 2
    ) == (100, 200, 42)
    assert format_disk_rate(850_000.0) == "0.85 MB/s"
    assert format_disk_rate(124_000_000.0) == "124 MB/s"
    assert format_memory_capacity(0.25, 16_000_000_000.0) == "4.0 / 16.0 GB"
    assert format_bit_rate(850_000.0) == "850 kbps"
    assert format_bit_rate(12_500_000.0) == "12.5 Mbps"
    assert format_link_speed(1_000_000_000.0) == "1 Gbps"
    assert format_link_speed(2_500_000_000.0) == "2.5 Gbps"
    assert format_link_speed(100_000_000.0) == "100 Mbps"
    assert format_link_speed(None) == "UNKNOWN"
    assert network_heat_color(1_000_000_000.0, 1_000_000_000.0) == heat_color(1.0)
    assert network_heat_color(0.0, 1_000_000_000.0) == (0.20, 0.22, 0.28)
    assert heat_color(0.0) == (0.0, 0.0, 0.0)
    assert heat_color(1.0) == HEAT_STOPS[-1][1]
    assert bar_heat_color(0.14) == heat_color(0.36)
    assert bar_heat_color(1.0) == heat_color(1.0)
    print("Self-test passed")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Sway output name; otherwise auto-detect the second display")
    parser.add_argument("--interface", help="network interface; otherwise use the IPv4 default route")
    parser.add_argument("--windowed", action="store_true", help="do not move or fullscreen the window")
    parser.add_argument("--fps", type=float, default=60.0)
    parser.add_argument("--sample-ms", type=float, default=50.0)
    parser.add_argument("--heating-seconds", type=float, default=0.15)
    parser.add_argument("--cooling-seconds", type=float, default=0.25)
    parser.add_argument("--circle-scale", type=float, default=0.75)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.self_test:
        run_self_test()
        return 0

    if arguments.fps <= 0 or arguments.sample_ms <= 0:
        raise SystemExit("fps and sample-ms must be positive")
    if arguments.heating_seconds <= 0 or arguments.cooling_seconds <= 0:
        raise SystemExit("heating and cooling times must be positive")
    if not 0.05 <= arguments.circle_scale <= 1.0:
        raise SystemExit("circle-scale must be between 0.05 and 1.0")

    settings = Settings(
        output=arguments.output,
        network_interface=arguments.interface,
        windowed=arguments.windowed,
        fps=arguments.fps,
        sample_seconds=arguments.sample_ms / 1000.0,
        heating_seconds=arguments.heating_seconds,
        cooling_seconds=arguments.cooling_seconds,
        circle_scale=arguments.circle_scale,
    )

    window = HeatmapWindow(settings)
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
