#!/usr/bin/env python3
"""Swaybar status command and click handler for the dashboard."""

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


APP_ID = "live-performance-dashboard"
WINDOW_CLASS = "LivePerformanceDashboard"


def sway(*arguments: str) -> str:
    try:
        return subprocess.run(
            ["swaymsg", *arguments], check=True, capture_output=True,
            text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def dashboard_pid(node: object) -> Optional[int]:
    if not isinstance(node, dict):
        return None
    properties = node.get("window_properties")
    if (
        node.get("app_id") == APP_ID
        or isinstance(properties, dict) and properties.get("class") == WINDOW_CLASS
    ):
        try:
            return int(node["pid"])
        except (KeyError, TypeError, ValueError):
            return None
    for children in (node.get("nodes", []), node.get("floating_nodes", [])):
        for child in children:
            pid = dashboard_pid(child)
            if pid is not None:
                return pid
    return None


def find_dashboard_pid() -> Optional[int]:
    try:
        return dashboard_pid(json.loads(sway("-t", "get_tree", "-r")))
    except json.JSONDecodeError:
        return None


def dashboard_running() -> bool:
    return find_dashboard_pid() is not None


def toggle_dashboard() -> None:
    pid = find_dashboard_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return
    subprocess.Popen(
        [str(Path(__file__).with_name("start-dashboard.sh"))],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def status_line() -> str:
    running = dashboard_running()
    state = "ON" if running else "OFF"
    color = "#ffffff" if running else "#d0d0d0"
    blocks = [
        {
            "name": "dashboard",
            "full_text": f"  Dashboard {state}  ",
            "color": color,
            "background": "#285577" if running else "#4a4a4a",
            "border": "#6f9fc8" if running else "#888888",
            "border_width": 1,
            "min_width": "  Dashboard OFF  ",
            "separator": False,
        },
        {"name": "clock", "full_text": time.strftime("%Y-%m-%d %X")},
    ]
    return json.dumps(blocks)


def parse_click_event(line: str) -> Optional[dict]:
    line = line.strip().lstrip(",")
    if not line or line == "[":
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def main() -> None:
    if "--self-test" in sys.argv[1:]:
        assert dashboard_pid({"app_id": APP_ID, "pid": 123}) == 123
        assert dashboard_pid({"floating_nodes": [{"window_properties": {"class": WINDOW_CLASS}, "pid": 456}]}) == 456
        assert json.loads(status_line())[0]["name"] == "dashboard"
        assert parse_click_event(',{"name":"dashboard","button":1}')["button"] == 1
        return

    print('{"version":1,"click_events":true}')
    print("[")
    while True:
        print(status_line() + ",", flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], 1.0)
        if not ready:
            continue
        line = sys.stdin.readline()
        if not line:
            return
        event = parse_click_event(line)
        if event and event.get("name") == "dashboard" and event.get("button") == 1:
            toggle_dashboard()


if __name__ == "__main__":
    main()
