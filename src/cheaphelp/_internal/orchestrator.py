"""The orchestrator: one tick of the cheaphelp state machine.

A tick is what the systemd timer fires. For each enabled repository it polls open
issues, classifies each into a pipeline stage by its labels, and dispatches the
right agent:

    (no pipeline label) + human spoke last  -> responder  (refine scope)
    cheaphelp:ready / cheaphelp:needs-replan -> planner    (produce tasks)
    cheaphelp:planned, tasks pending         -> worker     (implement)
    cheaphelp:planned, all tasks done        -> reviewer   (PR/replan)
    cheaphelp:in-review, new human feedback   -> rework    (address review, push, re-request review)

Budget guardrail
----------------
When ``config.daily_budget_usd > 0`` the orchestrator checks the cumulative daily
spend (tracked by :class:`~cheaphelp._internal.spend.DailySpendTracker`) before
each stage dispatch. If the cap is exhausted, the stage is skipped and a budget-
exceeded comment is posted on the active issue. Warnings are posted when spend
crosses ``budget_warn_at`` (default 80%) and the hardcoded secondary threshold
(95%). The check is a no-op when the cap is 0 (unlimited).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from cheaphelp._internal import cleanup, fixer, gitutil, opencode, planner, responder, reviewer, rework, worker
from cheaphelp._internal.config import BUDGET_WARN_SECONDARY, Config, Workspace
from cheaphelp._internal.conventions import read_conventions
from cheaphelp._internal.env import GITHUB_TOKEN_KEY, OPENROUTER_API_KEY, load_into_environ
from cheaphelp._internal.github import GitHubClient, Issue
from cheaphelp._internal.gitutil import ensure_clone
from cheaphelp._internal.lock import RunLock
from cheaphelp._internal.opencode import UsageData
from cheaphelp._internal.registry import Registry, RepoEntry
from cheaphelp._internal.spend import DailySpendTracker
from cheaphelp._internal.tasks import IssueCostStore, TaskStore

Logger = Callable[[str], None]

# Stages that the orchestrator will actually dispatch to an agent. Other
# classify() return values are terminal / waiting / idle and are skipped.
_ACTIONABLE_STAGES = frozenset({"responder", "planner", "build", "rework"})


def _is_mock() -> bool:
    return bool(os.environ.get("CHEAPHELP_AGENT_MOCK"))


# A `Depends-on: #41, #42` line in an issue's issues.md declares cross-issue
# ordering: cheaphelp holds the planner/build stage until those issues close.
_DEPENDS_ON_RE = re.compile(r"^\s*depends[ -]on:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def parse_depends_on(issue_md: str) -> list[int]:
    """Return the issue numbers a spec declares it depends on (deduped, sorted)."""
    numbers: set[int] = set()
    for match in _DEPENDS_ON_RE.finditer(issue_md):
        numbers.update(int(tok) for tok in re.findall(r"#?(\d+)", match.group(1)))
    return sorted(numbers)


def _unmet_dependencies(
    workspace: Workspace,
    repo: RepoEntry,
    issue: Issue,
    open_numbers: set[int],
) -> list[int]:
    """Dependencies of `issue` that are still open (so it must wait)."""
    spec = workspace.issue_dir(repo.owner, repo.name, issue.number) / "issues.md"
    if not spec.exists():
        return []
    deps = parse_depends_on(spec.read_text(encoding="utf-8"))
    return [n for n in deps if n != issue.number and n in open_numbers]


def classify(issue: Issue, comments: list, config: Config) -> str:
    """Return the pipeline stage for an issue.

    Always returns a string; actionable stages are ``"responder"``, ``"planner"``,
    ``"build"``, ``"rework"``; waiting/terminal stages are ``"rejected"``,
    ``"needs-human"``; the no-op case is ``"idle"``.
    """
    labels = set(issue.labels)
    lab = config.labels
    # Terminal / waiting-on-human states: leave alone, in precedence order.
    if lab["rejected"] in labels:
        return "rejected"
    # in-review always maps to rework; the no-op "is there new feedback?" check
    # lives in _run_rework so classify() stays GitHub-free.
    if lab["in_review"] in labels:
        return "rework"
    if lab["needs_human"] in labels:
        # If the human replied since the bot asked, route back to the responder.
        if comments and not responder.is_bot_comment(comments[-1]):
            return "responder"
        return "needs-human"
    if lab["planned"] in labels or lab["in_progress"] in labels:
        return "build"  # worker or reviewer, decided by task state
    if labels & {lab["ready"], lab["needs_replan"]}:
        return "planner"
    if responder.needs_turn(issue, comments, config):
        activated = lab.get("activated")
        if activated and activated not in labels:
            return "idle"
        return "responder"
    return "idle"


@dataclass
class RepoReport:
    """Per-repository summary of a tick."""

    slug: str
    issues_considered: int = 0
    issues_skipped: int = 0  # locked by a concurrent tick
    clones_pruned: int = 0  # build clones removed for closed issues
    turns_taken: int = 0
    actions: list[str] = field(default_factory=list)
    error: str | None = None
    cost: UsageData = field(default_factory=UsageData)
    issue_costs: dict[int, dict[str, list[UsageData]]] = field(default_factory=dict)
    budget_exhausted: bool = False
    daily_spend: float = 0.0


@dataclass
class TickReport:
    """Summary of a full orchestrator tick."""

    bot_login: str = ""
    dry_run: bool = False
    repos: list[RepoReport] = field(default_factory=list)
    error: str | None = None
    skipped: bool = False
    total_cost: UsageData = field(default_factory=UsageData)
    daily_spend: float = 0.0
    daily_budget: float = 0.0
    budget_exhausted: bool = False

    @property
    def total_turns(self) -> int:
        return sum(r.turns_taken for r in self.repos)


def _short_exc(exc: Exception) -> str:
    """Format an exception for a one-line log entry.

    A subprocess timeout carries the entire command (including the multi-KB
    agent prompt) in its string form; collapse it to something readable.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"agent timed out after {exc.timeout:.0f}s"
    text = " ".join(str(exc).split())
    limit = 200
    return text if len(text) <= limit else text[:limit] + "…"


