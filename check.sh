#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cache_dir=$(mktemp -d)
trap 'rm -rf "$cache_dir"' EXIT

sh -n "$script_dir/start-dashboard.sh"
PYTHONPYCACHEPREFIX="$cache_dir" /usr/bin/python3 -m py_compile \
    "$script_dir/live_performance_dashboard.py"
/usr/bin/python3 "$script_dir/live_performance_dashboard.py" --self-test
