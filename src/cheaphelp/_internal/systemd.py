"""Install cheaphelp as a systemd *user* service driven by a timer.

We use user units (`systemctl --user`) so no root is required. The timer fires
`cheaphelp run` on an interval. Note that user timers only run while the
user has a session unless lingering is enabled (`loginctl enable-linger`).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
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


@dataclass
class Health:
    """Result of a systemd health check for the cheaphelp timer/service.

    `available` is True when *systemctl* was found on PATH. When it is False,
    the other fields carry their default (conservative) values so callers can
    always pattern-match on the dataclass without checking ``available`` first.
    """

    available: bool
    installed: bool
    enabled: bool
    active: bool
    last_exit_code: int | None


def _exec_start(*, continuous: bool, max_ticks: int, sleep: float) -> str:
    """Command the service runs via the globally-installed ``cheaphelp`` entry point.

    Set up via ``uv tool install --from . cheaphelp``.

    In continuous mode, each timer firing drains the backlog (repeated ticks
    until one produces no agent turns, capped at *max_ticks*) instead of doing
    a single tick, so queued work doesn't have to wait for the next firing.
    """
    cmd = "cheaphelp run"
    if continuous:
        cmd += f" --continuous --max-ticks {max_ticks} --sleep {sleep:g}"
    return cmd


def render_units(
    *,
    home: Path | None,
    interval: str,
    description: str = "cheaphelp",
    continuous: bool = True,
    max_ticks: int = 20,
    sleep: float = 30.0,
) -> UnitFiles:
    """Render the .service and .timer unit file contents."""
    on_active = normalize_interval(interval)
    env_line = f"Environment=CHEAPHELP_HOME={home}\n" if home else ""
    service = f"""[Unit]
Description={description} - AI software engineer tick
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
{env_line}ExecStart={_exec_start(continuous=continuous, max_ticks=max_ticks, sleep=sleep)}
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


def install(
    *,
    home: Path | None,
    interval: str,
    continuous: bool = True,
    max_ticks: int = 20,
    sleep: float = 30.0,
) -> list[str]:
    """Write unit files and enable/start the timer. Returns a log of actions."""
    units = render_units(home=home, interval=interval, continuous=continuous, max_ticks=max_ticks, sleep=sleep)
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


def check_health() -> Health:
    """Check whether the cheaphelp systemd timer/service is healthy.

    Returns a :class:`Health` dataclass.  When ``systemctl`` is not on PATH,
    returns ``Health(available=False, …)`` with all other fields at their
    conservative default (False / None) so callers can always destructure the
    result safely.

    The function is safe to call from tests — any ``OSError`` from the
    underlying subprocess calls is caught and results in the same conservative
    fallback.
    """
    if shutil.which("systemctl") is None:
        return Health(
            available=False,
            installed=False,
            enabled=False,
            active=False,
            last_exit_code=None,
        )

    try:
        # is-enabled — determines installed + enabled
        ie = _systemctl("is-enabled", TIMER_NAME)
        no_such_file = "no such file" in ie.stderr.lower()
        installed = ie.returncode == 0 and not no_such_file
        enabled = ie.returncode == 0 and ie.stdout.strip() == "enabled"

        # is-active
        ia = _systemctl("is-active", TIMER_NAME)
        active = ia.returncode == 0 and ia.stdout.strip() == "active"

        # ExecMainStatus of the .service
        es = _systemctl("show", SERVICE_NAME, "-p", "ExecMainStatus", "--value")
        last_exit_code: int | None = None
        if es.returncode == 0:
            raw = es.stdout.strip()
            try:
                last_exit_code = int(raw)
            except (ValueError, TypeError):
                last_exit_code = None

        return Health(
            available=True,
            installed=installed,
            enabled=enabled,
            active=active,
            last_exit_code=last_exit_code,
        )
    except OSError:
        return Health(
            available=True,
            installed=False,
            enabled=False,
            active=False,
            last_exit_code=None,
        )
