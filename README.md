# cheaphelp

[![ci](https://github.com/jacotay7/cheaphelp/actions/workflows/ci.yml/badge.svg)](https://github.com/jacotay7/cheaphelp/actions/workflows/ci.yml)
[![documentation](https://img.shields.io/badge/docs-zensical-FF9100.svg?style=flat)](https://jacotay7.github.io/cheaphelp/)

An AI software-engineer for your GitHub repositories. cheaphelp installs as a
background service on your machine, watches the repos you register, and runs a
team of narrow AI agents — powered by **cheap [OpenRouter](https://openrouter.ai)
models** through the **[opencode](https://opencode.ai)** terminal harness — to
triage issues, plan work, implement it, and open pull requests for human review.

> **Status: full pipeline wired and exercised live.** All five roles run
> end-to-end against a real repository; expect to keep tuning prompts and
> hardening edge cases.

## Quick start

```bash
# 1. Install (from a clone, for now)
uv sync

# 2. Create your machine-local workspace (~/.cheaphelp) and store secrets.
#    Prompts for your tokens, or pass them as flags / set them later.
uv run cheaphelp init

# 3. Check everything is wired up.
uv run cheaphelp doctor

# 4. Register a repo to work on.
uv run cheaphelp repo add owner/name
uv run cheaphelp repo list

# 5. Dry-run one tick (shows what it *would* do, no changes).
uv run cheaphelp run --dry-run

# 6. Run it for real (responder engages open issues).
uv run cheaphelp run

# 7. Or drain the whole backlog now: repeat ticks until one is idle.
uv run cheaphelp run --continuous

# 8. Install the background timer (default every 10 minutes; each firing
#    runs `cheaphelp run --continuous` by default, see the docs).
uv run cheaphelp systemd install --interval 10m
uv run cheaphelp systemd status
```

## Documentation

- [Getting started](docs/getting-started.md) — install, requirements, running modes
- [Pipeline](docs/pipeline.md) — how the agents collaborate and the quality gate
- [Configuration](docs/configuration.md) — `config.json` fields, models, budget knobs
- [Workspace](docs/workspace.md) — files cheaphelp writes to disk
- [Sandboxing](docs/sandboxing.md) — how agents are isolated and its limits
- [Cost tracking](docs/cost-tracking.md) — daily budget and per-issue cost reporting
- [Background service](docs/background-service.md) — running under the systemd timer
- [Dev guide](docs/dev-guide.md) — for contributors (testing, source layout, mocking opencode)

## For contributors

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Dev guide](docs/dev-guide.md).
