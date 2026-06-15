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

## The pipeline

| Role | Job | Status |
|------|-----|--------|
| **Responder** | Talks to issue authors in the comment thread, refines scope, protects the repo's interests, and finalizes a clean `issues.md` (or rejects). Also re-engages issues that were stuck on `needs-human` once a person replies. | ✅ |
| **Planner** | Turns `issues.md` into an ordered manifest of small tasks (`task.md` files). | ✅ |
| **Workers** | Execute one task at a time on the issue branch, verify, commit, and write summaries. | ✅ |
| **Reviewer** | Reviews the combined diff; either opens a PR for human approval or sends it back to the planner. | ✅ |
| **Rework** | Watches open PRs (`cheaphelp:in-review`) for new human review feedback and pushes fixup commits to address it, or no-ops until there's something new. | ✅ |

An orchestrator runs on a **systemd timer**. Each tick it polls every registered
repo, classifies each issue into a pipeline stage by its labels, and dispatches
the right agent:

```
(no pipeline label) + human spoke last  -> responder   refine scope -> issues.md, label :ready
:ready / :needs-replan                  -> planner     issues.md -> tasks,        label :planned
:planned, tasks pending                  -> worker      implement one task on the issue branch
:planned, all tasks done                 -> quality gate -> reviewer  open PR (label :in-review) or replan
:in-review                               -> rework      address new PR review feedback, or no-op
:needs-human, human replied              -> responder   re-engage a stuck issue
:needs-human, no new reply               -> (idle)      waiting on a person
:rejected                                 -> (idle)      responder declined; left alone
```

