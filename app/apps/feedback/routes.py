import os
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform import events
from app.platform.audit import audit
from app.platform.auth import current_user
from app.platform.db import get_db
from app.platform.models import User
from app.web import render

from .models import KINDS, FeedbackTicket

router = APIRouter(prefix="/feedback")


def github_repo() -> str:
    return os.environ.get("GITHUB_REPO", "Nicolasara/powerapps-devin-poc")


def github_issue_url(ticket: FeedbackTicket, reporter_role: str) -> str:
    body = "\n".join([
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
    query = urlencode({"title": f"[{ticket.kind}] {ticket.title}", "body": body})
    return f"https://github.com/{github_repo()}/issues/new?{query}"


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
    db.commit()
    events.dispatch_pending(db)
    return RedirectResponse(f"/feedback/{t.id}?msg=Thanks%2C+feedback+recorded", status_code=303)


@router.get("/{ticket_id}")
def detail(ticket_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    t = _get(db, ticket_id)
    reporter = db.scalar(select(User).where(User.email == t.submitted_by_email))
    reporter_role = reporter.role if reporter else "unknown"
    return render(request, "feedback/detail.html", user, ticket=t, reporter=reporter, reporter_role=reporter_role,
                  issue_url=github_issue_url(t, reporter_role), repo=github_repo())


@router.post("/{ticket_id}/mark-filed")
def mark_filed(ticket_id: int, github_issue_url: str = Form(...), db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    t = _get(db, ticket_id)
    url = github_issue_url.strip()
    if not url.startswith(("https://", "http://")):
        return RedirectResponse(f"/feedback/{t.id}?error=Enter+the+full+issue+URL", status_code=303)
    before = {"status": t.status, "github_issue_url": t.github_issue_url}
    t.github_issue_url = url
    t.status = "filed"
    audit(db, user, "feedback.filed", "feedback", t.id, before=before,
          after={"status": t.status, "github_issue_url": t.github_issue_url})
    db.commit()
    events.dispatch_pending(db)
    return RedirectResponse(f"/feedback/{t.id}?msg=Linked+to+GitHub+issue", status_code=303)
