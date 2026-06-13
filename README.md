# cheaphelp

[![ci](https://github.com/jacobataylor7/cheaphelp/workflows/ci/badge.svg)](https://github.com/jacobataylor7/cheaphelp/actions?query=workflow%3Aci)
[![documentation](https://img.shields.io/badge/docs-zensical-FF9100.svg?style=flat)](https://jacobataylor7.github.io/cheaphelp/)

An AI software-engineer for your GitHub repositories. cheaphelp installs as a
background service on your machine, watches the repos you register, and runs a
team of narrow AI agents — powered by **cheap [OpenRouter](https://openrouter.ai)
models** through the **[opencode](https://opencode.ai)** terminal harness — to
triage issues, plan work, implement it, and open pull requests for human review.

> **Status: full pipeline wired and exercised live.** All four roles run
> end-to-end against a real repository; expect to keep tuning prompts and
> hardening edge cases.

## The pipeline

| Role | Job | Status |
|------|-----|--------|
| **Responder** | Talks to issue authors in the comment thread, refines scope, protects the repo's interests, and finalizes a clean `issues.md` (or rejects). | ✅ |
| **Planner** | Turns `issues.md` into an ordered manifest of small tasks (`task.md` files). | ✅ |
| **Workers** | Execute one task at a time on the issue branch, verify, commit, and write summaries. | ✅ |
| **Reviewer** | Reviews the combined diff; either opens a PR for human approval or sends it back to the planner. | ✅ |

An orchestrator runs on a **systemd timer**. Each tick it polls every registered
repo, classifies each issue into a pipeline stage by its labels, and dispatches
the right agent:

```
(no label) + human spoke last     -> responder   refine scope -> issues.md, label ready
cheaphelp:ready / :needs-replan    -> planner     issues.md -> tasks,        label planned
cheaphelp:planned, tasks pending   -> worker      implement a task on the issue branch
cheaphelp:planned, all tasks done  -> reviewer    open PR (label in-review) or replan
```

## Why opencode + OpenRouter

Each role is an opencode **agent** defined in a single generated `opencode.json`,
with its own model, system prompt and tool permissions (e.g. the responder is
read-only; workers may edit). Models are chosen per role and are **cheap by
default for testing** — swap them for frontier models in one config file once the
pipeline behaves.

## Requirements

- Python ≥ 3.10 (developed on 3.13)
- [`uv`](https://docs.astral.sh/uv/)
- [opencode](https://opencode.ai): `curl -fsSL https://opencode.ai/install | bash` (or `npm i -g opencode-ai`)
- A GitHub personal access token (repo + issues scope) and an OpenRouter API key

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
uv run cheaphelp run --once --dry-run

# 6. Run it for real (responder engages open issues).
uv run cheaphelp run --once

# 7. Install the background timer (default every 10 minutes).
uv run cheaphelp systemd install --interval 10m
uv run cheaphelp systemd status
```

For the timer to keep running while you are logged out:

```bash
loginctl enable-linger "$USER"
```

## The workspace

`cheaphelp init` creates a private workspace (default `~/.cheaphelp`, override
with `CHEAPHELP_HOME` or `--home`):

```
~/.cheaphelp/
├── config.json            # models per role, label names, poll interval
├── .env                   # GITHUB_TOKEN, OPENROUTER_API_KEY (chmod 600)
├── repos.json             # registered repositories
├── agents/                # editable agent prompts (responder.md, planner.md, …)
├── opencode/opencode.json # generated opencode config (provider + agents)
├── state/                 # per-issue state + finalized issues.md files
├── clones/                # shallow clones of registered repos (agent context)
└── logs/
```

Edit the prompts in `agents/` or models in `config.json`, then regenerate the
opencode config with `cheaphelp agents sync`.

## How the responder works

Each tick, for every enabled repo, cheaphelp lists open issues and finds those
"waiting on a turn" (a freshly opened issue, or one where a human replied after
the bot). It clones the repo so the agent can read the real code, hands the
agent the full issue thread, and the agent returns a JSON decision:

- **comment** — ask focused questions / raise concerns (posts a reply).
- **finalize** — scope is solid: writes `issues.md` and labels the issue
  `cheaphelp:ready` (the planner's input).
- **reject** — out of scope/duplicate/harmful: explains why and labels
  `cheaphelp:rejected`.

The bot recognizes its own comments via a hidden marker, so it never talks over
itself and only re-engages when a human responds.

## Configuration

`~/.cheaphelp/config.json` (defaults shown). Two tiers: a cheap conversational
model for the responder, a stronger model for the engineering roles. `variants`
maps a role to an opencode `--variant` (provider reasoning effort, e.g. `max`);
`""` means the provider default.

```json
{
  "models": {
    "responder": "openrouter/deepseek/deepseek-v4-flash",
    "planner":   "openrouter/minimax/minimax-m3",
    "worker":    "openrouter/minimax/minimax-m3",
    "reviewer":  "openrouter/minimax/minimax-m3"
  },
  "variants": { "responder": "max", "planner": "", "worker": "", "reviewer": "" },
  "poll_interval": "10m",
  "opencode_bin": "opencode"
}
```

After editing models, prompts, or variants, run `cheaphelp agents sync` to
regenerate the opencode config.

## Development

```bash
uv sync
uv run python -m pytest -q                              # tests
uv run ruff check --config config/ruff.toml src tests  # lint
```

Source lives in `src/cheaphelp/_internal/`:

| Module | Responsibility |
|--------|----------------|
| `config.py` | workspace layout + `config.json` |
| `env.py` | `.env` secrets (parse/write, chmod 600) |
| `github.py` | minimal GitHub REST client (httpx) |
| `registry.py` | registered-repo store |
| `gitutil.py` | shallow clones of registered repos |
| `opencode.py` | generate `opencode.json`, run agents headlessly (with `--variant`), parse decisions |
| `templates/` | bundled agent prompts (responder, planner, worker, reviewer) |
| `tasks.py` | task manifest + per-issue task state store |
| `responder.py` / `planner.py` / `worker.py` / `reviewer.py` | per-role turn logic |
| `orchestrator.py` | one tick of the state machine (stage dispatch) |
| `systemd.py` | user service + timer install |
| `commands.py` / `cli.py` | CLI |

### Testing without tokens or opencode

Set `CHEAPHELP_AGENT_MOCK=/path/to/decision.json` and the agent layer returns
that file's contents instead of invoking opencode — handy for exercising the
orchestrator offline.
