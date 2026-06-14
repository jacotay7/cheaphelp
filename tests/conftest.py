"""Shared fixtures and test doubles for the pytest test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cheaphelp._internal import pr_state
from cheaphelp._internal.github import Comment, GitHubClient, Issue, PRReviewComment

if TYPE_CHECKING:
    from pathlib import Path


def make_issue(
    number: int = 1,
    *,
    title: str = "t",
    body: str = "b",
    state: str = "open",
    labels: list[str] | None = None,
    user: str = "human",
    html_url: str = "",
) -> Issue:
    """Build an `Issue` with sensible test defaults; override via kwargs."""
    return Issue(
        number=number,
        title=title,
        body=body,
        state=state,
        labels=list(labels) if labels is not None else [],
        user=user,
        html_url=html_url,
    )


def make_comment(body: str, user: str, *, comment_id: int = 1, created_at: str = "") -> Comment:
    """Build a `Comment` with sensible test defaults; override via kwargs."""
    return Comment(id=comment_id, body=body, user=user, created_at=created_at)


def make_pr_state(issue_dir: Path, **overrides: Any) -> None:
    """Write a minimal `pr_state.json` to *issue_dir*, merging in *overrides*."""
    data: dict[str, Any] = {
        "pr_number": 42,
        "pr_url": "https://github.com/o/r/pull/42",
        "last_push_sha": "abc123",
        "reviewers": ["alice"],
        "rework_attempts": 0,
        **overrides,
    }
    pr_state.save_pr_state(issue_dir, data)


class FakeGitHubClient(GitHubClient):
    """In-memory `GitHubClient` double for tests.

    Subclasses the real client so instances satisfy type checks wherever a
    `GitHubClient` is expected, without any network I/O. Seed `issues`,
    `comments`, `labels`, `pull_requests`, `pr_reviews`, or
    `pr_review_comments` before use; every call is also recorded to `calls`
    as `(method_name, positional_args)`.
    """

    def __init__(self, *, login: str = "mybot") -> None:
        self.login = login
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.issues: dict[tuple[str, str], list[Issue]] = {}
        self.comments: dict[tuple[str, str, int], list[Comment]] = {}
        self.labels: dict[tuple[str, str, int], set[str]] = {}
        self.pull_requests: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.pr_reviews: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
        self.pr_review_comments: dict[tuple[str, str, int], list[PRReviewComment]] = {}
        self.created_prs: list[dict[str, Any]] = []
        self.requested_reviewers: list[tuple[str, str, int, list[str]]] = []
        self._next_comment_id = 1000

    def close(self) -> None:
        """No-op: there is no underlying httpx client to close."""

    def authenticated_login(self) -> str:
        self.calls.append(("authenticated_login", ()))
        return self.login

    def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        self.calls.append(("get_repo", (owner, repo)))
        return {"default_branch": "main"}

    def list_open_issues(self, owner: str, repo: str) -> list[Issue]:
        self.calls.append(("list_open_issues", (owner, repo)))
        return list(self.issues.get((owner, repo), []))

    def get_issue(self, owner: str, repo: str, number: int) -> Issue:
        self.calls.append(("get_issue", (owner, repo, number)))
        for issue in self.issues.get((owner, repo), []):
            if issue.number == number:
                return issue
        return make_issue(number=number, labels=sorted(self.labels.get((owner, repo, number), set())))

    def create_issue(self, owner: str, repo: str, *, title: str, body: str = "") -> Issue:
        self.calls.append(("create_issue", (owner, repo, title, body)))
        issue = make_issue(number=len(self.issues.get((owner, repo), [])) + 1, title=title, body=body)
        self.issues.setdefault((owner, repo), []).append(issue)
        return issue

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[Comment]:
        self.calls.append(("list_issue_comments", (owner, repo, number)))
        return list(self.comments.get((owner, repo, number), []))

    def create_comment(self, owner: str, repo: str, number: int, body: str) -> Comment:
        self.calls.append(("create_comment", (owner, repo, number, body)))
        comment = make_comment(body, self.login, comment_id=self._next_comment_id)
        self._next_comment_id += 1
        self.comments.setdefault((owner, repo, number), []).append(comment)
        return comment

    def add_labels(self, owner: str, repo: str, number: int, labels: list[str]) -> None:
        self.calls.append(("add_labels", (owner, repo, number, list(labels))))
        self.labels.setdefault((owner, repo, number), set()).update(labels)

    def remove_label(self, owner: str, repo: str, number: int, label: str) -> None:
        self.calls.append(("remove_label", (owner, repo, number, label)))
        self.labels.get((owner, repo, number), set()).discard(label)

    def ensure_label(self, owner: str, repo: str, name: str, *, color: str = "ededed", description: str = "") -> None:
        del color, description
        self.calls.append(("ensure_label", (owner, repo, name)))

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
        self.calls.append(("create_pull_request", (owner, repo, title, head, base)))
        number = len(self.created_prs) + 1
        pr: dict[str, Any] = {
            "number": number,
            "html_url": f"https://github.com/{owner}/{repo}/pull/{number}",
            "title": title,
            "body": body,
            "head": {"ref": head},
            "base": {"ref": base},
            "draft": draft,
            "state": "open",
        }
        self.created_prs.append(pr)
        self.pull_requests[(owner, repo, number)] = pr
        return pr

    def request_reviewers(self, owner: str, repo: str, number: int, reviewers: list[str]) -> bool:
        self.calls.append(("request_reviewers", (owner, repo, number, list(reviewers))))
        if not reviewers:
            return False
        self.requested_reviewers.append((owner, repo, number, list(reviewers)))
        return True

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        self.calls.append(("get_pull_request", (owner, repo, pr_number)))
        return self.pull_requests.get((owner, repo, pr_number), {"state": "open", "number": pr_number})

    def list_pr_reviews(self, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        self.calls.append(("list_pr_reviews", (owner, repo, pr_number)))
        return list(self.pr_reviews.get((owner, repo, pr_number), []))

    def list_pr_review_comments(self, owner: str, repo: str, pr_number: int) -> list[PRReviewComment]:
        self.calls.append(("list_pr_review_comments", (owner, repo, pr_number)))
        return list(self.pr_review_comments.get((owner, repo, pr_number), []))
