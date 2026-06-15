---
title: Workspace
---

`cheaphelp init` creates a private workspace that stores all configuration,
state, and runtime data.

## Directory layout

The workspace defaults to `~/.cheaphelp`. Override it with the `CHEAPHELP_HOME`
environment variable or the `--home` CLI flag.

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

See [Pipeline](pipeline.md) for the full role description.