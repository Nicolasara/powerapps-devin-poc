"""App shell: login, home, cross-app approvals inbox, audit log, notifications. Apps plug in as routers."""
from contextlib import asynccontextmanager
from urllib.parse import quote_plus, urlencode

from fastapi import Depends, FastAPI, Form, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.apps import feedback, flags, refunds
from app.apps.feedback import models as feedback_models
from app.apps.feedback.routes import router as feedback_router
from app.apps.flags import models as flags_models
from app.apps.flags.routes import router as flags_router
from app.apps.refunds import models as refunds_models
from app.apps.refunds import service as refunds_service
from app.apps.refunds.routes import router as refunds_router
from app.platform import events, rbac
from app.platform.approvals import ApprovalError, decide
from app.platform.auth import (
    clear_session,
    current_user,
    current_user_optional,
    require,
    set_session,
)
from app.platform.db import Base, SessionLocal, engine, get_db
from app.platform.models import Approval, AuditEvent, Notification, User, utcnow
from app.web import render, templates

# Every app's models must be imported so Base.metadata knows all tables before create_all().
MODEL_MODULES = (flags_models, refunds_models, feedback_models)
_registered = False


def register_apps() -> None:
    """Wire approval appliers and event subscribers once per process."""
    global _registered
    if _registered:
        return
    flags.register()
    refunds.register()
    feedback.register()
    _registered = True


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    register_apps()
    yield


app = FastAPI(title="Internal Tools (Power Apps replacement POC)", lifespan=lifespan)
register_apps()  # also at import so TestClient without the lifespan context still works
app.include_router(flags_router)
app.include_router(refunds_router)
app.include_router(feedback_router)


def _pending_approvals_count() -> int:
    db = SessionLocal()
    try:
        return db.scalar(select(func.count()).select_from(Approval).where(Approval.status == "pending")) or 0
    except SQLAlchemyError:  # e.g. tables not created yet
        return 0
    finally:
        db.close()


templates.env.globals["pending_approvals_count"] = _pending_approvals_count


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


@app.exception_handler(StarletteHTTPException)
async def html_aware_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if _wants_html(request):
        if exc.status_code == 401:
            return RedirectResponse(f"/login?next={quote_plus(str(request.url.path))}", status_code=303)
        if exc.status_code in (403, 404):
            db = SessionLocal()
            try:
                user = current_user_optional(request, db)
                resp = render(request, "error.html", user, status_code=exc.status_code, detail=exc.detail)
                resp.status_code = exc.status_code
                return resp
            finally:
                db.close()
    return await http_exception_handler(request, exc)


# ---------------------------------------------------------------- login (dev stand-in for Entra ID SSO)

@app.get("/login")
def login_form(request: Request, next: str = "/", db: Session = Depends(get_db),
               user: User | None = Depends(current_user_optional)):
    users = db.scalars(select(User).order_by(User.id)).all()
    role_perms = {u.id: sorted(rbac.permissions_for(u.role)) for u in users}
    return render(request, "login.html", user, users=users, role_perms=role_perms, next=next)


