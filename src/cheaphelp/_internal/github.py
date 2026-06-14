"""A small synchronous GitHub REST API client built on httpx.

Only the endpoints cheaphelp actually needs are implemented. Authentication is a
personal access token (classic or fine-grained) passed as a bearer token.
"""

from __future__ import annotations

import contextlib
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

_LOG = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "cheaphelp"

_HTTP_NO_CONTENT = 204
_HTTP_ERROR = 400
_PER_PAGE = 100


class GitHubError(RuntimeError):
    """Raised when the GitHub API returns an error response."""


@dataclass
class Issue:
    """A subset of the GitHub issue payload that we care about."""

    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    user: str
    html_url: str

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> Issue:
        return cls(
            number=int(data["number"]),
            title=data.get("title") or "",
            body=data.get("body") or "",
            state=data.get("state") or "open",
            labels=[label["name"] for label in data.get("labels", [])],
            user=(data.get("user") or {}).get("login", ""),
            html_url=data.get("html_url", ""),
        )

    @property
    def is_pull_request(self) -> bool:
        # The issues endpoint also returns PRs; they carry a pull_request key.
        return False


@dataclass
class Comment:
    """A single issue comment."""

    id: int
    body: str
    user: str
    created_at: str

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> Comment:
        return cls(
            id=int(data["id"]),
            body=data.get("body") or "",
            user=(data.get("user") or {}).get("login", ""),
            created_at=data.get("created_at", ""),
        )


class GitHubClient:
    """Thin wrapper over the GitHub REST API.

    Use as a context manager so the underlying httpx client is closed::

        with GitHubClient(token) as gh:
            gh.list_open_issues("owner", "repo")
    """

    def __init__(
        self,
        token: str,
        *,
        root: str = API_ROOT,
        timeout: float = 30.0,
        retry_attempts: int = 3,
        retry_base_delay: float = 1.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not token:
            raise GitHubError("A GitHub token is required (set GITHUB_TOKEN).")
        self._retry_attempts = int(retry_attempts)
        self._retry_base_delay = float(retry_base_delay)
        self._client = client or httpx.Client(
            base_url=root,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
        )

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _backoff_delay(self, attempt: int) -> float:
        """Return the backoff for `attempt` (1-indexed) with ±25% jitter."""
        base = self._retry_base_delay * (2 ** (attempt - 1))
        jitter = base * 0.25 * (2 * random.random() - 1)
        return max(0.0, base + jitter)

    @staticmethod
    def _parse_retry_after(header: str | None) -> float:
        """Parse a Retry-After header (seconds), capped at 60s. 0.0 if absent/invalid."""
        if not header:
            return 0.0
        try:
            seconds = float(header)
        except (TypeError, ValueError):
            return 0.0
        return min(max(0.0, seconds), 60.0)

    # --- low level ---------------------------------------------------------
    def _error_for(self, method: str, path: str, response: httpx.Response) -> GitHubError:
        """Build a GitHubError from a non-success response."""
        detail = response.text
        with contextlib.suppress(Exception):
            detail = response.json().get("message", detail)
        return GitHubError(f"{method} {path} -> {response.status_code}: {detail}")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(1, self._retry_attempts + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                if attempt >= self._retry_attempts:
                    raise
                delay = self._backoff_delay(attempt)
                _LOG.warning(
                    "%s %s: attempt %d/%d failed: %s, retrying in %.1fs",
                    method,
                    path,
                    attempt,
                    self._retry_attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._retry_attempts:
                    raise self._error_for(method, path, response)
                retry_after = (
                    self._parse_retry_after(response.headers.get("Retry-After")) if response.status_code == 429 else 0.0
                )
                delay = max(self._backoff_delay(attempt), retry_after)
                _LOG.warning(
                    "%s %s: attempt %d/%d failed: %d, retrying in %.1fs",
                    method,
                    path,
                    attempt,
                    self._retry_attempts,
                    response.status_code,
                    delay,
                )
                time.sleep(delay)
                continue

            if response.status_code >= _HTTP_ERROR:
                raise self._error_for(method, path, response)

            if response.status_code == _HTTP_NO_CONTENT or not response.content:
                return None
            return response.json()
        raise RuntimeError("retry loop exited without return")  # pragma: no cover

    def _paginate(self, path: str, **params: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request(
                "GET",
                path,
                params={**params, "per_page": _PER_PAGE, "page": page},
            )
            if not batch:
                break
            items.extend(batch)
            if len(batch) < _PER_PAGE:
                break
            page += 1
        return items

    # --- high level --------------------------------------------------------
    def authenticated_login(self) -> str:
        """Return the login of the token's owner (also validates the token)."""
        data = self._request("GET", "/user")
        return data["login"]

    def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        """Return repository metadata (default_branch, private, clone_url, ...)."""
        return self._request("GET", f"/repos/{owner}/{repo}")

    def list_open_issues(self, owner: str, repo: str) -> list[Issue]:
        """List open issues (pull requests are filtered out)."""
        raw = self._paginate(f"/repos/{owner}/{repo}/issues", state="open")
        return [Issue.from_payload(item) for item in raw if "pull_request" not in item]

    def get_issue(self, owner: str, repo: str, number: int) -> Issue:
        data = self._request("GET", f"/repos/{owner}/{repo}/issues/{number}")
        return Issue.from_payload(data)

    def create_issue(self, owner: str, repo: str, *, title: str, body: str = "") -> Issue:
        data = self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues",
            json={"title": title, "body": body},
        )
        return Issue.from_payload(data)

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[Comment]:
        raw = self._paginate(f"/repos/{owner}/{repo}/issues/{number}/comments")
        return [Comment.from_payload(item) for item in raw]

    def create_comment(self, owner: str, repo: str, number: int, body: str) -> Comment:
        data = self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
        )
        return Comment.from_payload(data)

    def add_labels(self, owner: str, repo: str, number: int, labels: list[str]) -> None:
        self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/labels",
            json={"labels": labels},
        )

    def remove_label(self, owner: str, repo: str, number: int, label: str) -> None:
        # 404 just means the label was not present; treat that as success.
        try:
            self._request("DELETE", f"/repos/{owner}/{repo}/issues/{number}/labels/{label}")
        except GitHubError as exc:
            if "404" not in str(exc):
                raise

    def ensure_label(
        self,
        owner: str,
        repo: str,
        name: str,
        *,
        color: str = "ededed",
        description: str = "",
    ) -> None:
        """Create a label if it does not already exist."""
        try:
            self._request("GET", f"/repos/{owner}/{repo}/labels/{name}")
        except GitHubError as exc:
            if "404" not in str(exc):
                raise
        else:
            return
        self._request(
            "POST",
            f"/repos/{owner}/{repo}/labels",
            json={"name": name, "color": color, "description": description},
        )

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str = "",
        draft: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
        )

    def request_reviewers(self, owner: str, repo: str, number: int, reviewers: list[str]) -> bool:
        """Request reviewers on a PR. Returns False if GitHub rejected the request.

        GitHub refuses to request a review from the PR's own author (a common case
        here, since the bot may be the repo owner); that is treated as a soft
        failure so the caller can fall back to an @mention.
        """
        if not reviewers:
            return False
        try:
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/pulls/{number}/requested_reviewers",
                json={"reviewers": reviewers},
            )
        except GitHubError:
            return False
        return True
