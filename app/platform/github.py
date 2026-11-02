"""Issue tracker adapter. GitHub is an external system, so it sits behind a Protocol with a fake for dev and tests;
the real client is used only when GITHUB_TOKEN is set. Issues carry a label (default `devin`) so an automation can
pick them up and open the pull request."""
import os
from typing import ClassVar, Protocol

import httpx


class IssueTrackerError(RuntimeError):
    pass


class IssueTracker(Protocol):
    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> str: ...


class FakeIssueTracker:
    """Records issues in memory and hands back a plausible URL. Tests and the dev server use this."""
    issues: ClassVar[list[dict]] = []

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> str:
        number = len(self.issues) + 100
        self.issues.append({"repo": repo, "number": number, "title": title, "body": body, "labels": labels})
        return f"https://github.com/{repo}/issues/{number}"


class GitHubIssueTracker:
    def __init__(self, token: str, api_base: str = "https://api.github.com"):
        self.token = token
        self.api_base = api_base.rstrip("/")

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> str:
        try:
            resp = httpx.post(
                f"{self.api_base}/repos/{repo}/issues",
                json={"title": title, "body": body, "labels": labels},
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"},
                timeout=15,
            )
        except httpx.HTTPError as e:
            raise IssueTrackerError(f"GitHub unreachable: {e.__class__.__name__}") from e
        if resp.status_code != 201:
            raise IssueTrackerError(f"GitHub returned {resp.status_code}: {resp.text[:200]}")
        return resp.json()["html_url"]


def issue_label() -> str:
    return os.environ.get("DEVIN_ISSUE_LABEL", "devin")


def get_issue_tracker() -> IssueTracker:
    token = os.environ.get("GITHUB_TOKEN")
    return GitHubIssueTracker(token) if token else FakeIssueTracker()