def _record_cost(
    workspace: Workspace,
    repo: RepoEntry,
    number: int,
    role: str,
    usage: UsageData | None,
    report: RepoReport,
    tracker: DailySpendTracker | None = None,
) -> None:
    """Record token/cost data from an agent turn and persist to the daily tracker."""
    if usage is None:
        return
    IssueCostStore(workspace.issue_dir(repo.owner, repo.name, number)).add(usage, role=role)
    report.cost = report.cost + usage
    by_issue = report.issue_costs.setdefault(number, {})
    by_issue.setdefault(role, []).append(usage)
    if usage.cost_usd != 0.0 and tracker is not None:
        tracker.record(usage)
        report.daily_spend = tracker.daily_spend()


def _check_budget(
    tracker: DailySpendTracker | None,
    config: Config,
    gh: GitHubClient | None,
    repo: RepoEntry,
    issue: Issue | None,
    log: Logger,
    report: RepoReport,
) -> bool:
    """Return True if the next stage may run under the daily budget.

    Side effects: may post a one-time pause comment, a one-time warn
    comment per threshold, log, and set ``report.budget_exhausted``.
    """
    cap = config.daily_budget_usd
    if cap <= 0.0 or tracker is None:
        return True
    spend = tracker.daily_spend()
    if spend >= cap:
        if not report.budget_exhausted:
            body = (
                f"Pausing cheaphelp: today's spend ${spend:.3f} reached the configured "
                f"daily cap of ${cap:.3f}. Work will resume tomorrow (UTC)."
            )
            if gh is not None and issue is not None:
                try:
                    gh.create_comment(
                        repo.owner,
                        repo.name,
                        issue.number,
                        responder.cheaphelp_message(body, "budget-exceeded", config),
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"  ! {repo.slug}#{issue.number}: budget comment failed: {_short_exc(exc)}")
            log(f"  ! {repo.slug}: daily budget exhausted (${spend:.3f} >= ${cap:.3f})")
            report.budget_exhausted = True
        if issue is not None:
            report.actions.append(f"#{issue.number}: budget-exceeded")
        return False
    # Warnings: only fire once per threshold per day.
    for frac in (config.budget_warn_at, BUDGET_WARN_SECONDARY):
        if frac <= 0.0 or tracker.was_warned(frac):
            continue
        if spend >= cap * frac:
            tracker.mark_warned(frac)
            pct = round(spend / cap * 100)
            body = (
                f"Heads up: today's spend is ${spend:.3f} ({pct}% of the daily cap of "
                f"${cap:.3f}). Approaching the budget limit."
            )
            if gh is not None and issue is not None:
                try:
                    gh.create_comment(
                        repo.owner,
                        repo.name,
                        issue.number,
                        responder.cheaphelp_message(body, "budget-warn", config),
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"  ! {repo.slug}#{issue.number}: budget-warn comment failed: {_short_exc(exc)}")
            log(f"  · {repo.slug}: daily spend at {pct}% of cap (${spend:.3f} / ${cap:.3f})")
    return True


