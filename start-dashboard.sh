#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python=/usr/bin/python3

if [ "${1:-}" = "--self-test" ]; then
    exec "$python" "$script_dir/live_performance_dashboard.py" "$@"
fi

if [ "${1:-}" = "--swaybar" ]; then
    exec "$python" "$script_dir/sway_dashboard_bar.py"
fi

# A root-owned Sway session (common under WSLg) can launch directly on its
# native Wayland socket; otherwise use the desktop user's session.
if [ "$(id -u)" -eq 0 ]; then
    if [ -n "${SWAYSOCK:-}" ] && [ -S "$SWAYSOCK" ] \
        && [ -n "${WAYLAND_DISPLAY:-}" ] \
        && [ -S "${XDG_RUNTIME_DIR:-}/$WAYLAND_DISPLAY" ] \
        && [ "$(stat -c '%u' "$SWAYSOCK")" = 0 ]; then
        export GDK_BACKEND=wayland
        exec "$python" "$script_dir/live_performance_dashboard.py" "$@"
    fi

    desktop_user="${LIVE_PERFORMANCE_DASHBOARD_USER:-${SUDO_USER:-}}"
    if [ -z "$desktop_user" ] || [ "$desktop_user" = "root" ]; then
        for sway_socket in /run/user/[0-9]*/sway-ipc.*.sock; do
            [ -S "$sway_socket" ] || continue
            runtime_dir=$(dirname -- "$sway_socket")
            desktop_uid=$(basename -- "$runtime_dir")
            desktop_user=$(getent passwd "$desktop_uid" | cut -d: -f1)
            [ -n "$desktop_user" ] && break
        done
    fi
    if [ -z "$desktop_user" ] || [ "$desktop_user" = "root" ]; then
        echo "No active non-root Sway user found; set LIVE_PERFORMANCE_DASHBOARD_USER." >&2
        exit 1
    fi

    desktop_uid="$(id -u "$desktop_user")"
    desktop_home=$(getent passwd "$desktop_user" | cut -d: -f6)
    exec /usr/sbin/runuser -u "$desktop_user" -- \
        env DISPLAY="${DISPLAY:-:0}" \
        XAUTHORITY="${LIVE_PERFORMANCE_DASHBOARD_XAUTHORITY:-$desktop_home/.Xauthority}" \
        XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
        GDK_BACKEND=wayland,x11 \
        "$python" "$script_dir/live_performance_dashboard.py" "$@"
fi

desktop_user=$(id -un)
desktop_home=$(getent passwd "$desktop_user" | cut -d: -f6)
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$desktop_home/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export GDK_BACKEND="${GDK_BACKEND:-wayland,x11}"

exec "$python" "$script_dir/live_performance_dashboard.py" "$@"
