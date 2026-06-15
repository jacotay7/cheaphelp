"""Tests for cheaphelp's responder role."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from cheaphelp._internal import (
    planner,
    reviewer,
    worker,
)
from cheaphelp._internal.config import Config, Workspace
from cheaphelp._internal.github import Comment, Issue
from cheaphelp._internal.orchestrator import classify
from cheaphelp._internal.responder import (
    ATTRIBUTION_PREFIX,
    BOT_MARKER,
    apply_decision,
    attribution_header,
    build_prompt,
    cheaphelp_message,
    is_bot_comment,
    needs_turn,
)
from cheaphelp._internal.tasks import Task


def _issue_with(labels: list[str]) -> Issue:
    return Issue(number=1, title="t", body="b", state="open", labels=labels, user="u", html_url="")


# --- responder -------------------------------------------------------------
def _issue(number: int = 1, labels: list[str] | None = None) -> Issue:
    return Issue(
        number=number,
        title="t",
        body="b",
        state="open",
        labels=labels or [],
        user="alice",
        html_url="",
    )


def _comment(body: str, user: str) -> Comment:
    return Comment(id=1, body=body, user=user, created_at="")


def test_needs_turn_logic() -> None:
    cfg = Config()
    bot = "mybot"
    assert needs_turn(_issue(), [], cfg) is True  # fresh issue
    bot_c = _comment(BOT_MARKER + "\nQ?", bot)
    assert needs_turn(_issue(), [bot_c], cfg) is False  # waiting on human
    human_c = _comment("answer", "alice")
    assert needs_turn(_issue(), [bot_c, human_c], cfg) is True  # human replied
    ready = _issue(labels=[cfg.labels["ready"]])
    assert needs_turn(ready, [human_c], cfg) is False  # already finalized
    # Single-account scenario: a reply from the bot's own account but without the
    # marker must be treated as a human reply (not a bot comment).
    same_account_reply = _comment("got it", bot)
    assert needs_turn(_issue(), [bot_c, same_account_reply], cfg) is True


def test_is_bot_comment_single_account() -> None:
    """Verify is_bot_comment relies solely on the BOT_MARKER, not the author login.

    The bug: when the bot account is the same as the issue author, a plain human
    reply was misidentified as a bot comment because the old is_bot_comment did
    a ``comment.user == bot_login`` check as a fallback.

    Under the fix: only the presence of BOT_MARKER matters.
    """
    bot = "mybot"
    # Same user as bot, body has no marker → NOT a bot comment (the bug fix).
    assert is_bot_comment(_comment("plain reply", bot)) is False
    # Same user AND marker present → still a bot comment.
    assert is_bot_comment(_comment(f"{BOT_MARKER}\nhi", bot)) is True
    # Different user, marker present → still a bot comment (marker alone is enough).
    assert is_bot_comment(_comment(f"{BOT_MARKER}\nhi", "alice")) is True
    # Control: neither user nor marker matches.
    assert is_bot_comment(_comment("alice said hi", "alice")) is False


def test_build_prompt_includes_thread() -> None:
    prompt = build_prompt(_issue(number=42), [_comment("hi", "alice")])
    assert "Issue #42" in prompt
    assert "@alice" in prompt
    assert "hi" in prompt
    # Back-compat: omitted conventions kwarg does not emit a section.
    assert "## Repository conventions" not in prompt


def test_attribution_header_names_agent_and_model() -> None:
    header = attribution_header("responder", "openrouter/x")
    assert header.startswith(ATTRIBUTION_PREFIX)
    assert "responder" in header
    assert "openrouter/x" in header
    # A role with no backing model omits the model segment.
    assert "model" not in attribution_header("quality-gate", None)


def test_cheaphelp_message_prefixes_header_marker_and_is_detected() -> None:
    cfg = Config()  # responder has a model + the "max" variant by default
    body = cheaphelp_message("hello world", "responder", cfg)
    assert body.startswith(ATTRIBUTION_PREFIX)
    assert cfg.model_for("responder") in body
    assert "(max)" in body  # variant recorded
    assert BOT_MARKER in body
    assert "hello world" in body
    # The hidden marker keeps the comment recognisable as cheaphelp's own.
    assert is_bot_comment(_comment(body, "someone-else")) is True


def test_build_prompt_strips_attribution_header_from_thread() -> None:
    cfg = Config()
    own = cheaphelp_message("an earlier question", "responder", cfg)
    prompt = build_prompt(_issue(number=7), [_comment(own, "mybot")])
    # The visible header and hidden marker are not shown back to the responder.
    assert ATTRIBUTION_PREFIX not in prompt
    assert BOT_MARKER not in prompt
    assert "an earlier question" in prompt


# --- conventions injection into build_prompt --------------------------------


def test_responder_build_prompt_with_conventions() -> None:
    prompt = build_prompt(
        _issue(number=1),
        [_comment("hello", "alice")],
        conventions="house rules: no emoji",
    )
    assert "## Repository conventions" in prompt
    assert "house rules: no emoji" in prompt


def test_planner_build_prompt_with_conventions() -> None:
    prompt = planner.build_prompt("spec body", conventions="house rules: no emoji")
    assert "## Repository conventions" in prompt
    assert "house rules: no emoji" in prompt


def test_worker_build_prompt_with_conventions() -> None:
    prompt = worker.build_prompt(
        Task(id="t1", title="Do the thing"),
        "spec body",
        conventions="house rules: no emoji",
    )
    assert "## Repository conventions" in prompt
    assert "house rules: no emoji" in prompt


def test_reviewer_build_prompt_with_conventions() -> None:
    prompt = reviewer.build_prompt(
        "spec",
        "M file.py",
        "diff --git a/file.py b/file.py",
        "### t1: done\nok",
        conventions="house rules: no emoji",
    )
    assert "## Repository conventions" in prompt
    assert "house rules: no emoji" in prompt


def test_planner_build_prompt_no_conventions_by_default() -> None:
    prompt = planner.build_prompt("spec body")
    assert "## Repository conventions" not in prompt


def test_worker_build_prompt_no_conventions_by_default() -> None:
    prompt = worker.build_prompt(Task(id="t1", title="x"), "spec")
    assert "## Repository conventions" not in prompt


def test_reviewer_build_prompt_no_conventions_by_default() -> None:
    prompt = reviewer.build_prompt("spec", "M f.py", "diff", "summary")
    assert "## Repository conventions" not in prompt


def test_build_prompt_conventions_whitespace_only() -> None:
    prompt = build_prompt(_issue(), [], conventions="   ")
    assert "## Repository conventions" not in prompt


def test_responder_prompt_finalize_does_not_say_spec_below() -> None:
    """The responder prompt must warn the model that `finalize` replies are short.

    `issue_md` is written to `issues.md` on disk, not appended to the GitHub
    comment, so the reply must not promise a 'spec below'. Without explicit
    guidance in the prompt, models default to phrasing like 'see the spec
    below', which leaves readers confused when the comment ends with nothing.
    """
    from cheaphelp._internal.templates import load_prompt  # noqa: PLC0415

    prompt = load_prompt("responder")
    prompt_lower = prompt.lower()
    # Negative guidance: tell the model not to say 'spec below' / 'full specification below'.
    assert "spec below" in prompt_lower
    # Positive guidance: point at the recommended 'planning phase' wording.
    assert "planning phase" in prompt_lower
    # A concrete `finalize` JSON example should be present so the model sees the
    # right shape; the original prompt only modelled a `comment` action.
    assert '"action": "finalize"' in prompt


# --- responder apply_decision label lifecycle --------------------------------
def test_apply_decision_comment_adds_needs_human_label(tmp_path: Path) -> None:
    """Comment action adds needs_human label after posting a reply."""

    class _RecGH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def ensure_label(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("ensure_label", a))

        def add_labels(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("add_labels", a))

        def remove_label(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("remove_label", a))

        def create_comment(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("create_comment", a))

        def authenticated_login(self) -> str:
            return "mybot"

    gh = _RecGH()
    ws = Workspace(tmp_path)
    cfg = Config()
    iss = _issue()
    dec = {"action": "comment", "reply": "one q?"}
    apply_decision(gh, ws, cfg, "o", "r", iss, dec)  # ty: ignore[invalid-argument-type]

    needs_human = cfg.labels["needs_human"]
    # find the order of create_comment and add_labels(needs_human)
    ci = next(i for i, c in enumerate(gh.calls) if c[0] == "create_comment")
    ai = next(i for i, c in enumerate(gh.calls) if c[0] == "add_labels" and needs_human in cast("list[str]", c[1][-1]))
    assert ci < ai, "add_labels(needs_human) should come after create_comment"
    # No ready or rejected labels were added.
    assert not any(
        c[0] == "add_labels"
        and (
            cfg.labels["ready"] in cast("list[str]", c[1][-1]) or cfg.labels["rejected"] in cast("list[str]", c[1][-1])
        )
        for c in gh.calls
    )


def test_apply_decision_comment_without_reply_still_adds_needs_human(tmp_path: Path) -> None:
    """Comment action with no reply still adds needs_human; no comment posted."""

    class _RecGH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def ensure_label(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("ensure_label", a))

        def add_labels(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("add_labels", a))

        def remove_label(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("remove_label", a))

        def create_comment(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("create_comment", a))

        def authenticated_login(self) -> str:
            return "mybot"

    gh = _RecGH()
    ws = Workspace(tmp_path)
    cfg = Config()
    iss = _issue()
    dec = {"action": "comment"}
    apply_decision(gh, ws, cfg, "o", "r", iss, dec)  # ty: ignore[invalid-argument-type]

    needs_human = cfg.labels["needs_human"]
    assert any(c[0] == "add_labels" and needs_human in cast("list[str]", c[1][-1]) for c in gh.calls), (
        "needs_human label must be added"
    )
    # No comment posted because no reply text.
    assert not any(c[0] == "create_comment" for c in gh.calls)


def test_apply_decision_finalize_removes_needs_human_and_adds_ready(tmp_path: Path) -> None:
    """Finalize action removes needs_human and adds ready label."""

    class _RecGH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def ensure_label(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("ensure_label", a))

        def add_labels(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("add_labels", a))

        def remove_label(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("remove_label", a))

        def create_comment(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("create_comment", a))

        def authenticated_login(self) -> str:
            return "mybot"

    gh = _RecGH()
    ws = Workspace(tmp_path)
    cfg = Config()
    iss = _issue()
    dec = {"action": "finalize", "issue_md": "# Spec"}
    apply_decision(gh, ws, cfg, "o", "r", iss, dec)  # ty: ignore[invalid-argument-type]

    needs_human = cfg.labels["needs_human"]
    assert any(c[0] == "remove_label" and needs_human in c[1] for c in gh.calls), "needs_human label must be removed"
    assert any(c[0] == "add_labels" and cfg.labels["ready"] in cast("list[str]", c[1][-1]) for c in gh.calls), (
        "ready label must be added"
    )
    # issues.md was written
    issue_md_path = ws.issue_dir("o", "r", 1) / "issues.md"
    assert issue_md_path.exists()


def test_apply_decision_reject_removes_needs_human_and_adds_rejected(tmp_path: Path) -> None:
    """Reject action removes needs_human and adds rejected label."""

    class _RecGH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def ensure_label(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("ensure_label", a))

        def add_labels(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("add_labels", a))

        def remove_label(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("remove_label", a))

        def create_comment(self, *a: object, **_kwargs: object) -> None:
            self.calls.append(("create_comment", a))

        def authenticated_login(self) -> str:
            return "mybot"

    gh = _RecGH()
    ws = Workspace(tmp_path)
    cfg = Config()
    iss = _issue()
    dec = {"action": "reject"}
    apply_decision(gh, ws, cfg, "o", "r", iss, dec)  # ty: ignore[invalid-argument-type]

    needs_human = cfg.labels["needs_human"]
    assert any(c[0] == "remove_label" and needs_human in c[1] for c in gh.calls), "needs_human label must be removed"
    assert any(c[0] == "add_labels" and cfg.labels["rejected"] in cast("list[str]", c[1][-1]) for c in gh.calls), (
        "rejected label must be added"
    )


# --- classify needs_human routing -------------------------------------------
def test_classify_needs_human_routes_to_responder_when_human_replied() -> None:
    """needs_human routes to responder when the last comment is from a human."""
    cfg = Config()
    lab = cfg.labels
    bot_comment = _comment(BOT_MARKER + "\nq?", "bot")
    human_comment = _comment("answer", "alice")
    result = classify(_issue_with([lab["needs_human"]]), [bot_comment, human_comment], cfg)
    assert result == "responder"


def test_classify_needs_human_stays_when_last_comment_is_bot() -> None:
    """needs_human stays terminal when the last comment is from the bot."""
    cfg = Config()
    lab = cfg.labels
    bot_comment = _comment(BOT_MARKER + "\nq?", "bot")
    result = classify(_issue_with([lab["needs_human"]]), [bot_comment], cfg)
    assert result == "needs-human"


def test_classify_needs_human_stays_when_no_comments() -> None:
    """needs_human stays terminal when there are no comments."""
    cfg = Config()
    lab = cfg.labels
    classify(_issue_with([lab["needs_human"]]), [], cfg)
