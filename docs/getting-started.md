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
#    runs `cheaphelp run --continuous` by default, see "Background service").
cheaphelp systemd install --interval 10m
cheaphelp systemd status
```

> **Upgrading.** After pulling new code, re-run `uv tool install --from . --reinstall cheaphelp` to refresh the global command. A plain `git pull` updates the source tree but does NOT refresh the installed binary, and `uv run cheaphelp` would then diverge from your checkout.

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