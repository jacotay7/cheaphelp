---
title: Sandboxing
---

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

See [Configuration](configuration.md) for the `sandbox` block fields.