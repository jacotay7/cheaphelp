---
title: Pipeline
---

The cheaphelp pipeline is a label-driven state machine. An orchestrator runs on
a **systemd timer**. Each tick it polls every registered repo, classifies each
issue into a pipeline stage by its labels, and dispatches the right agent.

## Roles

| Role | Job | Status |
|------|-----|--------|
| **Responder** | Talks to issue authors in the comment thread, refines scope, protects the repo's interests, and finalizes a clean `issues.md` (or rejects). Also re-engages issues that were stuck on `needs-human` once a person replies. | ✅ |
| **Planner** | Turns `issues.md` into an ordered manifest of small tasks (`task.md` files). | ✅ |
| **Workers** | Execute one task at a time on the issue branch, verify, commit, and write summaries. | ✅ |
| **Reviewer** | Reviews the combined diff; either opens a PR for human approval or sends it back to the planner. | ✅ |
| **Rework** | Watches open PRs (`cheaphelp:in-review`) for new human review feedback and pushes fixup commits to address it, or no-ops until there's something new. | ✅ |

## Label flow

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
agent turns for the rest of the day (see [Cost tracking](cost-tracking.md)).

## Quality gate

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