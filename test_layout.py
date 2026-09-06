"""Headless rendering regressions; no display server or metric listener needed."""
import math
import os
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import remote_metrics_agent as agent

import cairo
import live_performance_dashboard as dashboard


class LayoutTests(unittest.TestCase):
    def test_tab_positions_ignore_packet_arrival_order(self):
        goose = ("remote:golden-goose", {"hostname": "golden-goose"}, 1.0)
        howaborm = ("remote:howaborm", {"hostname": "howaborm"}, 2.0)
        window = SimpleNamespace(
            remote_server=SimpleNamespace(active=Mock(side_effect=[
                [goose, howaborm], [howaborm, goose], [goose, howaborm],
            ])),
            settings=SimpleNamespace(remote_timeout=2.0),
            selected_source_key="remote:howaborm",
            active_source_key="remote:howaborm",
            local_source_key="local:raspberrypi", local_hostname="raspberrypi",
            apply_remote_snapshot=Mock(),
        )
        context = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 960, 360))
        positions = []
        for _ in range(3):
            dashboard.HeatmapWindow.sample_cpu_load(window)
            with patch.object(dashboard, "draw_centered_text") as draw_text:
                dashboard.HeatmapWindow.draw_source_tabs(window, context, 0, 960, 32)
                self.assertEqual([call.args[1] for call in draw_text.call_args_list], [
                    "LOCAL raspberrypi", "REMOTE golden-goose", "REMOTE howaborm",
                ])
            positions.append(list(window.source_tab_hitboxes))
            self.assertEqual(dashboard.HeatmapWindow.source_keys(window), [
                "local:raspberrypi", "remote:golden-goose", "remote:howaborm",
            ])
        self.assertEqual(positions[0], positions[1])
        self.assertEqual(positions[1], positions[2])
        self.assertEqual(window.apply_remote_snapshot.call_count, 3)

    def test_cpu_counts(self):
        for count in (1, 2, 4, 6, 7, 13, 24, 64, 127, 256, 1024, 4096):
            for width, height in ((1094, 644), (320, 220), (600, 850)):
                columns, rows = dashboard.choose_grid(count, width, height)
                self.assertGreaterEqual(columns * rows, count)
                diameter = min(width / columns, height / rows)
                # A near-square packing supplies a useful lower bound for primes.
                reference_columns = max(1, min(count, math.ceil(math.sqrt(count * width / height))))
                reference_rows = math.ceil(count / reference_columns)
                self.assertGreaterEqual(diameter, min(width / reference_columns, height / reference_rows))
        self.assertEqual(dashboard.choose_grid(7, 600, 600), (3, 3))
        self.assertEqual(dashboard.choose_grid(13, 600, 600), (4, 4))

    def test_regions(self):
        for width, height in ((1920, 720), (1920, 1080), (960, 360), (800, 600), (480, 800), (320, 480)):
            tabs, cpu, metrics = dashboard.dashboard_regions(width, height)
            for x, y, w, h in (cpu, metrics):
                self.assertGreater(w, 0)
                self.assertGreater(h, 0)
                self.assertGreaterEqual(y, tabs)
                self.assertLessEqual(x + w, width + 1e-8)
                self.assertLessEqual(y + h, height + 1e-8)
            self.assertAlmostEqual(cpu[2] * cpu[3] + metrics[2] * metrics[3], width * (height - tabs))
            self.assertTrue(cpu[0] + cpu[2] <= metrics[0] or cpu[1] + cpu[3] <= metrics[1])

    def test_capacity_units_and_availability(self):
        self.assertEqual(dashboard.format_memory_capacity(0.5, 2e12), "1.00 / 2.00 TB")
        self.assertEqual(dashboard.format_bit_rate(40e9), "40.0 Gbps")
        self.assertEqual(dashboard.format_disk_rate(12e9), "12.0 GB/s")
        payload = {"version": 1, "cpu": [0.0], "memory": {}, "disk": {}, "network": {}}
        # Older agents retain their reported values until upgraded.
        self.assertTrue(dashboard.normalize_remote_snapshot(payload)["disk_available"])
        payload["disk"]["available"] = False
        self.assertFalse(dashboard.normalize_remote_snapshot(payload)["disk_available"])
        payload["disk"]["available"] = "false"
        with self.assertRaises(ValueError):
            dashboard.normalize_remote_snapshot(payload)
        with patch.object(agent, "root_block_device", return_value=None), patch.object(agent, "read_disk_counters", return_value=None):
            self.assertFalse(agent.DiskSampler().sample()["available"])
        with patch.object(dashboard, "root_block_device", return_value=None), patch.object(dashboard, "read_disk_counters", return_value=None):
            sampler = dashboard.SystemUsageSampler()
            sampler.sample()
            self.assertIsNone(sampler.previous_disk)

    def test_render_matrix(self):
        preview_dir = os.environ.get('DASHBOARD_PREVIEW_DIR')
        window = SimpleNamespace(
            settings=SimpleNamespace(circle_scale=0.75),
            local_hostname='raspberrypi', local_source_key='local',
            active_source_key='remote-1',
            remote_sources=[(f'remote-{i}', f'long-linux-hostname-{i}-example') for i in range(4)],
            displayed_memory_usage=0.65, memory_total_bytes=2e12,
            displayed_swap_usage=0.0, swap_total_bytes=0,
            displayed_disk_usage=0.7, displayed_disk_read=12e9,
            displayed_disk_write=1e9, displayed_filesystem_usage=0.42,
            filesystem_usage_available=True, disk_usage_available=False,
            disk_device_name='very-long-root-device-or-overlay-name',
            displayed_upload=10e9, displayed_download=40e9,
            effective_link_capacity=100e9, negotiated_link_capacity=None,
            tcp_connections=1234567, udp_sockets=None,
            network_interface_name='enp123s456f789-long-interface-name',
        )
        for name in ('draw_cpu_panel', 'draw_memory_panel', 'draw_disk_panel', 'draw_network_panel', 'draw_source_tabs', 'draw_usage_bar', 'on_canvas_click'):
            setattr(window, name, MethodType(getattr(dashboard.HeatmapWindow, name), window))
        for width, height, count in ((1920, 720, 7), (1920, 1080, 127), (960, 360, 4), (800, 600, 13), (480, 800, 64), (320, 480, 256), (1920, 720, 4096)):
            for available in (False, True):
                window.disk_usage_available = available
                window.displayed_loads = [(i % 11) / 10 for i in range(count)]
                widget = SimpleNamespace(get_allocated_width=lambda: width, get_allocated_height=lambda: height)
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
                dashboard.HeatmapWindow.on_draw(window, widget, cairo.Context(surface))
                for x, y, w, h, key in window.source_tab_hitboxes:
                    self.assertGreaterEqual(x, 0)
                    self.assertLessEqual(x + w, width)
                    self.assertLessEqual(y + h, dashboard.dashboard_regions(width, height)[0])
                    self.assertTrue(window.on_canvas_click(None, SimpleNamespace(x=x+w/2, y=y+h/2)))
                    self.assertEqual(window.selected_source_key, key)
                if preview_dir and not available:
                    Path(preview_dir).mkdir(parents=True, exist_ok=True)
                    surface.write_to_png(str(Path(preview_dir) / f'{width}x{height}-{count}cpus.png'))


if __name__ == '__main__':
    unittest.main()