def _run_responder(gh, workspace, config, repo, issue, comments, cwd, log, report, tracker=None) -> None:  # noqa: ANN001
    if not _check_budget(tracker, config, gh, repo, issue, log, report):
        return
    prompt = responder.build_prompt(issue, comments, conventions=read_conventions(cwd))
    log(f"  · {repo.slug}#{issue.number}: running responder ({config.model_for('responder')})…")
    issue_dir = workspace.issue_dir(repo.owner, repo.name, issue.number)
    result = opencode.run_agent(
        workspace,
        config,
        "responder",
        prompt,
        cwd=cwd,
        timeout=config.agent_timeout,
        issue_dir=issue_dir,
    )
    _record_cost(workspace, repo, issue.number, "responder", result.usage, report, tracker)
    if result.decision is None:
        log(f"  ! {repo.slug}#{issue.number}: responder produced no decision (rc={result.returncode})")
        report.actions.append(f"#{issue.number}: responder unparseable")
        return
    applied = responder.apply_decision(gh, workspace, config, repo.owner, repo.name, issue, result.decision)
    report.turns_taken += 1
    msg = f"#{issue.number}: responder {applied.action}" + (f" (error: {applied.error})" if applied.error else "")
    report.actions.append(msg)
    log(f"  > {repo.slug}{msg}")


def _run_planner(gh, workspace, config, repo, issue, cwd, log, report, tracker=None) -> None:  # noqa: ANN001
    if not _check_budget(tracker, config, gh, repo, issue, log, report):
        return
    issue_dir = workspace.issue_dir(repo.owner, repo.name, issue.number)
    spec_path = issue_dir / "issues.md"
    if not spec_path.exists():
        log(f"  ! {repo.slug}#{issue.number}: no issues.md found; skipping planner")
        report.actions.append(f"#{issue.number}: planner missing issues.md")
        return
    replan_path = issue_dir / "replan.md"
    replan_notes = replan_path.read_text(encoding="utf-8") if replan_path.exists() else ""
    existing_tasks = TaskStore(issue_dir).load()
    prompt = planner.build_prompt(
        spec_path.read_text(encoding="utf-8"),
        replan_notes=replan_notes,
        existing_tasks=existing_tasks,
        conventions=read_conventions(cwd),
    )
    log(f"  · {repo.slug}#{issue.number}: running planner ({config.model_for('planner')})…")
    result = opencode.run_agent(
        workspace,
        config,
        "planner",
        prompt,
        cwd=cwd,
        timeout=config.agent_timeout,
        issue_dir=issue_dir,
    )
    _record_cost(workspace, repo, issue.number, "planner", result.usage, report, tracker)
    if result.decision is None:
        log(f"  ! {repo.slug}#{issue.number}: planner produced no decision (rc={result.returncode})")
        report.actions.append(f"#{issue.number}: planner unparseable")
        return
    res = planner.apply_plan(gh, workspace, config, repo.owner, repo.name, issue.number, result.decision)
    report.turns_taken += 1
    if res.error:
        log(f"  ! {repo.slug}#{issue.number}: planner error: {res.error}")
        report.actions.append(f"#{issue.number}: planner error ({res.error})")
    else:
        log(f"  > {repo.slug}#{issue.number}: planned {res.task_count} task(s)")
        report.actions.append(f"#{issue.number}: planned {res.task_count} task(s)")


