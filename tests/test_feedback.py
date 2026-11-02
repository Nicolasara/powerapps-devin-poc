from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from app.apps.feedback.models import FeedbackTicket
from app.platform.models import AuditEvent


def _create(client, title: str, **extra):
    data = {"title": title, "body": "Takes three clicks to find the charge.", "kind": "friction",
            "page_path": "/refunds", **extra}
    return client.post("/feedback", data=data, follow_redirects=False)


def test_create_ticket(login, db):
    resp = _create(login("oscar"), "Refund search is slow & clunky")
    assert resp.status_code == 303
    ticket = db.scalar(select(FeedbackTicket))
    assert ticket is not None
    assert resp.headers["location"].startswith(f"/feedback/{ticket.id}")
    assert ticket.title == "Refund search is slow & clunky"
    assert ticket.status == "open"
    assert ticket.page_path == "/refunds"
    assert ticket.submitted_by_email == "oscar@corp.test"
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "feedback.created")) is not None

    queue = login("alice").get("/feedback")
    assert queue.status_code == 200
    assert "Refund search is slow &amp; clunky" in queue.text


def test_blank_title_is_rejected(login, db):
    resp = _create(login("oscar"), "   ")
    assert resp.status_code == 303
    assert "error" in parse_qs(urlparse(resp.headers["location"]).query)
    assert db.scalar(select(FeedbackTicket)) is None


def test_anonymous_cannot_file_feedback(anon, db):
    assert _create(anon, "hello").status_code == 401
    assert db.scalar(select(FeedbackTicket)) is None


def test_operators_see_no_github_controls(login):
    oscar = login("oscar")
    location = _create(oscar, "Refund search is slow & clunky").headers["location"]
    page = oscar.get(location).text
    assert "/file" not in page and "issues/new" not in page
    assert "What happens next" in page


def test_admin_files_issue_with_devin_label(login, db, monkeypatch):
    from app.platform.github import FakeIssueTracker
    monkeypatch.setenv("GITHUB_REPO", "acme/internal-tools")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    FakeIssueTracker.issues.clear()
    oscar = login("oscar")
    title = "Add card last-4 search"
    location = _create(oscar, title).headers["location"]
    ticket_id = int(urlparse(location).path.rsplit("/", 1)[-1])
    assert oscar.post(f"/feedback/{ticket_id}/file", follow_redirects=False).status_code == 403  # triage is admin-only

    resp = login("adam").post(f"/feedback/{ticket_id}/file", follow_redirects=False)
    assert resp.status_code == 303 and "issue+created" in resp.headers["location"]
    issue = FakeIssueTracker.issues[-1]
    assert issue["repo"] == "acme/internal-tools" and issue["title"] == f"[friction] {title}"
    assert "devin" in issue["labels"] and "/refunds" in issue["body"] and "operator" in issue["body"]
    db.expire_all()
    ticket = db.get(FeedbackTicket, ticket_id)
    assert ticket.status == "filed" and ticket.github_issue_url == f"https://github.com/acme/internal-tools/issues/{issue['number']}"

    again = login("adam").post(f"/feedback/{ticket_id}/file", follow_redirects=False)
    assert "Already+filed" in again.headers["location"] and len(FakeIssueTracker.issues) == 1


def test_auto_file_on_submit(login, db, monkeypatch):
    from app.platform.github import FakeIssueTracker
    monkeypatch.setenv("FEEDBACK_AUTO_FILE", "1")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    FakeIssueTracker.issues.clear()
    location = _create(login("oscar"), "Auto filed").headers["location"]
    ticket_id = int(urlparse(location).path.rsplit("/", 1)[-1])
    db.expire_all()
    assert db.get(FeedbackTicket, ticket_id).status == "filed"
    assert FakeIssueTracker.issues[-1]["title"] == "[friction] Auto filed"


def test_github_failure_keeps_ticket(login, db, monkeypatch):
    from app.platform import github

    class Broken:
        def create_issue(self, *a, **k):
            raise github.IssueTrackerError("boom")
    monkeypatch.setattr("app.apps.feedback.routes.get_issue_tracker", lambda: Broken())
    location = _create(login("oscar"), "Fragile").headers["location"]
    ticket_id = int(urlparse(location).path.rsplit("/", 1)[-1])
    resp = login("adam").post(f"/feedback/{ticket_id}/file", follow_redirects=False)
    assert "GitHub+refused" in resp.headers["location"]
    db.expire_all()
    assert db.get(FeedbackTicket, ticket_id).status == "open"
