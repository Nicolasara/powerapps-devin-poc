"""KYC review: signed vendor webhook intake, cross-applicant risk rules, analyst decisions, kyc.decision.v1 events."""
import hashlib
import hmac
import os
from collections import Counter

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.platform import events
from app.platform.audit import audit
from app.platform.models import User, utcnow
from app.platform.notify import get_notifier

from .models import KycCase

DECISION_EVENT = "kyc.decision.v1"
SIGNATURE_HEADER = "X-Vendor-Signature"
DISPOSABLE_DOMAINS = {"mailinator.com", "guerrillamail.com", "tempmail.dev", "10minutemail.com"}


class KycError(ValueError):
    pass


def webhook_secret() -> str | None:
    """Shared HMAC secret with the vendor. The dev default only applies when APP_ENV is unset or 'dev'."""
    secret = os.environ.get("KYC_WEBHOOK_SECRET")
    if secret:
        return secret
    return "dev-kyc-webhook-secret" if os.environ.get("APP_ENV", "dev") == "dev" else None


def sign(body: bytes) -> str:
    secret = webhook_secret()
    if not secret:
        raise KycError("KYC_WEBHOOK_SECRET is not configured")
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, header: str | None) -> bool:
    if not header or not webhook_secret():
        return False
    return hmac.compare_digest(sign(body), header)


# ---------------------------------------------------------------- risk rules (deterministic, explainable)

def _linked(db: Session, case: KycCase) -> list[KycCase]:
    """Other cases sharing a device, address or IP with this one."""
    clauses = []
    if case.device_fingerprint:
        clauses.append(KycCase.device_fingerprint == case.device_fingerprint)
    if case.address:
        clauses.append(KycCase.address == case.address)
    if case.ip_address:
        clauses.append(KycCase.ip_address == case.ip_address)
    if not clauses:
        return []
    q = select(KycCase).where(KycCase.id != case.id)
    cond = clauses[0]
    for c in clauses[1:]:
        cond = cond | c
    return db.scalars(q.where(cond).order_by(KycCase.created_at.desc())).all()


def linked_cases(db: Session, case: KycCase) -> list[tuple[KycCase, list[str]]]:
    out = []
    for other in _linked(db, case):
        shared = []
        if case.device_fingerprint and other.device_fingerprint == case.device_fingerprint:
            shared.append("device")
        if case.address and other.address == case.address:
            shared.append("address")
        if case.ip_address and other.ip_address == case.ip_address:
            shared.append("ip")
        out.append((other, shared))
    return out


def score(db: Session, case: KycCase) -> tuple[int, str, list[str]]:
    points, reasons = 0, []
    links = linked_cases(db, case)
    rejected = [(o, s) for o, s in links if o.status == "rejected"]
    if rejected:
        kinds = Counter(k for _, s in rejected for k in s)
        what = ", ".join(sorted(kinds))
        points += 60
        reasons.append(f"Shares {what} with {len(rejected)} rejected applicant{'s' if len(rejected) > 1 else ''}")
    device_peers = [o for o, s in links if "device" in s and o.status != "rejected"]
    if len(device_peers) >= 2:
        points += 25
        reasons.append(f"Device fingerprint seen on {len(device_peers) + 1} applications")
    elif device_peers:
        points += 10
        reasons.append("Device fingerprint shared with another applicant")
    failed_checks = [k for k, v in (case.vendor_checks or {}).items() if v == "fail"]
    if case.vendor_decision == "pass" and failed_checks:
        points += 30
        reasons.append("Vendor passed despite failed checks: " + ", ".join(failed_checks))
    if case.vendor_decision == "fail":
        points += 50
        failed = [k for k, v in (case.vendor_checks or {}).items() if v == "fail"]
        reasons.append("Vendor failed: " + ", ".join(failed) if failed else "Vendor decision: fail")
    elif case.vendor_decision == "review":
        points += 25
        flagged = [k for k, v in (case.vendor_checks or {}).items() if v != "pass"]
        reasons.append("Vendor asked for review: " + ", ".join(flagged) if flagged else "Vendor decision: review")
    domain = case.applicant_email.rsplit("@", 1)[-1].lower()
    if domain in DISPOSABLE_DOMAINS:
        points += 15
        reasons.append("Disposable email domain")
    band = "high" if points >= 50 else ("medium" if points >= 20 else "low")
    return points, band, reasons


def rescore(db: Session, case: KycCase) -> None:
    case.risk_score, case.risk_band, case.risk_reasons = score(db, case)


# ---------------------------------------------------------------- intake and decisions

def _insert_ignore_conflict(db: Session):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(KycCase).on_conflict_do_nothing(index_elements=["vendor_ref"])