def _run_checks(work_dir, repo) -> tuple[int, str]:  # noqa: ANN001
    """Run the repo's quality-gate command, returning ``(returncode, output)``."""
    try:
        return gitutil.run_command(work_dir, repo.checks)
    except Exception as exc:  # noqa: BLE001
        return 1, f"quality gate could not run: {exc}"


def _gate_to_replan(gh, workspace, config, repo, number, rc, output, log, report) -> bool:  # noqa: ANN001
    """Send a failed quality gate back to the planner. Always returns False."""
    tail = output[-4000:]
    issue_dir = workspace.issue_dir(repo.owner, repo.name, number)
    issue_dir.mkdir(parents=True, exist_ok=True)
    (issue_dir / "replan.md").write_text(
        f"The automated quality checks failed (exit {rc}). The implementation must "
        f"be corrected before it can be reviewed. Command:\n\n    {repo.checks}\n\n"
        f"Output (tail):\n\n```\n{tail}\n```\n",
        encoding="utf-8",
    )
    gh.ensure_label(
        repo.owner,
        repo.name,
        config.labels["needs_replan"],
        color="fbca04",
        description="cheaphelp: reviewer sent back to planner",
    )
    gh.add_labels(repo.owner, repo.name, number, [config.labels["needs_replan"]])
    gh.remove_label(repo.owner, repo.name, number, config.labels["planned"])
    gh.remove_label(repo.owner, repo.name, number, config.labels["in_progress"])
    gh.create_comment(
        repo.owner,
        repo.name,
        number,
        responder.cheaphelp_message(
            f"Quality checks failed; sending back to planning.\n\n```\n{tail[-1500:]}\n```",
            "quality-gate",
            config,
        ),
    )
    log(f"  ! {repo.slug}#{number}: quality gate FAILED -> needs-replan")
    report.actions.append(f"#{number}: quality gate failed")
    return False


def _quality_gate(gh, workspace, config, repo, issue, work_dir, log, report, *, token=None, tracker=None) -> bool:  # noqa: ANN001
    """Run the repo's check command in the clone, repairing failures before replan.

    On a clean pass, returns True (proceed to the reviewer). On failure, runs up
    to ``config.quality_gate_fix_attempts`` ``fixer`` turns — each re-running the
    gate — to repair the working tree cheaply instead of an expensive replan. If
    the gate still fails, loops the issue back to the planner and returns False.
    """
    number = issue.number
    # Cheap path first: auto-fix trivial issues (formatting, import order,
    # lint --fix) and commit them, so they never trigger an expensive replan.
    if repo.autofix:
        log(f"  · {repo.slug}#{number}: auto-fixing ({repo.autofix})")
        try:
            gitutil.run_command(work_dir, repo.autofix)
        except Exception as exc:  # noqa: BLE001
            log(f"  · {repo.slug}#{number}: auto-fix command errored: {exc}")
        if gitutil.commit_all(work_dir, message="cheaphelp: auto-fix (format/lint)"):
            log(f"  > {repo.slug}#{number}: auto-fix made changes, committed")

    log(f"  · {repo.slug}#{number}: running quality gate ({repo.checks})")
    rc, output = _run_checks(work_dir, repo)
    if rc == 0:
        log(f"  > {repo.slug}#{number}: quality gate passed")
        return True

    # Quality gate failed: try to repair it with a worker before replanning.
    attempts = config.quality_gate_fix_attempts
    for attempt in range(1, attempts + 1):
        # The fixer is an agent call; respect the daily budget.
        if not _check_budget(tracker, config, gh, repo, issue, log, report):
            break
        log(f"  · {repo.slug}#{number}: quality gate failed; running fixer (attempt {attempt}/{attempts})…")
        res = fixer.run_fix(workspace, config, repo, number, output, work_dir, token=token)
        _record_cost(workspace, repo, number, "fixer", res.usage, report, tracker)
        report.turns_taken += 1
        flag = " +commit" if res.committed else ""
        log(f"  > {repo.slug}#{number}: fixer -> {res.status}{flag}")
        report.actions.append(f"#{number}: fixer {res.status}")
        if not res.committed:
            # No changes were made; re-running the gate would fail identically.
            log(f"  · {repo.slug}#{number}: fixer made no changes; sending back to planning")
            break
        rc, output = _run_checks(work_dir, repo)
        if rc == 0:
            log(f"  > {repo.slug}#{number}: quality gate passed after fix")
            report.actions.append(f"#{number}: quality gate passed after fix")
            return True

    return _gate_to_replan(gh, workspace, config, repo, number, rc, output, log, report)


