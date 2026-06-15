# cheaphelp

[![ci](https://github.com/jacotay7/cheaphelp/actions/workflows/ci.yml/badge.svg)](https://github.com/jacotay7/cheaphelp/actions/workflows/ci.yml)
[![documentation](https://img.shields.io/badge/docs-zensical-FF9100.svg?style=flat)](https://jacotay7.github.io/cheaphelp/)

An AI software-engineer for your GitHub repositories. cheaphelp installs as a
background service on your machine, watches the repos you register, and runs a
team of narrow AI agents — powered by **cheap [OpenRouter](https://openrouter.ai)
models** through the **[opencode](https://opencode.ai)** terminal harness — to
triage issues, plan work, implement it, and open pull requests for human review.

> **Status: full pipeline wired and exercised live.** All roles run end-to-end
> against a real repository; expect to keep tuning prompts and hardening edge
> cases.

## Install

The recommended way to install the `cheaphelp` CLI on your machine:

```bash
pipx install cheaphelp
# or, if you don't use pipx:
pip install --user cheaphelp
```

For development (running from a clone, contributing, or testing unreleased
changes), use `uv sync` inside the clone and invoke the CLI as
`uv run cheaphelp …` — see [Quick start](#quick-start) step 1.

## Quick start

```bash
# 1. (Development install — end users should `pipx install cheaphelp`, see
#    the Install section above.) Clone, sync deps, and install the
#    `cheaphelp` command globally.
git clone https://github.com/jacotay7/cheaphelp.git
cd cheaphelp
uv sync                                          # install deps (needed before tool install)
uv tool install --from . cheaphelp              # puts `cheaphelp` on $PATH

# 2. Create your machine-local workspace (~/.cheaphelp) and store secrets.
#    Prompts for your tokens, or pass them as flags / set them later.
cheaphelp init

# 3. Check everything is wired up.
cheaphelp doctor

# 4. Register a repo to work on.
cheaphelp repo add owner/name
cheaphelp repo list

# 5. Dry-run one tick (shows what it *would* do, no changes).
cheaphelp run --dry-run

# 6. Run it for real (responder engages open issues).
cheaphelp run

# 7. Or drain the whole backlog now: repeat ticks until one is idle.
cheaphelp run --continuous

# 8. Install the background timer (default every 10 minutes; each firing
#    runs `cheaphelp run --continuous` by default, see the docs).
cheaphelp systemd install --interval 10m
cheaphelp systemd status
```

> **Upgrading.** After pulling new code, re-run `uv tool install --from . --reinstall cheaphelp` to refresh the global command. A plain `git pull` updates the source tree but does NOT refresh the installed binary, and `uv run cheaphelp` would then diverge from your checkout.

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
