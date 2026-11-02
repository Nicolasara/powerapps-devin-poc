import json
import secrets
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.platform import events
from app.platform.audit import audit
from app.platform.auth import require
from app.platform.db import get_db
from app.platform.models import AuditEvent, User
from app.web import render

from . import service
from .models import STATUSES, KycCase

router = APIRouter(prefix="/kyc")

Db = Annotated[Session, Depends(get_db)]
Reader = Annotated[User, Depends(require("kyc.read"))]
Reviewer = Annotated[User, Depends(require("kyc.review"))]

RISK_BADGES = {"high": "badge-red", "medium": "badge-amber", "low": "badge-green"}
STATUS_BADGES = {"pending": "badge-amber", "approved": "badge-green", "rejected": "badge-red"}
VENDOR_BADGES = {"pass": "badge-green", "review": "badge-amber", "fail": "badge-red"}


def _ctx(**extra) -> dict:
    return {"risk_badges": RISK_BADGES, "status_badges": STATUS_BADGES, "vendor_badges": VENDOR_BADGES, **extra}


def _redirect(path: str, **params) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def _get(db: Session, case_id: int) -> KycCase:
    case = db.get(KycCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return case


PAGE_SIZE = 25
MAX_WEBHOOK_BYTES = 64 * 1024


@router.get("")
def queue(request: Request, db: Db, user: Reader, status: str = "pending", risk: str = "", q: str = "", page: int = 1):
    status = status if status in STATUSES or status == "all" else "pending"
    stmt = select(KycCase)
    if status != "all":
        stmt = stmt.where(KycCase.status == status)
    if risk in RISK_BADGES:
        stmt = stmt.where(KycCase.risk_band == risk)
    q = q.strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(KycCase.applicant_email.ilike(like) | KycCase.applicant_name.ilike(like))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    order = (KycCase.risk_score.desc(), KycCase.created_at.asc(), KycCase.id.asc()) if status == "pending" \
        else (KycCase.decided_at.desc(), KycCase.id.desc())
    cases = db.scalars(stmt.order_by(*order).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()

    def page_url(n: int) -> str:
        return "/kyc?" + urlencode({k: v for k, v in {"status": status, "risk": risk, "q": q, "page": n}.items() if v})

    counts = {s: db.scalar(select(func.count()).select_from(KycCase).where(KycCase.status == s)) or 0 for s in STATUSES}
    return render(request, "kyc/queue.html", user, **_ctx(cases=cases, status=status, risk=risk, q=q, counts=counts,
                                                          stats=service.stats(db), page=page, pages=pages, total=total,
                                                          page_url=page_url))


@router.get("/{case_id}")
def case_detail(request: Request, case_id: int, db: Db, user: Reader, error: str | None = None):
    case = _get(db, case_id)
    linked = service.linked_cases(db, case)
    trail = db.scalars(
        select(AuditEvent).where(AuditEvent.entity_type == "kyc_case", AuditEvent.entity_id == str(case.id))
        .order_by(AuditEvent.id.desc())
    ).all()
    docs = {k: service.document_url(case, k) for k in service.DOC_KINDS}
    return render(request, "kyc/case.html", user, **_ctx(case=case, linked=linked, trail=trail, error=error, docs=docs))


@router.get("/{case_id}/documents/{kind}")
def view_document(request: Request, case_id: int, kind: str, exp: int, sig: str, db: Db, user: Reviewer):
    """Renders the applicant's document through a short-lived signed link and logs who looked at it."""
    case = _get(db, case_id)
    if not service.document_link_valid(case, kind, exp, sig):
        raise HTTPException(status_code=403, detail="document link expired or invalid; reopen the case")
    audit(db, user, "kyc.document_viewed", "kyc_case", case.id, after={"kind": kind})
    db.commit()
    return render(request, "kyc/document.html", user, **_ctx(case=case, kind=kind, expires_at=exp))


@router.post("/{case_id}/decide")
def decide(case_id: int, decision: Annotated[str, Form()], db: Db, user: Reviewer,
           reason: Annotated[str, Form()] = ""):
    case = _get(db, case_id)
    if decision not in ("approve", "reject"):
        return _redirect(f"/kyc/{case.id}", error="decision must be approve or reject")
    try:
        service.decide(db, user, case, approve=(decision == "approve"), reason=reason)
    except service.KycError as e:
        db.rollback()
        return _redirect(f"/kyc/{case.id}", error=str(e))
    db.commit()
    events.dispatch_pending(db)
    return _redirect("/kyc", msg=f"Case #{case.id} {case.status}.")


# ---------------------------------------------------------------- vendor integration

@router.post("/webhook/vendor")
async def vendor_webhook(request: Request, db: Db):
    """Signed webhook from the KYC vendor. No user session: the HMAC signature is the authentication."""
    declared = request.headers.get("content-length") or "0"
    if not declared.isdigit():
        raise HTTPException(status_code=400, detail="invalid content-length")
    if int(declared) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    if not service.verify_signature(body, request.headers.get(service.SIGNATURE_HEADER)):
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        payload = json.loads(body)
        case, created = service.intake(db, payload)
    except (ValueError, service.KycError) as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from None
    db.commit()
    events.dispatch_pending(db)
    return JSONResponse({"case_id": case.id, "status": case.status, "risk_band": case.risk_band},
                        status_code=201 if created else 200)


SCENARIOS = {
    "clean": ("pass", {"document": "pass", "selfie": "pass", "sanctions": "pass", "address": "pass"}),
    "ring": ("pass", {"document": "pass", "selfie": "pass", "sanctions": "pass", "address": "pass"}),
    "vendor_review": ("review", {"document": "pass", "selfie": "review", "sanctions": "pass", "address": "pass"}),
}


@router.post("/simulate")
def simulate_webhook(db: Db, user: Reviewer, scenario: Annotated[str, Form()] = "clean"):
    """Dev helper: build a vendor payload, sign it and run it through the same intake as the real webhook."""
    decision, checks = SCENARIOS.get(scenario, SCENARIOS["clean"])
    n = secrets.token_hex(2)
    device = {"fingerprint": f"fp_{secrets.token_hex(6)}", "ip": f"203.0.113.{secrets.randbelow(250) + 1}"}
    address = f"{secrets.randbelow(900) + 100} market st, san francisco, ca"
    if scenario == "ring":
        ring = db.scalar(select(KycCase).where(KycCase.status == "rejected", KycCase.device_fingerprint != "")
                         .order_by(KycCase.id))
        if ring:
            device = {"fingerprint": ring.device_fingerprint, "ip": ring.ip_address}
            address = ring.address
    payload = {"id": f"sim_{n}", "vendor": "alloy", "decision": decision, "checks": checks,
               "applicant": {"name": f"Sim Applicant {n}", "email": f"sim.{n}@example.com", "address": address},
               "device": device}
    body = json.dumps(payload).encode()
    assert service.verify_signature(body, service.sign(body))  # same path as the real webhook
    case, _ = service.intake(db, payload)
    db.commit()
    events.dispatch_pending(db)
    return _redirect(f"/kyc/{case.id}", msg=f"Simulated {scenario} webhook: case #{case.id} is {case.status}.")
