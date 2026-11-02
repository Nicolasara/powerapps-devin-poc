"""Refund business rules. Routes and seed both go through here so the rules live in one place."""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.platform import approvals, events
from app.platform.audit import audit
from app.platform.models import Approval, User, utcnow
from app.platform.notify import get_notifier

from .models import OPEN_STATUSES, REASON_CODES, KycSignal, RefundRequest, Transaction
from .processor import get_processor

KIND = "refund.execute"
APPROVE_THRESHOLD_CENTS = 100_00  # >= $100 needs a checker
LARGE_THRESHOLD_CENTS = 1_000_00  # >= $1,000 needs a senior checker


class RefundError(Exception):
    pass


def fmt_money(cents: int | None, currency: str = "USD") -> str:
    cents = cents or 0
    sign = "-" if cents < 0 else ""
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{sign}{symbol}{abs(cents) / 100:,.2f}"


def parse_amount(raw: str) -> int:
    """'12.34' / '$1,200' -> cents. Rejects more than 2 decimal places."""
    cleaned = (raw or "").strip().replace("$", "").replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise RefundError("Enter the amount as a number, e.g. 25.00") from None
    if value.as_tuple().exponent < -2:
        raise RefundError("Amount can have at most 2 decimal places")
    cents = int(value * 100)
    if cents <= 0:
        raise RefundError("Amount must be greater than zero")
    return cents


def required_permission_for(amount_cents: int) -> str | None:
    if amount_cents >= LARGE_THRESHOLD_CENTS:
        return "refunds.approve_large"
    if amount_cents >= APPROVE_THRESHOLD_CENTS:
        return "refunds.approve"
    return None


def reserved_cents(db: Session, tx: Transaction) -> int:
    """Amount tied up in refunds that are still awaiting a decision."""
    return db.scalar(
        select(func.coalesce(func.sum(RefundRequest.amount_cents), 0)).where(
            RefundRequest.transaction_id == tx.id, RefundRequest.status.in_(OPEN_STATUSES)
        )
    ) or 0


def available_cents(db: Session, tx: Transaction) -> int:
    return tx.amount_cents - tx.refunded_cents - reserved_cents(db, tx)


def find_by_key(db: Session, idempotency_key: str) -> RefundRequest | None:
    return db.scalar(select(RefundRequest).where(RefundRequest.idempotency_key == idempotency_key))


def _event_payload(rr: RefundRequest) -> dict:
    tx = rr.transaction
    return {
        "refund_request_id": rr.id,
        "transaction_id": tx.id,
        "amount_cents": rr.amount_cents,
        "currency": tx.currency,
        "reason_code": rr.reason_code,
        "status": rr.status,
        "customer_email": tx.customer_email,
    }


def create_request(db: Session, actor: User, tx: Transaction, amount_cents: int, reason_code: str,
                   note: str | None, idempotency_key: str) -> RefundRequest:
    """Create a refund request and route it through maker/checker. Caller commits + dispatches."""
    if not idempotency_key:
        raise RefundError("Missing idempotency key; reload the page and try again")
    if reason_code not in REASON_CODES:
        raise RefundError("Choose a reason code")
    available = available_cents(db, tx)
    if amount_cents > available:
        raise RefundError(f"Amount exceeds the remaining refundable amount ({fmt_money(available, tx.currency)})")

    perm = required_permission_for(amount_cents)
    rr = RefundRequest(
        transaction_id=tx.id, transaction=tx, amount_cents=amount_cents, reason_code=reason_code,
        note=(note or "").strip() or None, status="requested", idempotency_key=idempotency_key,
        requested_by_email=actor.email, required_permission=perm,
    )
    db.add(rr)
    db.flush()
    audit(db, actor, "refund.requested", "refund_request", rr.id,
          after={"transaction_id": tx.id, "amount_cents": amount_cents, "reason_code": reason_code,
                 "required_permission": perm})
    events.publish(db, "refund.requested.v1", _event_payload(rr))

    summary = f"Refund {fmt_money(amount_cents, tx.currency)} on tx #{tx.id} ({tx.merchant}) - {reason_code}"
    if perm is not None:
        rr.status = "pending_approval"
    approval = approvals.request(db, actor, KIND, "refund_request", rr.id, {"refund_request_id": rr.id},
                                 summary, perm)
    if approval is not None:
        rr.approval_id = approval.id
    return rr


