# Live Performance Dashboard

A fullscreen Linux system dashboard designed for a wide secondary display
under Sway. It combines a per-vCPU heatmap with live memory, swap, root-disk,
and default-route network measurements.

The interface is rendered with GTK 3 and Cairo. It runs as an X11 application
through Xwayland and uses Sway IPC to move itself to the selected output,
enforce landscape orientation, remove borders, and enter fullscreen.

## Screen layout

The left 57% is an adaptive grid of per-vCPU circles. The right 43% contains
three equal-height landscape rows:

```text
┌──────────────────────────────┬──────────────────────────────────┐
│                              │ MEMORY [usage]  %  used/total GB │
│                              │ SWAP   [usage]  %  used/total GB │
│       per-vCPU heatmap       ├──────────────────────────────────┤
│                              │ DISK I/O [busy] READ/WRITE MB/s  │
│                              ├──────────────────────────────────┤
│                              │ UPLINK / DOWNLINK / LINK SPEED   │
└──────────────────────────────┴──────────────────────────────────┘
```

Every live measurement uses the same black, blue, purple, crimson, and red
heat palette. Faint one-pixel separators define the regions, and unused
capacity in proportional bars uses a subtle neutral background.

## Features

- Discovers all logical CPUs dynamically and adapts the circle grid.
- Samples CPU and system counters every 50 ms and renders at 60 FPS.
- Applies time-based interpolation instead of abrupt visual jumps.
- Shows RAM and swap as proportional heat bars with used/total decimal GB.
- Shows root-disk busy time plus synchronized read/write MB/s.
- Selects the active IPv4 default-route interface automatically.
- Shows network uplink, downlink, interface name, and negotiated link speed.
- Prefers an active 1920×720 secondary Sway output but supports other sizes.
- Can run fullscreen under Sway or as a normal test window.
- Supports direct invocation by a desktop user or by root.
- Accepts versioned metric snapshots from a remote Linux agent over TCP.
- Falls back to local measurements automatically when remote data becomes stale.

## Data sources

| Measurement | Linux source |
|---|---|
| Per-vCPU load | `/proc/stat` counter deltas |
| RAM and swap | `/proc/meminfo` |
| Root device | `/proc/self/mountinfo` |
| Disk busy/read/write | `/proc/diskstats` |
| Default network interface | `/proc/net/route` |
| Network throughput | `/proc/net/dev` |
| Negotiated Ethernet speed | `/sys/class/net/<interface>/speed` |

CPU utilization comes from `/proc/stat`, not `/proc/cpuinfo`.
`/proc/cpuinfo` describes processor hardware but does not provide live load.

## Requirements

- Linux with procfs and sysfs mounted
- Python 3
- GTK 3 and PyGObject
- Pycairo
- Sway and Xwayland for automatic output placement

On Debian, Ubuntu, or Raspberry Pi OS:

```sh
sudo apt update
sudo apt install python3 python3-gi python3-cairo gir1.2-gtk-3.0 sway xwayland util-linux
```

The package names are also listed in `dependencies-debian.txt`.

## Run

From the repository directory:

```sh
./start-dashboard.sh
```

The launcher is location-independent. When invoked as root, it discovers the
active non-root Sway session and launches the GUI with that user's Xwayland
credentials. If more than one Sway user is active, select one explicitly:

```sh
sudo LIVE_PERFORMANCE_DASHBOARD_USER=lonezor ./start-dashboard.sh
```

Select a Sway output explicitly:

```sh
./start-dashboard.sh --output HDMI-A-2
```

Override default-route network detection:

```sh
./start-dashboard.sh --interface eth0
```

Run in a normal window on the current display:

```sh
./start-dashboard.sh --windowed
```

Press `Esc` or `q` to exit. Run `./start-dashboard.sh --help` for all tuning
options, including sampling rate, frame rate, smoothing, and circle size.

## Output selection

