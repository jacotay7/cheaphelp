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
| **Fixer** | When the quality gate fails, makes one attempt to repair the working tree from the gate output (failing tests, lint, types) so it never escalates to a full re-plan. | ✅ |
| **Reviewer** | Reviews the combined diff; either opens a PR for human approval or sends it back to the planner. | ✅ |
| **Rework** | Watches open PRs (`cheaphelp:in-review`) for new human review feedback and pushes fixup commits to address it, or no-ops until there's something new. | ✅ |

## Label flow

```
(no pipeline label) + human spoke last  -> responder   refine scope -> issues.md, label :ready
:ready / :needs-replan                  -> planner     issues.md -> tasks,        label :planned
:planned, tasks pending                  -> worker      implement one task on the issue branch
:planned, all tasks done                 -> quality gate -> reviewer  open PR (label :in-review) or replan
                                            (gate fails -> fixer repairs + re-runs gate before replanning)
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
   A **failing gate never becomes a PR**. On failure the **fixer** role gets one
   (configurable) attempt to repair the working tree from the gate output, after
   which the gate is re-run. Only if it *still* fails are the remaining failures
   written to the issue's `replan.md`, the issue relabeled `needs-replan`, and the
   planner asked for a minimal corrective plan. Set
   `quality_gate_fix_attempts` to `0` in `config.json` to skip the fixer and fail
   straight to a re-plan.

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