def _check_blast_radius(gh, workspace, config, repo, number, work_dir, log, report) -> bool:  # noqa: ANN001, ARG001
    """Bypass the reviewer when the branch diff vs base exceeds the per-repo limits.

    Returns True if the diff is within limits (proceed to the reviewer) or if
    we can't measure it (safe default). On a violation, posts an attributed
    comment with the diff stats, adds the `needs-human` label, removes
    `in-progress`, and returns False so the caller skips the reviewer.
    """
    stats = gitutil.diff_stat(work_dir, repo)
    if stats is None:
        return True  # unparseable / no diff -> don't block
    files, insertions, deletions = stats

    files_ok = repo.max_diff_files == 0 or files <= repo.max_diff_files
    lines = insertions + deletions
    lines_ok = repo.max_diff_lines == 0 or lines <= repo.max_diff_lines
    if files_ok and lines_ok:
        return True

    def _fmt(value: int) -> str:
        return "unlimited" if value == 0 else str(value)

    body = (
        f"The diff exceeds the configured blast-radius guardrail and was not "
        f"opened as a PR. The branch remains on the remote for inspection.\n\n"
        f"- Files changed: {files} (limit: {_fmt(repo.max_diff_files)})\n"
        f"- Lines added: {insertions} (limit: {_fmt(repo.max_diff_lines)})\n"
        f"- Lines removed: {deletions}\n\n"
        f"A human can widen the limits via "
        f"`cheaphelp repo set --max-diff-files N --max-diff-lines N {repo.slug}` "
        f"or split the work into smaller issues."
    )
    gh.ensure_label(
        repo.owner,
        repo.name,
        config.labels["needs_human"],
        color="d93f0b",
        description="cheaphelp: stuck; needs a human",
    )
    gh.add_labels(repo.owner, repo.name, number, [config.labels["needs_human"]])
    gh.remove_label(repo.owner, repo.name, number, config.labels["in_progress"])
    gh.create_comment(
        repo.owner,
        repo.name,
        number,
        responder.cheaphelp_message(body, "blast-radius", config),
    )
    log(f"  ! {repo.slug}#{number}: blast-radius exceeded ({files} files, {lines} lines) -> needs-human")
    report.actions.append(f"#{number}: blast-radius exceeded")
    return False