A daily USD spend cap (`daily_budget_usd` in `config.json`) guards every tick:
once the cap is hit, the orchestrator posts a comment and stops dispatching new
agent turns for the rest of the day (see [Cost tracking and budget](#cost-tracking-and-budget)).

### Quality gate

Before the reviewer can open a PR, the orchestrator runs two registry-configured
commands inside the work clone:

1. **`autofix`** (set with `--autofix`) runs first — e.g.
   `ruff check --fix . ; ruff format .`. Any changes it makes are committed
   automatically. This resolves trivial issues (formatting, import order,
   `--fix`-able lint) cheaply, so they never escalate to a re-plan.
2. **`checks`** (set with `--checks`) is the gate — e.g. `ruff check . && pytest`.
   A **failing gate never becomes a PR**: the remaining failures are written to
   the issue's `replan.md`, the issue is relabeled `needs-replan`, and the
   planner produces a minimal corrective plan.

Set them when registering: `cheaphelp repo add <slug> --autofix "…" --checks "…"`.
Leave either empty to disable that step. Together they are the deterministic
backstop so lint/test failures can't slip into a pull request even if an agent
misses them.

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
mode for the systemd timer (see [Background service](#background-service)).

## The workspace

`cheaphelp init` creates a private workspace (default `~/.cheaphelp`, override
with `CHEAPHELP_HOME` or `--home`):

```
~/.cheaphelp/
├── config.json            # models per role, label names, poll interval, budget
├── .env                   # GITHUB_TOKEN, OPENROUTER_API_KEY (chmod 600)
├── repos.json             # registered repositories
├── agents/                # editable agent prompts (responder.md, planner.md, …)
├── opencode/opencode.json # generated opencode config (provider + agents)
├── state/                 # per-issue state + finalized issues.md files
│   └── daily_spend.json   # running total of today's USD spend (budget guardrail)
├── clones/                # shallow clones of registered repos (agent context)
└── logs/                  # run-YYYY-MM-DD.log, see `cheaphelp logs`
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

If any role gets stuck (e.g. a worker produces no parseable result, or a push
is rejected), the issue is labeled `cheaphelp:needs-human` and left alone until
a person comments — at which point the responder picks it back up. You can also
force a retry without waiting for the next tick: `cheaphelp retry owner/name 123`.

## Cost tracking and budget

Every agent turn's token usage and cost is recorded per tick, per role, and per
issue (`<issue_dir>/cost.json`). `cheaphelp status --costs` shows the cumulative
cost per issue, and each tick's summary in `cheaphelp logs` includes a cost
breakdown line.

Set `daily_budget_usd` in `config.json` (default `0`, meaning unlimited) to cap
total spend per UTC day across all repos. As spend approaches the cap,
cheaphelp posts a warning comment on the active issue at `budget_warn_at`
(default `0.80`) and again at 95%; once the cap is reached it posts an
"exceeded" comment and stops dispatching new agent turns until the next day.

## Configuration

`~/.cheaphelp/config.json` (defaults shown). Two tiers: a cheap conversational
model for the responder, a stronger model for the engineering roles. `variants`
maps a role to an opencode `--variant` (provider reasoning effort, e.g. `max`);
`""` means the provider default. Use `cheaphelp config show|get|set` to inspect
or edit it without hand-editing JSON.

```json
{
  "models": {
    "responder": "openrouter/deepseek/deepseek-v4-flash",
    "planner":   "openrouter/minimax/minimax-m3",
    "worker":    "openrouter/deepseek/deepseek-v4-flash",
    "reviewer":  "openrouter/minimax/minimax-m3",
    "rework":    "openrouter/deepseek/deepseek-v4-flash"
  },
  "variants": { "responder": "max", "planner": "", "worker": "max", "reviewer": "", "rework": "max" },
  "sandbox": {
    "confine_to_workdir": true,
    "restrict_bash": true,
    "no_network_tools": true
  },
  "pr_reviewers": [],
  "poll_interval": "10m",
  "agent_timeout": 600,
  "daily_budget_usd": 0,
  "budget_warn_at": 0.80,
  "opencode_bin": "opencode"
}
```

`pr_reviewers` is a list of GitHub usernames to request as reviewers on PRs the
reviewer role opens (empty = no reviewer request). `agent_timeout` is the
per-agent subprocess timeout in seconds.

After editing models, prompts, variants, or sandbox settings, run
`cheaphelp agents sync` to regenerate the opencode config.

## Repo conventions for agents

If the registered repo has a `CHEAPHELP.md`, `AGENTS.md`, or `CONTRIBUTING.md`
at its root (checked in that order), its contents are included in every agent's
context — use it for commit-message conventions, branch naming, or anything
else you'd tell a human contributor.

## Sandboxing the agents

Agents run inside a disposable clone, and cheaphelp generates an opencode
`permission` policy per role (`sandbox` in `config.json`):

- **`confine_to_workdir`** → opencode's `external_directory: "deny"`, so the
  edit/read tools stay inside the working directory (the clone) and can't reach
  the rest of your machine (including `~/.cheaphelp/.env`).
- **`restrict_bash`** → a bash allow/deny policy. The read-only roles
  (responder, planner, reviewer) deny bash by default and allow only read-only
  probes (`ls`, `cat`, `grep`, `git status/log/diff`, …). The worker allows bash
  by default but denies dangerous/out-of-scope commands (`sudo`, `rm -rf /…`,
  `dd`, `git push`, `ssh`, pipe-to-shell, …). cheaphelp does its own `git push`,
  so agents never touch remotes.
- **`no_network_tools`** → disables `webfetch`/`websearch` for all agents.

> ⚠️ **These are guardrails, not a true sandbox.** Bash pattern-matching can be
> evaded (e.g. via a script interpreter), and `external_directory` governs
> opencode's file tools, not what a shell subprocess can touch. For strong
> isolation, run cheaphelp under a dedicated low-privilege user, inside a
> container, or under a sandbox like `bwrap`/`firejail`. Set any knob to `false`
> to loosen, then `cheaphelp agents sync`.

## Background service

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
| `gitutil.py` | shallow clones, commit, push, diff helpers |
| `lock.py` | per-repo file locks so concurrent ticks don't collide |
| `spend.py` | daily USD spend tracker for the budget guardrail |
| `conventions.py` | reads `CHEAPHELP.md`/`AGENTS.md`/`CONTRIBUTING.md` into agent context |
| `pr_state.py` | persists PR ↔ issue link state for the rework stage |
| `opencode.py` | generate `opencode.json`, run agents headlessly (with `--variant`), parse decisions |
| `templates/` | bundled agent prompts (responder, planner, worker, reviewer, rework) |
| `tasks.py` | task manifest + per-issue task state store |
| `responder.py` / `planner.py` / `worker.py` / `reviewer.py` / `rework.py` | per-role turn logic |
| `orchestrator.py` | one tick of the state machine (`tick()`, `classify()`, stage dispatch) |
| `cleanup.py` | prune build clones for closed issues / unregistered repos |
| `systemd.py` | user service + timer install (continuous-mode by default) |
| `commands.py` / `cli.py` | CLI (`init`, `repo`, `run`, `systemd`, `agents`, `config`, `doctor`, `status`, `clean`, `retry`, `logs`) |

### Testing without tokens or opencode

Set `CHEAPHELP_AGENT_MOCK=/path/to/decision.json` and the agent layer returns
that file's contents instead of invoking opencode — handy for exercising the
orchestrator offline.
