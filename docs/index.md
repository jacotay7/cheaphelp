---
title: Overview
hide:
- feedback
---

cheaphelp is an AI software-engineer for your GitHub repositories. It installs as
a background service on your machine, watches the repos you register, and runs a
team of narrow AI agents — powered by cheap [OpenRouter](https://openrouter.ai)
models through the [opencode](https://opencode.ai) terminal harness — to triage
issues, plan work, implement it, and open pull requests for human review.

An orchestrator runs on a configurable timer. Each tick it polls every registered
repository, classifies each open issue by its pipeline label, and dispatches the
right agent role. A daily USD spend cap guards against runaway costs.

## How it works

cheaphelp uses a label-driven pipeline to move issues from triage through to a
pull request:

. **Responder** engages issue authors in the comment thread, refines the scope,
  and finalizes a clean specification (`issues.md`) or rejects out-of-scope
  requests.
. **Planner** turns the specification into an ordered manifest of small,
  executable tasks.
. **Workers** implement one task at a time on an issue branch, verifying their
  work before committing.
. **Fixer** makes one attempt to repair the working tree when the quality gate
  fails, before the issue is sent back to the planner.
. **Reviewer** reviews the combined diff and either opens a pull request for
  human approval or sends the work back for replanning.
. **Rework** watches open PRs for new human review feedback and pushes fixup
  commits to address it.

If any role gets stuck, the issue is labelled `needs-human` and left alone until
a person responds. The full state machine is documented in the
[Pipeline](pipeline.md) page.

## Documentation

- [Getting started](getting-started.md) — requirements, install, quick start,
  and running modes
- [Pipeline](pipeline.md) — label-driven state machine, quality gate, and role
  descriptions
- [Configuration](configuration.md) — config.json fields, models, variants,
  budget knobs, and CLI config commands
- [Workspace](workspace.md) — directory layout, .env secrets, state storage,
  and agent prompt overrides
- [Sandboxing](sandboxing.md) — sandbox settings, opencode permissions, and
  security limitations
- [Cost tracking](cost-tracking.md) — daily budget, warnings, and per-issue
  cost.json
- [Background service](background-service.md) — systemd timer, continuous mode,
  and lingering
- [Dev guide](dev-guide.md) — testing, source layout, and contributing

For full API documentation see the [API reference](reference/api.md). The source
code is available on [GitHub](https://github.com/jacotay7/cheaphelp).