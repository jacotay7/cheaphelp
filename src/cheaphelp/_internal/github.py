"""A small synchronous GitHub REST API client built on httpx.

Only the endpoints cheaphelp actually needs are implemented. Authentication is a
personal access token (classic or fine-grained) passed as a bearer token.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

import httpx

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

    def __init__(self, token: str, *, root: str = API_ROOT, timeout: float = 30.0) -> None:
        if not token:
            raise GitHubError("A GitHub token is required (set GITHUB_TOKEN).")
        self._client = httpx.Client(
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

    # --- low level ---------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= _HTTP_ERROR:
            detail = response.text
            with contextlib.suppress(Exception):
                detail = response.json().get("message", detail)
            raise GitHubError(f"{method} {path} -> {response.status_code}: {detail}")
        if response.status_code == _HTTP_NO_CONTENT or not response.content:
            return None
        return response.json()

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
