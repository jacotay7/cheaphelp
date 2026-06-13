# CLAUDE.md

Guidance for AI agents working in this repository.

## What this is

`cheaphelp` is an AI software-engineer for GitHub repos. It installs as a
background service (systemd timer), watches registered repos, and runs a team of
narrow agents — via the **opencode** harness against cheap **OpenRouter** models
— to triage issues, plan, implement, and open PRs for human review.

The pipeline is a label-driven state machine. Each tick polls open issues and
dispatches by label:

```
(no pipeline label) + human spoke last  -> responder  refine scope -> issues.md, label :ready
:ready / :needs-replan                  -> planner    issues.md -> tasks,      label :planned
:planned, tasks pending                 -> worker     implement one task on the issue branch
:planned, all tasks done                -> quality gate -> reviewer  open PR or replan
```

## Workflow (IMPORTANT: branch protection)

`main` is protected. **Never commit or push to `main` directly.** All changes
land through a pull request:

1. Branch off `main`: `git switch -c <type>-<short-name>` (e.g. `feat-json-output`).
2. Make the change with tests.
3. `make format && make check && make test` (all must pass).
4. Commit (see convention below), push the branch, open a PR.

## Dev commands

```bash
make setup     # install deps (uv sync)
make format    # auto-format (ruff format + ruff --fix)
make check     # lint + types + docs + api checks
make test      # pytest
make run cheaphelp [ARGS...]   # run the CLI
```

Direct equivalents (no direnv):

```bash
uv run ruff check src tests duties.py scripts --config config/ruff.toml
uv run ruff format src tests duties.py scripts --config config/ruff.toml
uv run python -m pytest -q
uv run cheaphelp <subcommand>
```

Lint is `ruff` with `select = ["ALL"]` (config in `config/ruff.toml`); it is
strict (e.g. COM812 trailing commas, ANN annotations, D docstrings). Run
`make format` before `make check`. Python target is 3.10+.

## Layout

All product code is under `src/cheaphelp/_internal/`:

| Module | Responsibility |
|--------|----------------|
| `config.py` | `Workspace` paths + `config.json` (`Config`) |
| `env.py` | `.env` secret parsing/writing (chmod 600) |
| `github.py` | minimal GitHub REST client (httpx) |
| `registry.py` | registered-repo store (`repos.json`) |
| `gitutil.py` | clones, commit, push, diff helpers |
| `opencode.py` | generate `opencode.json`, run agents headlessly, parse decisions |
| `templates/*.md` | bundled agent prompts (responder/planner/worker/reviewer) |
| `tasks.py` | task manifest + per-issue task-state store |
| `responder.py` / `planner.py` / `worker.py` / `reviewer.py` | per-role turn logic |
| `orchestrator.py` | one tick of the state machine (`tick()`, `classify()`, stage dispatch) |
| `systemd.py` | user service + timer install |
| `commands.py` / `cli.py` | argparse CLI (`cmd_*` per subcommand) |

Tests in `tests/` (`test_cli.py`, `test_api.py`, `test_core.py`); shared
fixtures in `tests/conftest.py`.

## Key conventions & invariants

- **Agents communicate via a single final ```json block.** Each role parses the
  last fenced JSON block from agent output (`opencode.extract_decision`). If you
  change a role's output contract, update both its prompt in `templates/<role>.md`
  and the parser/`apply_*` logic in the matching module.
- **Workspace is the source of truth at runtime**, default `~/.cheaphelp`
  (override `CHEAPHELP_HOME`): `config.json`, `.env`, `repos.json`, `agents/`
  (editable prompt overrides), `opencode/opencode.json` (generated),
  `state/<owner>__<repo>/issue-<n>/` (issues.md, plan.md, tasks.json, tasks/),
  `clones/`, `logs/`. After editing models/prompts/sandbox, regenerate with
  `cheaphelp agents sync`.
- **`tasks.json` is authoritative; the `.md` files are readable mirrors.** Use
  `TaskStore` for all task state transitions.
- **cheaphelp owns git remotes**: workers/reviewer never push; the orchestrator
  does (`gitutil.push_branch`). Agent bash is sandboxed (see `opencode.py`
  `_READER_BASH` / `_WORKER_BASH`); only `worker` may edit files.
- **Defaults live in `config.py`** (`DEFAULT_MODELS`, `DEFAULT_LABELS`,
  `DEFAULT_VARIANTS`, `DEFAULT_SANDBOX`). `Config.from_dict` merges over them, so
  new keys must be added to the defaults *and* `from_dict`/`to_dict`.
- **Network isolation in tests**: never make real network/API calls; mock the
  GitHub client and the agent layer. Set `CHEAPHELP_AGENT_MOCK=/path/to/decision.json`
  to make `run_agent` return canned output instead of invoking opencode.
- **Per-issue failures must not abort a tick** — `_process_repo` guards each
  stage; preserve that resilience.

## Commit messages

Angular/Karma convention: `<type>[(scope)]: Subject` (capitalized, no trailing
period). Types: `build`, `chore`, `ci`, `deps`, `docs`, `feat`, `fix`, `perf`,
`refactor`, `style`, `tests`. Don't update `CHANGELOG.md` (generated). Link the
related issue in the PR body (`Closes #<n>`).
