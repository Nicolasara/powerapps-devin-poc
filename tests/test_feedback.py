import html
import re
from urllib.parse import parse_qs, quote_plus, urlparse

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


def test_detail_links_to_prefilled_github_issue_for_admins(login, monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "acme/internal-tools")
    oscar = login("oscar")
    title = "Refund search is slow & clunky"
    location = _create(oscar, title).headers["location"]
    assert "issues/new" not in oscar.get(location).text  # operators file tickets here, not on GitHub

    detail = login("adam").get(location)
    assert detail.status_code == 200

    page = html.unescape(detail.text)
    expected = f"https://github.com/acme/internal-tools/issues/new?title={quote_plus('[friction] ' + title)}"
    assert expected in page

    url = re.search(r'href="(https://github\.com/[^"]+/issues/new\?[^"]+)"', page).group(1)
    params = parse_qs(urlparse(url).query)
    assert params["title"] == [f"[friction] {title}"]
    assert "/refunds" in params["body"][0]
    assert "operator" in params["body"][0]


def test_mark_filed_links_issue(login, db):
    oscar = login("oscar")
    location = _create(oscar, "Add card last-4 search").headers["location"]
    ticket_id = int(urlparse(location).path.rsplit("/", 1)[-1])
    assert oscar.post(f"/feedback/{ticket_id}/mark-filed", data={"github_issue_url": "https://x/1"},
                      follow_redirects=False).status_code == 403  # triage is admin-only
    resp = login("adam").post(f"/feedback/{ticket_id}/mark-filed",
                      data={"github_issue_url": "https://github.com/acme/internal-tools/issues/7"},
                      follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    ticket = db.get(FeedbackTicket, ticket_id)
    assert ticket.status == "filed"
    assert ticket.github_issue_url == "https://github.com/acme/internal-tools/issues/7"