def apply_refund(db: Session, approval: Approval, actor: User) -> None:
    """Approval applier: call the processor and record the outcome in the same transaction."""
    rr = db.get(RefundRequest, approval.payload["refund_request_id"])
    if rr is None or rr.status in ("executed", "rejected", "failed"):
        return
    tx = rr.transaction
    before = {"status": rr.status, "refunded_cents": tx.refunded_cents}
    error: str | None = None
    if rr.amount_cents > tx.amount_cents - tx.refunded_cents:
        error = "amount exceeds remaining refundable amount at execution time"
    else:
        try:
            refund_id = get_processor().refund(tx.processor_charge_id, rr.amount_cents, rr.idempotency_key)
        except Exception as exc:  # noqa: BLE001 - any adapter error must mark the refund failed
            error = str(exc) or exc.__class__.__name__

    if error is not None:
        rr.status = "failed"
        rr.failure_reason = error
        audit(db, actor, "refund.failed", "refund_request", rr.id, before=before,
              after={"status": "failed"}, reason=error)
        events.publish(db, "refund.failed.v1", {**_event_payload(rr), "error": error})
        get_notifier(db).send("#refunds", f"Refund #{rr.id} FAILED ({fmt_money(rr.amount_cents, tx.currency)} "
                                          f"on tx #{tx.id}): {error}")
        return

    rr.status = "executed"
    rr.processor_refund_id = refund_id
    rr.executed_at = utcnow()
    tx.refunded_cents += rr.amount_cents
    audit(db, actor, "refund.executed", "refund_request", rr.id, before=before,
          after={"status": "executed", "refunded_cents": tx.refunded_cents, "processor_refund_id": refund_id})
    events.publish(db, "refund.executed.v1", {**_event_payload(rr), "processor_refund_id": refund_id})
    approved = f"approved by {actor.email}" if approval.required_permission else "auto-approved under threshold"
    get_notifier(db).send("#refunds", f"Refund #{rr.id} executed: {fmt_money(rr.amount_cents, tx.currency)} "
                                      f"on tx #{tx.id} ({tx.merchant}), requested by {rr.requested_by_email}, "
                                      f"{approved}")


def sync_rejections(db: Session) -> int:
    """Mirror checker rejections onto refund requests (approvals only calls appliers on approve)."""
    rows = db.execute(
        select(RefundRequest, Approval).join(Approval, Approval.id == RefundRequest.approval_id).where(
            RefundRequest.status == "pending_approval", Approval.status == "rejected"
        )
    ).all()
    for rr, appr in rows:
        rr.status = "rejected"
        decider = db.get(User, appr.decided_by) if appr.decided_by else None
        audit(db, decider, "refund.rejected", "refund_request", rr.id, before={"status": "pending_approval"},
              after={"status": "rejected"}, reason=appr.decision_reason)
    if rows:
        db.commit()
    return len(rows)


def _parse_at(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))  # py3.10 fromisoformat rejects 'Z'
        except ValueError:
            pass
    return utcnow()


def on_kyc_decision(db: Session, payload: dict) -> None:
    db.add(KycSignal(
        customer_email=str(payload.get("customer_email", "")).lower(),
        risk_band=str(payload.get("risk_band", "unknown")),
        outcome=str(payload.get("outcome", "unknown")),
        reviewed_manually=bool(payload.get("reviewed_manually", False)),
        at=_parse_at(payload.get("at")),
    ))


def kyc_hint(db: Session, customer_email: str) -> str | None:
    signal = db.scalars(
        select(KycSignal).where(
            KycSignal.customer_email == customer_email.lower(),
            (KycSignal.reviewed_manually.is_(True)) | (KycSignal.risk_band == "high"),
        ).order_by(KycSignal.at.desc())
    ).first()
    if signal is None:
        return None
    day = signal.at.strftime("%Y-%m-%d")
    if signal.reviewed_manually and signal.risk_band == "high" and signal.outcome == "approved":
        return f"KYC: manually approved high-risk on {day}"
    manual = "manually reviewed" if signal.reviewed_manually else "automated"
    return f"KYC: {signal.risk_band}-risk, {signal.outcome} ({manual}) on {day}"