Without `--output`, the application waits until at least two active Sway
outputs are present. It prefers an active 1920×720 output, then a non-focused
secondary output. The selected output is changed to `transform normal` for
landscape orientation before the application enters fullscreen.

If only one output exists, the dashboard remains windowed and periodically
retries placement. `--windowed` disables automatic moving and fullscreen mode.

## Network and disk interpretation

Network counters include all traffic on the selected interface, including
local-network traffic; they are not guaranteed to represent Internet-only
traffic. Network heat color is normalized against the reported link speed.
When link speed is unavailable, color normalization uses a 1 Gbps fallback,
while the LINK SPEED value correctly reports `UNKNOWN`.

Disk busy percentage and MB/s are complementary. Busy time indicates how long
the root device has outstanding work, while throughput shows the data volume.
Modern multiqueue NVMe performance cannot be fully characterized by either
value alone.

## Remote measurements

By default the dashboard listens on TCP port `9177` on all local addresses.
Local measurements remain active until a valid remote snapshot arrives. Fresh
remote data replaces the complete dashboard source, including CPU count,
memory, swap, disk, and network measurements. If no snapshot arrives for two
seconds, the dashboard automatically returns to local measurements.

Clickable source tabs at the top-right identify and select the active machine.
The local tab is always present, and up to four fresh remote hostnames appear
beside it in a dedicated strip above the measurement rows. The selected tab is
highlighted; press `Tab` or `Shift+Tab` to cycle without a pointer. The network
footer shows the selected source's interface.
Automatic selection remains on one remote source until it disconnects rather
than alternating between concurrently reporting machines.
Listener options are:

```text
--listen-address ADDRESS   Default: 0.0.0.0
--listen-port PORT         Default: 9177
--remote-timeout SECONDS   Default: 2
--local-only               Disable remote reception
```

Test a remote machine interactively by copying this repository there and
running:

```sh
./remote_metrics_agent.py --dashboard DASHBOARD_IP:9177
```

The agent samples every 50 ms by default, reconnects automatically, detects
the default-route interface and root disk, and requires only Python 3. Override
its network interface or rate if needed:

```sh
./remote_metrics_agent.py \
  --dashboard DASHBOARD_IP:9177 \
  --interface eth0 \
  --interval-ms 50
```

### Install the remote systemd service

On the remote Linux machine, from this repository:

```sh
sudo install -m 755 remote_metrics_agent.py /usr/local/bin/live-performance-agent
sudo install -m 644 systemd/live-performance-agent.service \
  /etc/systemd/system/live-performance-agent.service
sudo install -m 644 systemd/live-performance-agent.default \
  /etc/default/live-performance-agent
sudo editor /etc/default/live-performance-agent
sudo systemctl daemon-reload
sudo systemctl enable --now live-performance-agent
```

Set `DASHBOARD_ENDPOINT` in `/etc/default/live-performance-agent` to the
dashboard computer's reachable address. Check operation with:

```sh
systemctl status live-performance-agent
journalctl -u live-performance-agent -f
```

If the dashboard host uses UFW, permit only the remote machine:

```sh
sudo ufw allow from REMOTE_IP to any port 9177 proto tcp
```

The protocol is newline-delimited JSON with a 64 KiB message limit and strict
field validation. It intentionally has no authentication or encryption, so it
must only be exposed on a trusted LAN or protected VPN. Do not expose port 9177
directly to the Internet.

## Validate

Run the non-GUI checks:

```sh
./check.sh
```

This validates both shell scripts, compiles the Python source, and runs the
built-in parser, formatting, heat-color, and layout assertions.

## Repository contents

```text
live_performance_dashboard.py  Dashboard application
start-dashboard.sh             Portable Xwayland/Sway launcher
remote_metrics_agent.py        Standard-library remote measurement agent
check.sh                       Non-GUI validation
dependencies-debian.txt        Debian-family runtime packages
systemd/                       Remote agent service and configuration template
README.md                      Setup, operation, and design notes
```
