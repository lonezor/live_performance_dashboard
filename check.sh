#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cache_dir=$(mktemp -d)
trap 'rm -rf "$cache_dir"' EXIT

sh -n "$script_dir/start-dashboard.sh"
sh -n "$script_dir/check.sh"
PYTHONPYCACHEPREFIX="$cache_dir" /usr/bin/python3 -m py_compile \
    "$script_dir/live_performance_dashboard.py" \
    "$script_dir/remote_metrics_agent.py"
env -u DISPLAY -u XAUTHORITY \
    /usr/bin/python3 "$script_dir/live_performance_dashboard.py" --self-test
/usr/bin/python3 "$script_dir/remote_metrics_agent.py" --self-test

env -u DISPLAY -u XAUTHORITY /usr/bin/python3 "$script_dir/test_layout.py"