def intake(db: Session, payload: dict) -> tuple[KycCase, bool]:
    """Create a case from a vendor webhook. Returns (case, created). Retries with the same vendor_ref are no-ops."""
    try:
        ref = str(payload["id"])
        applicant = payload["applicant"]
        decision = str(payload["decision"])
    except (KeyError, TypeError):
        raise KycError("payload must include id, applicant and decision") from None
    existing = db.scalar(select(KycCase).where(KycCase.vendor_ref == ref))
    if existing:
        return existing, False
    if decision not in ("pass", "review", "fail"):
        raise KycError("decision must be pass, review or fail")
    device = payload.get("device") or {}
    values = {
        "vendor": str(payload.get("vendor", "alloy")), "vendor_ref": ref,
        "applicant_name": str(applicant.get("name", "")).strip(), "applicant_email": str(applicant.get("email", "")).lower(),
        "address": " ".join(str(applicant.get("address", "")).split()).lower(),
        "device_fingerprint": str(device.get("fingerprint", "")), "ip_address": str(device.get("ip", "")),
        "vendor_decision": decision, "vendor_checks": dict(payload.get("checks") or {}),
    }
    # One idempotent INSERT in the outer transaction (no savepoint: pysqlite would commit it on its own), so a
    # concurrent retry sees "already there" and a failure later in this unit of work rolls the row back too.
    inserted = db.execute(_insert_ignore_conflict(db).values(**values)).rowcount == 1
    case = db.scalar(select(KycCase).where(KycCase.vendor_ref == ref))
    if not inserted:
        return case, False
    rescore(db, case)
    audit(db, None, "kyc.case_created", "kyc_case", case.id,
          after={**case.summary(), "vendor_decision": decision, "vendor_ref": ref}, reason=f"webhook from {case.vendor}")
    for other, _ in linked_cases(db, case):
        if other.status == "pending":
            before = other.summary()
            rescore(db, other)
            if other.summary() != before:
                audit(db, None, "kyc.rescored", "kyc_case", other.id, before=before, after=other.summary(),
                      reason=f"linked case #{case.id} received")
    all_checks_pass = all(v == "pass" for v in (case.vendor_checks or {}).values())
    if decision == "pass" and all_checks_pass and case.risk_band == "low":
        _decide(db, case, None, approve=True, reason="auto: vendor pass, all checks pass, low risk")
    else:
        get_notifier(db).send("#kyc-review", f"New KYC case #{case.id} ({case.risk_band} risk): {case.applicant_name} "
                                             f"- {'; '.join(case.risk_reasons) or 'vendor ' + decision}")
    return case, True


def _decide(db: Session, case: KycCase, actor: User | None, approve: bool, reason: str | None) -> None:
    before = case.summary()
    case.status = "approved" if approve else "rejected"
    case.auto_decided = actor is None
    case.decided_by_email = actor.email if actor else "system"
    case.decided_at = utcnow()
    case.decision_reason = reason
    audit(db, actor, f"kyc.{case.status}", "kyc_case", case.id, before=before, after=case.summary(), reason=reason)
    events.publish(db, DECISION_EVENT, {
        "customer_email": case.applicant_email, "risk_band": case.risk_band, "outcome": case.status,
        "reviewed_manually": actor is not None, "at": case.decided_at.isoformat(), "case_id": case.id,
    })
    if actor is not None:
        get_notifier(db).send("#kyc-review", f"{actor.email} {case.status} KYC case #{case.id} "
                                             f"({case.applicant_name}, {case.risk_band} risk): {reason or 'no reason'}")


def decide(db: Session, actor: User, case: KycCase, approve: bool, reason: str | None) -> KycCase:
    if case.status != "pending":
        raise KycError(f"case is already {case.status}")
    if not approve and not (reason or "").strip():
        raise KycError("a reason is required to reject")
    # Conditional update: exactly one reviewer can move pending -> decided, even under concurrency.
    claimed = db.execute(update(KycCase).where(KycCase.id == case.id, KycCase.status == "pending")
                         .values(status="deciding")).rowcount
    if claimed != 1:
        db.refresh(case)
        raise KycError(f"case is already {case.status}")
    db.refresh(case)
    case.status = "pending"  # restore for before/after bookkeeping inside _decide
    _decide(db, case, actor, approve, (reason or "").strip() or None)
    if not approve:
        # A rejection changes the picture for everyone still pending who shares a device/address/IP.
        for other, _ in linked_cases(db, case):
            if other.status == "pending":
                before = other.summary()
                rescore(db, other)
                if other.summary() != before:
                    audit(db, actor, "kyc.rescored", "kyc_case", other.id, before=before, after=other.summary(),
                          reason=f"linked case #{case.id} rejected")
    return case


def stats(db: Session) -> dict:
    def count(*where) -> int:
        return db.scalar(select(func.count()).select_from(KycCase).where(*where)) or 0

    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "pending": count(KycCase.status == "pending"),
        "high_pending": count(KycCase.status == "pending", KycCase.risk_band == "high"),
        "approved_today": count(KycCase.status == "approved", KycCase.decided_at >= today),
        "rejected": count(KycCase.status == "rejected"),
    }


# ---------------------------------------------------------------- documents (vendor vault, short-lived signed links)

DOC_KINDS = ("document", "selfie")
DOC_TTL_SECONDS = 300


def document_url(case: KycCase, kind: str, now: int | None = None) -> str:
    """Short-lived signed link, the same idea as an S3 presigned URL: nobody can bookmark or share a PII image."""
    import time
    exp = (now or int(time.time())) + DOC_TTL_SECONDS
    msg = f"{case.id}:{kind}:{exp}".encode()
    sig = hmac.new((webhook_secret() or "no-secret").encode(), msg, hashlib.sha256).hexdigest()[:32]
    return f"/kyc/{case.id}/documents/{kind}?exp={exp}&sig={sig}"


def document_link_valid(case: KycCase, kind: str, exp: int, sig: str, now: int | None = None) -> bool:
    import time
    if kind not in DOC_KINDS or exp < (now or int(time.time())):
        return False
    msg = f"{case.id}:{kind}:{exp}".encode()
    good = hmac.new((webhook_secret() or "no-secret").encode(), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(good, sig)