def _run_build(gh, workspace, config, repo, issue, token, log, report, tracker=None) -> None:  # noqa: ANN001
    """Worker + reviewer stage for a planned issue."""
    if not _check_budget(tracker, config, gh, repo, issue, log, report):
        return
    number = issue.number
    store = TaskStore(workspace.issue_dir(repo.owner, repo.name, number))
    tasks = store.load()
    if not tasks:
        log(f"  ! {repo.slug}#{number}: planned but no tasks found; skipping")
        report.actions.append(f"#{number}: no tasks")
        return

    branch = worker.branch_name(number)
    work_dir = workspace.work_clone_path(repo.owner, repo.name, number)
    try:
        gitutil.ensure_work_clone(work_dir, repo, token=token, branch=branch)
    except Exception as exc:  # noqa: BLE001
        log(f"  ! {repo.slug}#{number}: work clone failed: {exc}")
        report.actions.append(f"#{number}: work clone failed")
        return

    gh.ensure_label(
        repo.owner,
        repo.name,
        config.labels["in_progress"],
        color="f9a825",
        description="cheaphelp: worker is actively executing tasks",
    )
    gh.add_labels(repo.owner, repo.name, number, [config.labels["in_progress"]])

    # Run every currently-ready task; a linear chain finishes in one tick. When
    # max_tasks_per_tick is set, stop after that many tasks and resume the rest
    # on the next tick (task state is persisted, so this is safe).
    max_tasks = config.max_tasks_per_tick
    ran = 0
    while ran < len(tasks):
        task = store.next_ready()
        if task is None:
            break
        log(f"  · {repo.slug}#{number}: running worker {task.id} ({config.model_for('worker')}): {task.title}…")
        res = worker.run_task(workspace, config, repo, number, task, work_dir, token=token)
        _record_cost(workspace, repo, number, "worker", res.usage, report, tracker)
        report.turns_taken += 1
        ran += 1
        flag = " +commit" if res.committed else ""
        log(f"  > {repo.slug}#{number}: worker {task.id} -> {res.status}{flag}")
        report.actions.append(f"#{number}: worker {task.id} {res.status}")
        if res.status != "done":
            break
        if max_tasks > 0 and ran >= max_tasks and store.next_ready() is not None:
            log(f"  · {repo.slug}#{number}: ran {ran} task(s) this tick; deferring the rest")
            report.actions.append(f"#{number}: deferred remaining tasks")
            break
        # Mid-tick budget check: an in-flight agent call finished; before the
        # next worker, check whether the daily cap has been hit.
        if store.next_ready() is not None and not _check_budget(tracker, config, gh, repo, issue, log, report):
            break

    tasks = store.load()
    if store.all_done(tasks):
        # Quality gate: a failing check never becomes a PR. The gate first tries
        # to repair the failure with a fixer turn, then loops back to planning.
        if repo.checks and not _quality_gate(
            gh,
            workspace,
            config,
            repo,
            issue,
            work_dir,
            log,
            report,
            token=token,
            tracker=tracker,
        ):
            return
        # Blast-radius guardrail: skip the reviewer (and its LLM call) when the
        # diff is too large to open as a PR. Routes the issue to needs-human
        # with a diff-stats comment; the branch stays on the remote.
        if not _check_blast_radius(gh, workspace, config, repo, number, work_dir, log, report):
            return
        # Budget check before the reviewer call; a prior worker may have tipped
        # the daily spend over the cap.
        if not _check_budget(tracker, config, gh, repo, issue, log, report):
            return
        log(f"  > {repo.slug}#{number}: all tasks done; running reviewer")
        rr = reviewer.review_issue(gh, workspace, config, repo, number, work_dir, token=token)
        _record_cost(workspace, repo, number, "reviewer", rr.usage, report, tracker)
        report.turns_taken += 1
        detail = rr.pr_url or rr.error or rr.decision
        log(f"  > {repo.slug}#{number}: reviewer {rr.decision} ({detail})")
        report.actions.append(f"#{number}: reviewer {rr.decision}")
    elif store.is_blocked(tasks):
        gh.ensure_label(
            repo.owner,
            repo.name,
            config.labels["needs_human"],
            color="d93f0b",
            description="cheaphelp: stuck; needs a human",
        )
        gh.add_labels(repo.owner, repo.name, number, [config.labels["needs_human"]])
        gh.remove_label(repo.owner, repo.name, number, config.labels["in_progress"])
        log(f"  ! {repo.slug}#{number}: blocked; labeled needs-human")
        report.actions.append(f"#{number}: blocked")


def _run_rework(gh, workspace, config, repo, number, token, log, report, tracker=None) -> None:  # noqa: ANN001
    """Rework stage: respond to unaddressed PR review feedback."""
    if not _check_budget(tracker, config, gh, repo, None, log, report):  # issue=None — rework resolves by number
        return
    try:
        log(f"  · {repo.slug}#{number}: running rework ({config.model_for('rework')})…")
        res = rework.run_rework(gh, workspace, config, repo, number, token=token)
        action = f"#{number}: rework {res.status}"
        if res.error:
            action += f" ({res.error})"
        if res.committed:
            action += " +commit"
        if res.pushed:
            action += " +push"
        report.actions.append(action)
        log(f"  > {repo.slug}{action}")
        # Only count turns for statuses that did actual work; no_feedback is a free no-op.
        if res.status in ("done", "escalated", "blocked"):
            report.turns_taken += 1
    except Exception as exc:  # noqa: BLE001
        log(f"  ! {repo.slug}#{number}: rework crashed: {_short_exc(exc)}")
        report.actions.append(f"#{number}: rework error")


