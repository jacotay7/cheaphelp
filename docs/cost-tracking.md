---
title: Cost tracking
---

Every agent turn's token usage and cost is recorded per tick, per role, and per
issue (`<issue_dir>/cost.json`). `cheaphelp status --costs` shows the cumulative
cost per issue, and each tick's summary in `cheaphelp logs` includes a cost
breakdown line.

Set `daily_budget_usd` in `config.json` (default `0`, meaning unlimited) to cap
total spend per UTC day across all repos. As spend approaches the cap,
cheaphelp posts a warning comment on the active issue at `budget_warn_at`
(default `0.80`) and again at 95%; once the cap is reached it posts an
"exceeded" comment and stops dispatching new agent turns until the next day.

See [Configuration](configuration.md) for the `daily_budget_usd` and
`budget_warn_at` JSON keys.