@app.post("/login")
def login(user_id: int = Form(...), next: str = Form("/"), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if u is None:
        return RedirectResponse("/login?error=Unknown+user", status_code=303)
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(target, status_code=303)
    set_session(resp, u)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    clear_session(resp)
    return resp


# ---------------------------------------------------------------- home

@app.get("/")
def home(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Dashboard: shell-level summary of every app (the shell may read app tables; apps may not read each other's)."""
    Flag, RefundRequest, FeedbackTicket = flags_models.Flag, refunds_models.RefundRequest, feedback_models.FeedbackTicket

    def count(stmt) -> int:
        return db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    refunded_today = db.scalar(
        select(func.coalesce(func.sum(RefundRequest.amount_cents), 0)).where(
            RefundRequest.status == "executed", RefundRequest.executed_at >= start_of_day
        )
    ) or 0
    recent = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(10)).all()
    return render(
        request, "home.html", user, now=utcnow(), recent=recent,
        pending=count(select(Approval).where(Approval.status == "pending")),
        flag_count=count(select(Flag)), flags_on=count(select(Flag).where(Flag.enabled.is_(True), Flag.rollout_percent > 0)),
        refunded_today=refunds_service.fmt_money(refunded_today),
        refund_pending=count(select(RefundRequest).where(RefundRequest.status == "pending_approval")),
        open_feedback=count(select(FeedbackTicket).where(FeedbackTicket.status == "open")),
    )


# ---------------------------------------------------------------- approvals inbox (shared across apps)

@app.get("/approvals")
def approvals(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    pending = db.scalars(select(Approval).where(Approval.status == "pending").order_by(Approval.requested_at)).all()
    recent = db.scalars(select(Approval).where(Approval.status != "pending")
                        .order_by(Approval.decided_at.desc(), Approval.id.desc()).limit(20)).all()
    ids = {a.requested_by for a in [*pending, *recent]} | {a.decided_by for a in recent if a.decided_by}
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(ids))).all()} if ids else {}
    rows = []
    for a in pending:
        if a.requested_by == user.id:
            blocked = "You requested this change (maker cannot be checker)"
        elif not rbac.has_permission(user.role, a.required_permission):
            blocked = f"Requires {a.required_permission}"
        else:
            blocked = None
        rows.append({"a": a, "blocked": blocked})
    return render(request, "approvals.html", user, rows=rows, recent=recent, users=users, pending_count=len(pending))


@app.post("/approvals/{approval_id}/decide")
def decide_approval(approval_id: int, decision: str = Form(...), reason: str = Form(""),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = db.get(Approval, approval_id)
    if a is None:
        return RedirectResponse("/approvals?error=Approval+not+found", status_code=303)
    if decision not in ("approve", "reject"):
        return RedirectResponse("/approvals?error=Invalid+decision", status_code=303)
    try:
        decide(db, user, a, approve=decision == "approve", reason=reason.strip() or None)
    except ApprovalError as e:
        db.rollback()
        return RedirectResponse(f"/approvals?error={quote_plus(f'Approval #{approval_id}: {e}')}", status_code=303)
    db.commit()
    events.dispatch_pending(db)
    verb = "approved" if decision == "approve" else "rejected"
    return RedirectResponse(f"/approvals?msg={quote_plus(f'Approval #{approval_id} {verb}')}", status_code=303)


# ---------------------------------------------------------------- audit log

AUDIT_PAGE_SIZE = 50


@app.get("/audit")
@app.get("/audit")
def audit_log(request: Request, entity_type: str = "", page: int = 1, user: User = Depends(require("audit.read")),
              db: Session = Depends(get_db)):
    base = select(AuditEvent)
    if entity_type:
        base = base.where(AuditEvent.entity_type == entity_type)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    pages = max(1, (total + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)
    page = min(max(1, page), pages)
    rows = db.scalars(
        base.order_by(AuditEvent.id.desc()).offset((page - 1) * AUDIT_PAGE_SIZE).limit(AUDIT_PAGE_SIZE)
    ).all()
    entity_types = db.scalars(select(AuditEvent.entity_type).distinct().order_by(AuditEvent.entity_type)).all()

    def page_url(n: int) -> str:
        return "/audit?" + urlencode({k: v for k, v in {"entity_type": entity_type, "page": n}.items() if v})

    return render(request, "audit.html", user, rows=rows, entity_type=entity_type, entity_types=entity_types,
                  page=page, pages=pages, total=total, page_url=page_url, page_size=AUDIT_PAGE_SIZE)


# ---------------------------------------------------------------- notifications (fake Teams/Slack sink)

@app.get("/notifications")
def notifications(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Notification).order_by(Notification.id.desc()).limit(200)).all()
    return render(request, "notifications.html", user, rows=rows)