def _process_repo(
    gh: GitHubClient,
    workspace: Workspace,
    config: Config,
    repo: RepoEntry,
    token: str,
    *,
    dry_run: bool,
    log: Logger,
    max_issues: int = 0,
    tracker: DailySpendTracker | None = None,
) -> RepoReport:
    report = RepoReport(slug=repo.slug)
    try:
        issues = gh.list_open_issues(repo.owner, repo.name)
    except Exception as exc:  # noqa: BLE001 - surface per-repo failures, keep going
        report.error = str(exc)
        log(f"  ! {repo.slug}: failed to list issues: {exc}")
        return report

    open_numbers = {issue.number for issue in issues}

    # Ensure the activation label exists on the repo.
    gh.ensure_label(
        repo.owner,
        repo.name,
        config.labels["activated"],
        color="5319e7",
        description="cheaphelp: human applied this label to opt the issue into the pipeline",
    )

    # Reclaim disk: drop build clones for issues that have since closed. State is
    # kept. Guarded by each issue's lock so it can't race a concurrent tick.
    if config.prune_work_clones:
        pruned = cleanup.prune_repo_work_clones(workspace, repo, open_numbers, dry_run=dry_run, log=log)
        report.clones_pruned = len(pruned)

    work: list[tuple[str, Issue, list]] = []
    for issue in issues:
        comments = gh.list_issue_comments(repo.owner, repo.name, issue.number)
        stage = classify(issue, comments, config)
        if stage not in _ACTIONABLE_STAGES:
            continue
        # Hold the planner/build stages until depended-on issues are closed.
        if stage in {"planner", "build"}:
            unmet = _unmet_dependencies(workspace, repo, issue, open_numbers)
            if unmet:
                deps = ", ".join(f"#{n}" for n in unmet)
                log(f"  · {repo.slug}#{issue.number}: waiting on {deps} (open); deferring")
                report.actions.append(f"#{issue.number}: waiting on {deps}")
                continue
        work.append((stage, issue, comments))
    report.issues_considered = len(work)

    if max_issues > 0 and len(work) > max_issues:
        log(f"  · {repo.slug}: capping at {max_issues} issue(s) this tick ({len(work)} actionable)")
        work = work[:max_issues]

    if not work:
        log(f"  - {repo.slug}: nothing to do")
        return report

    # Repo-wide budget check: skip all work if the daily cap is already hit.
    if not _check_budget(tracker, config, None, repo, None, log, report):
        log(f"  ! {repo.slug}: skipping all issues — daily budget exhausted")
        return report

    if dry_run:
        for stage, issue, _ in work:
            log(f"  · {repo.slug}#{issue.number}: would run {stage}")
            report.actions.append(f"#{issue.number}: dry-run {stage}")
        return report

    # Clone once so read-only agents (responder, planner) can read the codebase.
    # Serialise the fetch/reset across overlapping ticks (the clone dir is shared).
    cwd = workspace.home
    if not _is_mock():
        try:
            with RunLock(workspace.clone_lock_path(repo.owner, repo.name), blocking=True):
                cwd = ensure_clone(workspace.clone_path(repo.owner, repo.name), repo, token=token)
        except Exception as exc:  # noqa: BLE001
            report.error = f"clone failed: {exc}"
            log(f"  ! {repo.slug}: clone failed: {exc}")
            return report

    for stage, issue, _comments in work:
        # Per-issue lock: if another tick is already on this issue, skip it (don't
        # block) and move to the next so concurrent ticks make progress.
        with RunLock(workspace.issue_lock_path(repo.owner, repo.name, issue.number)) as issue_lock:
            if not issue_lock.acquired:
                pid = issue_lock.holder_pid
                suffix = f" (PID {pid})" if pid is not None else ""
                log(f"  · {repo.slug}#{issue.number}: already being worked on{suffix}; skipping")
                report.issues_skipped += 1
                report.actions.append(f"#{issue.number}: skipped (in progress elsewhere)")
                continue
            # Re-validate under the lock. Between classifying this issue and
            # acquiring its lock, an overlapping tick may have already acted on it
            # (responded, planned, advanced it to a later stage). Re-fetch the
            # current state and act only if it is still the same stage — this makes
            # overlapping ticks idempotent: no double responses, plans, etc.
            try:
                fresh_issue = gh.get_issue(repo.owner, repo.name, issue.number)
                fresh_comments = gh.list_issue_comments(repo.owner, repo.name, issue.number)
            except Exception as exc:  # noqa: BLE001 - a refresh failure must not abort the tick
                log(f"  ! {repo.slug}#{issue.number}: could not refresh state: {_short_exc(exc)}")
                report.actions.append(f"#{issue.number}: refresh failed")
                continue
            current = classify(fresh_issue, fresh_comments, config)
            if current != stage:
                log(f"  · {repo.slug}#{issue.number}: now '{current}' (was '{stage}'); already handled, skipping")
                report.issues_skipped += 1
                report.actions.append(f"#{issue.number}: skipped (already {current})")
                continue
            try:
                # Per-issue budget check before dispatching to any agent.
                if not _check_budget(tracker, config, gh, repo, fresh_issue, log, report):
                    break  # no further issues for this repo
                if stage == "responder":
                    _run_responder(
                        gh,
                        workspace,
                        config,
                        repo,
                        fresh_issue,
                        fresh_comments,
                        cwd,
                        log,
                        report,
                        tracker=tracker,
                    )
                elif stage == "planner":
                    _run_planner(gh, workspace, config, repo, fresh_issue, cwd, log, report, tracker=tracker)
                elif stage == "build":
                    _run_build(gh, workspace, config, repo, fresh_issue, token, log, report, tracker=tracker)
                elif stage == "rework":
                    _run_rework(gh, workspace, config, repo, fresh_issue.number, token, log, report, tracker=tracker)
            except Exception as exc:  # noqa: BLE001 - one issue's failure must not abort the tick
                log(f"  ! {repo.slug}#{issue.number}: {stage} crashed: {_short_exc(exc)}")
                report.actions.append(f"#{issue.number}: {stage} crashed")

    return report


