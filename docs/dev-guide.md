---
title: Developer guide
---

This page covers development workflows, the source layout, and testing without
network access.

## Development

```bash
uv sync
uv run python -m pytest -q                              # tests
uv run ruff check --config config/ruff.toml src tests  # lint
```

### Source layout

All product code is under `src/cheaphelp/_internal/`:

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
| `templates/` | bundled agent prompts (responder, planner, worker, reviewer, rework, fixer) |
| `tasks.py` | task manifest + per-issue task state store |
| `responder.py` / `planner.py` / `worker.py` / `reviewer.py` / `rework.py` / `fixer.py` | per-role turn logic |
| `orchestrator.py` | one tick of the state machine (`tick()`, `classify()`, stage dispatch) |
| `cleanup.py` | prune build clones for closed issues / unregistered repos |
| `systemd.py` | user service + timer install (continuous-mode by default) |
| `commands.py` / `cli.py` | CLI (`init`, `repo`, `run`, `systemd`, `agents`, `config`, `doctor`, `status`, `clean`, `retry`, `logs`) |

Tests live in `tests/`, one file per `src/cheaphelp/_internal/` module (plus
`test_cli_*.py` per CLI subcommand and `test_api.py`); shared fixtures in
`tests/conftest.py`.

### Testing without tokens or opencode

Set `CHEAPHELP_AGENT_MOCK=/path/to/decision.json` and the agent layer returns
that file's contents instead of invoking opencode — handy for exercising the
orchestrator offline.