"""KYC review queue: vendor webhook intake, cross-applicant risk rules, analyst decisions, kyc.decision.v1 events."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.platform import events
from app.platform.models import User, utcnow

from . import service
from .models import KycCase

_RING_FP = "fp_7f3a9c2e1b4d"
_CLEAN = {"document": "pass", "selfie": "pass", "sanctions": "pass", "address": "pass"}

# vendor_ref, name, email, address, fingerprint, ip, decision, checks, days_ago
_SEED = [
    ("alloy_1001", "Maya Chen", "maya.chen@example.com", "12 pine st, oakland, ca", "fp_a1", "198.51.100.10",
     "pass", _CLEAN, 9),
    ("alloy_1002", "Liam Okafor", "liam.okafor@example.com", "88 lake ave, chicago, il", "fp_b2", "198.51.100.11",
     "pass", _CLEAN, 8),
    ("alloy_1003", "Jordan Blake", "j.blake@mailinator.com", "400 elm st apt 2, austin, tx", _RING_FP, "203.0.113.7",
     "fail", {"document": "fail", "selfie": "pass", "sanctions": "pass", "address": "review"}, 6),
    ("alloy_1004", "Sofia Marin", "sofia.marin@example.com", "7 harbor rd, boston, ma", "fp_c3", "198.51.100.12",
     "review", {"document": "pass", "selfie": "review", "sanctions": "pass", "address": "pass"}, 3),
    ("alloy_1005", "Taylor Reed", "taylor.reed@example.com", "400 elm st apt 2, austin, tx", _RING_FP, "203.0.113.7",
     "pass", _CLEAN, 1),
    ("alloy_1006", "Casey Nguyen", "casey.nguyen@example.com", "19 oak ct, austin, tx", _RING_FP, "203.0.113.9",
     "pass", _CLEAN, 0),
    ("alloy_1007", "Noah Patel", "noah.patel@example.com", "5 river ln, denver, co", "fp_d4", "198.51.100.13",
     "pass", _CLEAN, 0),
]


def register() -> None:
    """KYC emits events; it subscribes to nothing."""


def seed(db: Session) -> None:
    if db.scalar(select(func.count(KycCase.id))):
        return
    april = db.scalar(select(User).where(User.email == "april@corp.test"))
    now = utcnow()
    for ref, name, email, address, fp, ip, decision, checks, days in _SEED:
        case, _ = service.intake(db, {
            "id": ref, "vendor": "alloy", "decision": decision, "checks": checks,
            "applicant": {"name": name, "email": email, "address": address},
            "device": {"fingerprint": fp, "ip": ip},
        })
        case.created_at = now - timedelta(days=days, hours=3)
        if case.status != "pending":
            case.decided_at = case.created_at
        if ref == "alloy_1003" and april:
            # Rejected before the ring applied; the later applicants inherit the risk.
            service.decide(db, april, case, approve=False, reason="Document failed vendor check; address mismatch")
            case.decided_at = case.created_at + timedelta(hours=2)
        db.flush()
    for case in db.scalars(select(KycCase).where(KycCase.status == "pending")):
        service.rescore(db, case)
    db.commit()
    events.dispatch_pending(db)
