import os
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform import events
from app.platform.audit import audit
from app.platform.auth import current_user, require
from app.platform.db import get_db
from app.platform.github import IssueTrackerError, get_issue_tracker, issue_label
from app.platform.models import User
from app.web import render

from .models import KINDS, FeedbackTicket

router = APIRouter(prefix="/feedback")


def github_repo() -> str:
    return os.environ.get("GITHUB_REPO", "Nicolasara/powerapps-devin-poc")


def auto_file_enabled() -> bool:
    return os.environ.get("FEEDBACK_AUTO_FILE", "").lower() in ("1", "true", "yes")


def issue_body(ticket: FeedbackTicket, reporter_role: str) -> str:
    return "\n".join([
        f"**Page:** `{ticket.page_path or '(not specified)'}`",
        f"**Kind:** {ticket.kind}",
        f"**Reporter role:** {reporter_role}",
        "",
        "**Description**",
        "",
        ticket.body or "(no description)",
        "",
        "---",
        f"Filed from the internal tools feedback queue (ticket #{ticket.id}).",
    ])


def _reporter_role(db: Session, ticket: FeedbackTicket) -> str:
    reporter = db.scalar(select(User).where(User.email == ticket.submitted_by_email))
    return reporter.role if reporter else "unknown"


def file_to_github(db: Session, ticket: FeedbackTicket, actor: User | None) -> str:
    """Create the GitHub issue for a ticket through the adapter, link it and audit it. Idempotent per ticket."""
    if ticket.github_issue_url:
        return ticket.github_issue_url
    label = issue_label()
    url = get_issue_tracker().create_issue(github_repo(), f"[{ticket.kind}] {ticket.title}",
                                           issue_body(ticket, _reporter_role(db, ticket)), [label, ticket.kind])
    before = {"status": ticket.status, "github_issue_url": ticket.github_issue_url}
    ticket.github_issue_url = url
    ticket.status = "filed"
    audit(db, actor, "feedback.filed", "feedback", ticket.id, before=before,
          after={"status": ticket.status, "github_issue_url": url, "label": label},
          reason="auto-filed on submission" if actor is None else None)
    return url


def _get(db: Session, ticket_id: int) -> FeedbackTicket:
    t = db.get(FeedbackTicket, ticket_id)
    if t is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return t


@router.get("")
def queue(request: Request, status: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = select(FeedbackTicket).order_by(FeedbackTicket.created_at.desc(), FeedbackTicket.id.desc())
    if status:
        stmt = stmt.where(FeedbackTicket.status == status)
    tickets = db.scalars(stmt).all()
    return render(request, "feedback/list.html", user, tickets=tickets, status=status or "")


@router.get("/new")
def new_form(request: Request, page: str = "", user: User = Depends(current_user)):
    return render(request, "feedback/new.html", user, page=page, kinds=KINDS)


@router.post("")
def create(title: str = Form(...), body: str = Form(""), kind: str = Form("friction"), page_path: str = Form(""),
           db: Session = Depends(get_db), user: User = Depends(current_user)):
    title = title.strip()
    if not title:
        return RedirectResponse(f"/feedback/new?{urlencode({'page': page_path, 'error': 'Title is required'})}", status_code=303)
    if kind not in KINDS:
        kind = "friction"
    t = FeedbackTicket(title=title, body=body.strip(), kind=kind, page_path=page_path.strip(),
                       submitted_by_email=user.email, status="open")
    db.add(t)
    db.flush()
    audit(db, user, "feedback.created", "feedback", t.id, after={"title": t.title, "kind": t.kind, "page": t.page_path})
    msg = "Thanks, feedback recorded"
    if auto_file_enabled():
        try:
            file_to_github(db, t, None)
            msg = "Thanks, feedback recorded and filed to GitHub"
        except IssueTrackerError as e:
            audit(db, None, "feedback.file_failed", "feedback", t.id, after={"error": str(e)[:200]})
    db.commit()
    events.dispatch_pending(db)
    return RedirectResponse(f"/feedback/{t.id}?{urlencode({'msg': msg})}", status_code=303)


@router.get("/{ticket_id}")
def detail(ticket_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    t = _get(db, ticket_id)
    reporter = db.scalar(select(User).where(User.email == t.submitted_by_email))
    return render(request, "feedback/detail.html", user, ticket=t, reporter=reporter,
                  reporter_role=_reporter_role(db, t), repo=github_repo(), label=issue_label(),
                  auto_file=auto_file_enabled())


@router.post("/{ticket_id}/file")
def file_issue(ticket_id: int, db: Session = Depends(get_db), user: User = Depends(require("admin"))):
    """Platform-team triage: one click creates the GitHub issue (labelled so Devin picks it up) and links it."""
    t = _get(db, ticket_id)
    if t.github_issue_url:
        return RedirectResponse(f"/feedback/{t.id}?notice=Already+filed", status_code=303)
    try:
        file_to_github(db, t, user)
    except IssueTrackerError as e:
        db.rollback()
        return RedirectResponse(f"/feedback/{t.id}?{urlencode({'error': f'GitHub refused the issue: {e}'})}", status_code=303)
    db.commit()
    events.dispatch_pending(db)
    return RedirectResponse(f"/feedback/{t.id}?msg=GitHub+issue+created", status_code=303)
