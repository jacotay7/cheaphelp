---
title: Configuration
---

cheaphelp is configured through `~/.cheaphelp/config.json`. The defaults are
shown below. Two tiers: a cheap conversational model for the responder, a
stronger model for the engineering roles. `variants` maps a role to an opencode
`--variant` (provider reasoning effort, e.g. `max`); `""` means the provider
default. Use `cheaphelp config show|get|set` to inspect or edit it without
hand-editing JSON.

```json
{
  "models": {
    "responder": "openrouter/deepseek/deepseek-v4-flash",
    "planner":   "openrouter/minimax/minimax-m3",
    "worker":    "openrouter/deepseek/deepseek-v4-flash",
    "reviewer":  "openrouter/minimax/minimax-m3",
    "rework":    "openrouter/deepseek/deepseek-v4-flash",
    "fixer":     "openrouter/deepseek/deepseek-v4-flash"
  },
  "variants": { "responder": "max", "planner": "", "worker": "max", "reviewer": "", "rework": "max", "fixer": "max" },
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
  "quality_gate_fix_attempts": 1,
  "opencode_bin": "opencode"
}
```

- **`pr_reviewers`** is a list of GitHub usernames to request as reviewers on
  PRs the reviewer role opens (empty = no reviewer request).
- **`agent_timeout`** is the per-agent subprocess timeout in seconds.
- **`sandbox`** controls agent permissions; see [Sandboxing](sandboxing.md).
- **`daily_budget_usd`** and **`budget_warn_at`** govern daily spend; see
  [Cost tracking](cost-tracking.md).
- **`quality_gate_fix_attempts`** is how many times the **fixer** role attempts
  to repair a failing quality gate (re-running the gate after each attempt)
  before the issue is sent back to the planner; `0` skips the fixer entirely.
  See [Pipeline](pipeline.md#quality-gate).

After editing models, prompts, variants, or sandbox settings, run
`cheaphelp agents sync` to regenerate the opencode config.