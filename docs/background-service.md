---
title: Background service
---

`cheaphelp systemd install` writes a user-level `cheaphelp.service` +
`cheaphelp.timer` (`systemctl --user`, no root needed) and starts the timer:

```bash
cheaphelp systemd install --interval 10m   # default: every 10 minutes
cheaphelp systemd status                   # show the timer schedule
cheaphelp systemd uninstall                # stop and remove both units
```

By default the timer's `ExecStart` is `cheaphelp run --continuous --max-ticks
20 --sleep 30`: each time the timer fires it keeps ticking — sleeping 30s
between ticks — until a tick produces no agent turns (everything is idle) or
20 ticks pass, then exits. This drains a backlog of issues quickly after it
shows up, instead of trickling out one tick per timer interval. Tune it at
install time:

```bash
cheaphelp systemd install --interval 10m --max-ticks 40 --sleep 15
cheaphelp systemd install --interval 5m --no-continuous   # one tick per firing
```

`Type=oneshot` means systemd won't start an overlapping run if one is still
draining the backlog when the timer next fires. For the timer to keep running
while you're logged out:

```bash
loginctl enable-linger "$USER"
```

See [Pipeline](pipeline.md) for what the timer runs on each tick.