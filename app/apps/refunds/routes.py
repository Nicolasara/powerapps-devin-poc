import uuid
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.platform import events
from app.platform.auth import require
from app.platform.db import get_db
from app.platform.models import Approval, AuditEvent, User, utcnow
from app.platform.tables import page_url_factory, resolve_sort, sort_url_factory
from app.web import render

from . import service
from .models import REASON_CODES, RefundRequest, Transaction

router = APIRouter(prefix="/refunds")

Db = Annotated[Session, Depends(get_db)]
Reader = Annotated[User, Depends(require("refunds.read"))]
Requester = Annotated[User, Depends(require("refunds.request"))]

PAGE_SIZE = 20

STATUS_BADGES = {
    "requested": "badge-gray",
    "pending_approval": "badge-amber",
    "executed": "badge-green",
    "rejected": "badge-red",
    "failed": "badge-red",
}


def _ctx(**extra) -> dict:
    return {"money": service.fmt_money, "status_badges": STATUS_BADGES, **extra}


def _redirect(path: str, **params) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def _get_tx(db: Session, tx_id: int) -> Transaction:
    tx = db.get(Transaction, tx_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    return tx


@router.get("")
def index(request: Request, db: Db, user: Reader, q: str = "", page: int = 1, view: str = "transactions",
          sort: str = "", dir: str = ""):
    service.sync_rejections(db)
    start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    refunded_today = db.scalar(
        select(func.coalesce(func.sum(RefundRequest.amount_cents), 0)).where(
            RefundRequest.status == "executed", RefundRequest.executed_at >= start_of_day
        )
    )
    pending = db.scalar(select(func.count()).select_from(RefundRequest).where(RefundRequest.status == "pending_approval"))
    failed = db.scalar(select(func.count()).select_from(RefundRequest).where(RefundRequest.status == "failed"))

    q = q.strip()
    view = view if view in ("transactions", "requests") else "transactions"
    like = f"%{q}%"
    if view == "requests":
        stmt = select(RefundRequest).join(Transaction)
        if q:
            stmt = stmt.where(or_(Transaction.customer_email.ilike(like), Transaction.customer_name.ilike(like)))
        allowed = {"requested": (RefundRequest.created_at,), "amount": (RefundRequest.amount_cents,),
                   "status": (RefundRequest.status,), "reason": (RefundRequest.reason_code,),
                   "by": (RefundRequest.requested_by_email,)}
        sort, dir, order = resolve_sort(sort, dir, allowed, "requested", "desc")
        tiebreak = RefundRequest.id.desc()
    else:
        stmt = select(Transaction)
        if q:
            stmt = stmt.where(or_(Transaction.customer_email.ilike(like), Transaction.customer_name.ilike(like)))
        allowed = {"date": (Transaction.created_at,), "customer": (Transaction.customer_name,),
                   "merchant": (Transaction.merchant,), "amount": (Transaction.amount_cents,)}
        sort, dir, order = resolve_sort(sort, dir, allowed, "date", "desc")
        tiebreak = Transaction.id.desc()
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    rows = db.scalars(stmt.order_by(*order, tiebreak).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    params = {"view": view if view != "transactions" else None, "q": q, "sort": sort, "dir": dir}
    return render(request, "refunds/index.html", user, **_ctx(
        refunded_today=refunded_today, pending=pending, failed=failed, q=q, view=view, total=total,
        txs=rows if view == "transactions" else [], requests=rows if view == "requests" else [],
        page=page, pages=pages, page_url=page_url_factory("/refunds", params),
        sort=sort, dir=dir, sort_url=sort_url_factory("/refunds", params, sort, dir),
        view_url=lambda v, search=q: "/refunds?" + urlencode(
            {k: x for k, x in {"view": v if v != "transactions" else None, "q": search}.items() if x}),
        approve_threshold=service.APPROVE_THRESHOLD_CENTS, large_threshold=service.LARGE_THRESHOLD_CENTS,
    ))


def _tx_page(request: Request, db: Session, user: User, tx: Transaction, error: str | None = None,
             form: dict | None = None, status_code: int = 200):
    service.sync_rejections(db)
    resp = render(request, "refunds/tx_detail.html", user, **_ctx(
        tx=tx, refunds=tx.refunds, available=service.available_cents(db, tx),
        reserved=service.reserved_cents(db, tx), kyc_hint=service.kyc_hint(db, tx.customer_email),
        kyc_link=service.kyc_case_link(db, tx.customer_email),
        reason_codes=REASON_CODES, idempotency_key=(form or {}).get("idempotency_key") or str(uuid.uuid4()),
        form=form or {}, error=error, approve_threshold=service.APPROVE_THRESHOLD_CENTS,
        large_threshold=service.LARGE_THRESHOLD_CENTS,
    ))
    resp.status_code = status_code
    return resp


@router.get("/tx/{tx_id}")
def tx_detail(tx_id: int, request: Request, db: Db, user: Reader):
    return _tx_page(request, db, user, _get_tx(db, tx_id))


@router.post("/requests")
def create_request(request: Request, db: Db, user: Requester, transaction_id: Annotated[int, Form()],
                   amount: Annotated[str, Form()], reason_code: Annotated[str, Form()],
                   idempotency_key: Annotated[str, Form()], note: Annotated[str, Form()] = ""):
    idempotency_key = idempotency_key.strip()[:64]
    existing = service.find_by_key(db, idempotency_key) if idempotency_key else None
    if existing is not None:
        return _redirect(f"/refunds/requests/{existing.id}", msg="Duplicate submission ignored: this refund was already submitted.")

    tx = _get_tx(db, transaction_id)
    form = {"amount": amount, "reason_code": reason_code, "note": note, "idempotency_key": idempotency_key}
    try:
        amount_cents = service.parse_amount(amount)
        rr = service.create_request(db, user, tx, amount_cents, reason_code, note, idempotency_key)
        db.commit()
    except service.RefundError as exc:
        db.rollback()
        return _tx_page(request, db, user, tx, error=str(exc), form=form, status_code=400)
    except IntegrityError:
        # Two concurrent submits with the same key: the unique constraint picks a winner.
        db.rollback()
        existing = service.find_by_key(db, idempotency_key)
        if existing is None:
            raise
        return _redirect(f"/refunds/requests/{existing.id}", msg="Duplicate submission ignored: this refund was already submitted.")
    events.dispatch_pending(db)

    messages = {
        "executed": "Refund executed with the processor.",
        "pending_approval": f"Refund submitted for approval ({rr.required_permission}).",
        "failed": "Refund failed at the processor. See details below.",
    }
    return _redirect(f"/refunds/requests/{rr.id}", msg=messages.get(rr.status, "Refund requested."))


@router.get("/requests/{request_id}")
def request_detail(request_id: int, request: Request, db: Db, user: Reader):
    service.sync_rejections(db)
    rr = db.get(RefundRequest, request_id)
    if rr is None:
        raise HTTPException(status_code=404, detail="refund request not found")
    approval = db.get(Approval, rr.approval_id) if rr.approval_id else None
    requested_by = db.get(User, approval.requested_by) if approval else None
    decided_by = db.get(User, approval.decided_by) if approval and approval.decided_by else None
    trail = db.scalars(
        select(AuditEvent).where(
            or_(
                (AuditEvent.entity_type == "refund_request") & (AuditEvent.entity_id == str(rr.id)),
                (AuditEvent.entity_type == "approval") & (AuditEvent.entity_id == str(rr.approval_id or "")),
            )
        ).order_by(AuditEvent.id)
    ).all()
    return render(request, "refunds/request_detail.html", user, **_ctx(
        rr=rr, tx=rr.transaction, approval=approval, requested_by=requested_by, decided_by=decided_by, trail=trail,
    ))
