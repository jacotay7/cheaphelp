"""Install cheaphelp as a systemd *user* service driven by a timer.

We use user units (`systemctl --user`) so no root is required. The timer fires
`cheaphelp run` on an interval. Note that user timers only run while the
user has a session unless lingering is enabled (`loginctl enable-linger`).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SERVICE_NAME = "cheaphelp.service"
TIMER_NAME = "cheaphelp.timer"

_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)


def user_unit_dir() -> Path:
    """Directory for systemd user units, honouring XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def normalize_interval(interval: str) -> str:
    """Validate an interval like '10m' and return a systemd-friendly form.

    Accepts NUMBER + unit (s/m/h/d). Returns e.g. '10min', '2h', '30s'.
    """
    match = _INTERVAL_RE.match(interval)
    if not match:
        raise ValueError(f"Invalid interval {interval!r}; use forms like '30s', '10m', '2h'.")
    value, unit = match.group(1), match.group(2).lower()
    systemd_unit = {"s": "s", "m": "min", "h": "h", "d": "d"}[unit]
    return f"{value}{systemd_unit}"


@dataclass
class UnitFiles:
    """Generated unit file contents."""

    service: str
    timer: str


def _exec_start() -> str:
    """Command the service runs. Uses the current interpreter's `-m cheaphelp`."""
    return f"{sys.executable} -m cheaphelp run --once"


def render_units(*, home: Path | None, interval: str, description: str = "cheaphelp") -> UnitFiles:
    """Render the .service and .timer unit file contents."""
    on_active = normalize_interval(interval)
    env_line = f"Environment=CHEAPHELP_HOME={home}\n" if home else ""
    service = f"""[Unit]
Description={description} - AI software engineer tick
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
{env_line}ExecStart={_exec_start()}
"""
    timer = f"""[Unit]
Description={description} timer

[Timer]
OnBootSec=2min
OnUnitActiveSec={on_active}
Persistent=true

[Install]
WantedBy=timers.target
"""
    return UnitFiles(service=service, timer=timer)


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def install(*, home: Path | None, interval: str) -> list[str]:
    """Write unit files and enable/start the timer. Returns a log of actions."""
    units = render_units(home=home, interval=interval)
    unit_dir = user_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / SERVICE_NAME).write_text(units.service, encoding="utf-8")
    (unit_dir / TIMER_NAME).write_text(units.timer, encoding="utf-8")
    actions = [f"wrote {unit_dir / SERVICE_NAME}", f"wrote {unit_dir / TIMER_NAME}"]

    reload = _systemctl("daemon-reload")
    if reload.returncode != 0:
        actions.append(f"daemon-reload failed: {reload.stderr.strip()} (units still written)")
        return actions
    enable = _systemctl("enable", "--now", TIMER_NAME)
    if enable.returncode != 0:
        actions.append(f"enable failed: {enable.stderr.strip()}")
    else:
        actions.append(f"enabled and started {TIMER_NAME}")
    return actions


def uninstall() -> list[str]:
    """Stop/disable the timer and remove unit files."""
    actions: list[str] = []
    _systemctl("disable", "--now", TIMER_NAME)
    actions.append(f"disabled {TIMER_NAME}")
    unit_dir = user_unit_dir()
    for name in (TIMER_NAME, SERVICE_NAME):
        path = unit_dir / name
        if path.exists():
            path.unlink()
            actions.append(f"removed {path}")
    _systemctl("daemon-reload")
    return actions


def status() -> str:
    """Return human-readable timer status."""
    result = _systemctl("list-timers", TIMER_NAME, "--no-pager")
    if result.returncode != 0:
        return f"could not query timers: {result.stderr.strip()}"
    return result.stdout.strip() or "no cheaphelp timer found"
