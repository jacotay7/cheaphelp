"""Tests for cheaphelp's GitHub REST client."""

from __future__ import annotations

import random
from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest

from cheaphelp._internal.github import GitHubClient, GitHubError, PRReviewComment


def _make_github_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    root: str = "https://api.github.com",
    timeout: float = 30.0,
    retry_attempts: int = 3,
    retry_base_delay: float = 1.0,
) -> GitHubClient:
    """Build a GitHubClient wired to an httpx.MockTransport (no network)."""
    client = httpx.Client(base_url=root, transport=httpx.MockTransport(handler))
    return GitHubClient(
        "test-token",
        root=root,
        timeout=timeout,
        retry_attempts=retry_attempts,
        retry_base_delay=retry_base_delay,
        client=client,
    )


# --- GitHubClient retry tests -----------------------------------------------
def _make_counting_handler() -> tuple[list[httpx.Response], Callable]:
    """Return (responses, handler) — the handler pops from *responses* each call.

    Pop an ``httpx.Response`` to return, or raise the item if it is an exception class.
    """
    items: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return items.pop(0)

    return items, handler


def test_request_retries_on_5xx_then_succeeds() -> None:
    items, handler = _make_counting_handler()
    items.append(httpx.Response(503))
    items.append(httpx.Response(503))
    items.append(httpx.Response(200, json={"ok": True}))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.01)
    result = gh._request("GET", "/test")
    assert result == {"ok": True}
    assert len(items) == 0  # all consumed


def test_request_retries_on_429_then_succeeds() -> None:
    items, handler = _make_counting_handler()
    items.append(httpx.Response(429))
    items.append(httpx.Response(429))
    items.append(httpx.Response(200, json={"ok": True}))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.01)
    result = gh._request("GET", "/test")
    assert result == {"ok": True}
    assert len(items) == 0


def test_request_does_not_retry_on_404() -> None:
    items, handler = _make_counting_handler()
    items.append(httpx.Response(404))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.01)
    with pytest.raises(GitHubError) as exc_info:
        gh._request("GET", "/test")
    assert "404" in str(exc_info.value)
    assert len(items) == 0


def test_request_does_not_retry_on_422() -> None:
    items, handler = _make_counting_handler()
    items.append(httpx.Response(422))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.01)
    with pytest.raises(GitHubError) as exc_info:
        gh._request("GET", "/test")
    assert "422" in str(exc_info.value)
    assert len(items) == 0


def test_request_retries_on_connect_error() -> None:
    calls: list[int] = []

    def handler_connect(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"ok": True})

    gh = _make_github_client(handler_connect, retry_attempts=3, retry_base_delay=0.01)
    result = gh._request("GET", "/test")
    assert result == {"ok": True}
    assert len(calls) == 3


def test_request_exhausted_retries_raises_last_error() -> None:
    items, handler = _make_counting_handler()
    for _ in range(4):  # one more than needed
        items.append(httpx.Response(503))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.01)
    with pytest.raises(GitHubError) as exc_info:
        gh._request("GET", "/test")
    assert "503" in str(exc_info.value)


def test_request_exhausted_transport_raises_last_error() -> None:
    calls: list[int] = []

    def handler_timeout(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("timed out")

    gh = _make_github_client(handler_timeout, retry_attempts=3, retry_base_delay=0.01)
    with pytest.raises(httpx.ReadTimeout):
        gh._request("GET", "/test")
    assert len(calls) == 3


def test_request_honors_retry_after() -> None:
    sleeps: list[float] = []

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    items, handler = _make_counting_handler()
    items.append(httpx.Response(429, headers={"Retry-After": "5"}))
    items.append(httpx.Response(200, json={"ok": True}))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.0)

    with patch("time.sleep", fake_sleep):
        result = gh._request("GET", "/test")
    assert result == {"ok": True}
    assert len(sleeps) == 1
    assert sleeps[0] >= 5.0


def test_request_honors_retry_after_caps_at_60() -> None:
    from unittest.mock import patch  # noqa: PLC0415

    sleeps: list[float] = []

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    items, handler = _make_counting_handler()
    items.append(httpx.Response(429, headers={"Retry-After": "999"}))
    items.append(httpx.Response(200, json={"ok": True}))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.0)

    with patch("time.sleep", fake_sleep):
        result = gh._request("GET", "/test")
    assert result == {"ok": True}
    assert len(sleeps) == 1
    assert sleeps[0] <= 60.0


def test_request_no_sleep_on_last_attempt() -> None:
    from unittest.mock import patch  # noqa: PLC0415

    sleeps: list[float] = []

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    items, handler = _make_counting_handler()
    for _ in range(3):
        items.append(httpx.Response(503))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=0.01)

    with patch("time.sleep", fake_sleep), pytest.raises(GitHubError):
        gh._request("GET", "/test")
    assert len(sleeps) == 2  # retry_attempts - 1