def tick(
    workspace: Workspace,
    *,
    dry_run: bool = False,
    log: Logger | None = None,
    max_issues: int = 0,
) -> TickReport:
    """Run one orchestrator tick across all enabled repositories.

    Ticks may overlap: rather than a single global lock, each issue is guarded by
    its own lock so a long build on one issue never blocks work on another.
    """
    log = log or (lambda _msg: None)
    report = TickReport(dry_run=dry_run)

    if not workspace.exists():
        report.error = "workspace not initialised; run `cheaphelp init` first"
        return report

    load_into_environ(workspace.env_path)
    config = workspace.load_config()
    token = os.environ.get(GITHUB_TOKEN_KEY, "")

    if not token:
        report.error = f"{GITHUB_TOKEN_KEY} not set; add it to {workspace.env_path}"
        return report
    if not _is_mock() and not os.environ.get(OPENROUTER_API_KEY):
        log(f"  (warning: {OPENROUTER_API_KEY} not set; agent calls will fail)")

    repos = [r for r in Registry(workspace.registry_path).load() if r.enabled]
    if not repos:
        report.error = "no enabled repositories registered; use `cheaphelp repo add owner/name`"
        return report

    tracker = DailySpendTracker(workspace.state_dir)

    try:
        with GitHubClient(
            token,
            retry_attempts=config.retry_attempts,
            retry_base_delay=config.retry_base_delay,
        ) as gh:
            report.bot_login = gh.authenticated_login()
            log(f"acting as @{report.bot_login} ({'dry-run' if dry_run else 'live'})")
            for repo in repos:
                report.repos.append(
                    _process_repo(
                        gh,
                        workspace,
                        config,
                        repo,
                        token,
                        dry_run=dry_run,
                        log=log,
                        max_issues=max_issues,
                        tracker=tracker,
                    ),
                )
    except Exception as exc:  # noqa: BLE001 - top-level guard for the tick
        report.error = str(exc)

    for r in report.repos:
        report.total_cost = report.total_cost + r.cost

    # Populate top-level budget fields from the tracker.
    report.daily_spend = tracker.daily_spend()
    report.daily_budget = config.daily_budget_usd
    report.budget_exhausted = any(r.budget_exhausted for r in report.repos)
    # Ensure every RepoReport has the current daily spend (in case a repo was
    # skipped wholesale before any agent ran).
    for r in report.repos:
        if r.daily_spend == 0.0 and r.budget_exhausted:
            r.daily_spend = report.daily_spend

    return report
