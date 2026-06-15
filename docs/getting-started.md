---
title: Getting started
---

This page covers the requirements, installation, and quick-start steps to get
cheaphelp running on your machine.

## Requirements

- Python ≥ 3.10 (developed on 3.13)
- [`uv`](https://docs.astral.sh/uv/)
- [opencode](https://opencode.ai): `curl -fsSL https://opencode.ai/install | bash` (or `npm i -g opencode-ai`)
- A GitHub personal access token (repo + workflow + issues scope) and an OpenRouter API key
  — the `workflow` scope is required so cheaphelp can push branches that touch
  `.github/workflows/`; without it those pushes are rejected and the PR never opens

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
#    runs `cheaphelp run --continuous` by default, see "Background service").
uv run cheaphelp systemd install --interval 10m
uv run cheaphelp systemd status
```

### Running modes

```bash
cheaphelp run                       # one tick across all enabled repos
cheaphelp run -n 5 --sleep 60       # 5 ticks, sleeping 60s between each
cheaphelp run --continuous          # repeat ticks until one is idle (no agent turns),
                                     # capped at --max-ticks (default 20)
cheaphelp run --dry-run             # report planned actions, no API/filesystem changes
                                     # (combine with any of the above)
```

`--continuous` is useful both for clearing a backlog by hand and as the default
mode for the systemd timer (see [Background service](background-service.md)).

---

Next: see [Pipeline](pipeline.md) to understand what runs.