def test_request_backoff_is_exponential() -> None:
    from unittest.mock import patch  # noqa: PLC0415

    random.seed(42)  # deterministic jitter
    sleeps: list[float] = []

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    items, handler = _make_counting_handler()
    for _ in range(3):
        items.append(httpx.Response(503))
    gh = _make_github_client(handler, retry_attempts=3, retry_base_delay=1.0)

    with patch("time.sleep", fake_sleep), pytest.raises(GitHubError):
        gh._request("GET", "/test")
    assert len(sleeps) == 2

    # Attempt 1: base * 2^(0) = 1.0, jitter ±0.25
    assert 0.75 <= sleeps[0] <= 1.25
    # Attempt 2: base * 2^(1) = 2.0, jitter ±0.5
    assert 1.5 <= sleeps[1] <= 2.5


# --- PR review API ---------------------------------------------------------


def _make_client() -> GitHubClient:
    """Build a GitHubClient with a non-empty token for testing."""
    return GitHubClient("test-token")


def test_get_pull_request_returns_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = _make_client()
    canned = {"number": 7, "state": "open", "head": {"sha": "abc123"}, "requested_reviewers": [{"login": "alice"}]}

    def fake_request(method: str, path: str, **kwargs: object) -> dict:
        assert path == "/repos/owner/repo/pulls/7"
        return canned

    monkeypatch.setattr(gh, "_request", fake_request)
    result = gh.get_pull_request("owner", "repo", 7)
    assert result["number"] == 7
    assert result["head"]["sha"] == "abc123"


def test_list_pr_reviews_returns_list(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = _make_client()
    canned = [
        {"id": 1, "state": "CHANGES_REQUESTED", "user": {"login": "alice"}, "submitted_at": "2024-01-01T00:00:00Z"},
        {"id": 2, "state": "APPROVED", "user": {"login": "bob"}, "submitted_at": "2024-01-02T00:00:00Z"},
    ]

    def fake_paginate(path: str, **params: object) -> list[dict]:
        assert path == "/repos/owner/repo/pulls/7/reviews"
        return canned

    monkeypatch.setattr(gh, "_paginate", fake_paginate)
    result = gh.list_pr_reviews("owner", "repo", 7)
    assert len(result) == 2
    assert result[0]["state"] == "CHANGES_REQUESTED"
    assert result[1]["user"]["login"] == "bob"


def test_list_pr_reviews_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = _make_client()
    monkeypatch.setattr(gh, "_paginate", lambda _path, **_: [])
    assert gh.list_pr_reviews("owner", "repo", 7) == []


def test_list_pr_review_comments_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = _make_client()
    canned = [
        {
            "id": 42,
            "body": "please fix this",
            "user": {"login": "reviewer1"},
            "created_at": "2024-01-01T00:00:00Z",
            "path": "src/main.py",
            "line": 15,
            "commit_id": "abc123",
        },
    ]

    def fake_paginate(path: str, **params: object) -> list[dict]:
        assert path == "/repos/owner/repo/pulls/7/comments"
        return canned

    monkeypatch.setattr(gh, "_paginate", fake_paginate)
    result = gh.list_pr_review_comments("owner", "repo", 7)
    assert len(result) == 1
    assert isinstance(result[0], PRReviewComment)
    assert result[0].id == 42
    assert result[0].body == "please fix this"
    assert result[0].user == "reviewer1"
    assert result[0].path == "src/main.py"
    assert result[0].line == 15
    assert result[0].commit_id == "abc123"


def test_pr_review_comment_from_payload_tolerates_missing_path_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A review comment at the file level (not line-level) may omit path/line."""
    gh = _make_client()
    canned = [
        {
            "id": 99,
            "body": "overall file feedback",
            "user": {"login": "reviewer2"},
            "created_at": "2024-01-01T00:00:00Z",
            "commit_id": "def456",
            # No "path" or "line" key.
        },
    ]

    monkeypatch.setattr(gh, "_paginate", lambda _path, **_: canned)
    result = gh.list_pr_review_comments("owner", "repo", 7)
    assert len(result) == 1
    assert result[0].id == 99
    assert result[0].path == ""  # default empty string
    assert result[0].line is None  # explicit None


def test_pr_review_comment_from_payload_line_none() -> None:
    """from_payload handles line=null correctly."""
    data: dict[str, object] = {
        "id": 1,
        "body": "comment",
        "user": {"login": "u"},
        "created_at": "",
        "path": "file.py",
        "line": None,
        "commit_id": "c1",
    }
    comment = PRReviewComment.from_payload(data)
    assert comment.line is None
    assert comment.path == "file.py"
