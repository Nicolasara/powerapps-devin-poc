import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select

from app.apps.refunds.models import KycSignal, RefundRequest, Transaction
from app.platform import events
from app.platform.models import Approval, AuditEvent, OutboxEvent


@pytest.fixture
def tx_id(db) -> int:
    tx = Transaction(customer_name="Maya Chen", customer_email="maya.chen@example.com", amount_cents=250_000,
                     currency="USD", merchant="Contoso Travel", processor_charge_id=f"ch_{uuid.uuid4().hex[:20]}")
    db.add(tx)
    db.commit()
    return tx.id


def _submit(client, tx_id: int, amount: str, key: str | None = None, reason: str = "duplicate"):
    return client.post("/refunds/requests", data={
        "transaction_id": str(tx_id), "amount": amount, "reason_code": reason, "note": "test",
        "idempotency_key": key or str(uuid.uuid4()),
    }, follow_redirects=False)


def _request_id(resp) -> int:
    assert resp.status_code == 303, resp.text
    return int(urlparse(resp.headers["location"]).path.rsplit("/", 1)[-1])


def _refund(db, rr_id: int) -> RefundRequest:
    db.expire_all()
    return db.get(RefundRequest, rr_id)


def _tx(db, tx_id: int) -> Transaction:
    db.expire_all()
    return db.get(Transaction, tx_id)


def _decide(client, approval_id: int, decision: str = "approve") -> dict[str, list[str]]:
    resp = client.post(f"/approvals/{approval_id}/decide", data={"decision": decision}, follow_redirects=False)
    assert resp.status_code == 303
    return parse_qs(urlparse(resp.headers["location"]).query)


def test_small_refund_executes_immediately(login, db, tx_id):
    rr_id = _request_id(_submit(login("oscar"), tx_id, "50.00"))
    rr = _refund(db, rr_id)
    assert rr.status == "executed"
    assert rr.processor_refund_id.startswith("re_")
    assert rr.required_permission is None
    assert _tx(db, tx_id).refunded_cents == 5_000
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "refund.executed",
                                              AuditEvent.entity_id == str(rr_id))) is not None
    assert db.scalar(select(func.count()).select_from(OutboxEvent)
                     .where(OutboxEvent.type == "refund.executed.v1")) == 1


def test_viewer_cannot_request_refund(login, db, tx_id):
    resp = _submit(login("alice"), tx_id, "10.00")
    assert resp.status_code == 403
    assert db.scalar(select(func.count()).select_from(RefundRequest)) == 0


def test_mid_size_refund_needs_an_approver(login, db, tx_id):
    oscar, april = login("oscar"), login("april")
    rr_id = _request_id(_submit(oscar, tx_id, "500.00"))
    rr = _refund(db, rr_id)
    assert rr.status == "pending_approval"
    assert rr.required_permission == "refunds.approve"
    assert _tx(db, tx_id).refunded_cents == 0

    assert "maker cannot be checker" in _decide(oscar, rr.approval_id)["error"][0]
    assert _refund(db, rr_id).status == "pending_approval"

    assert "error" not in _decide(april, rr.approval_id)
    rr = _refund(db, rr_id)
    assert rr.status == "executed"
    assert rr.processor_refund_id
    assert _tx(db, tx_id).refunded_cents == 50_000


def test_large_refund_needs_admin_not_approver(login, db, tx_id):
    rr_id = _request_id(_submit(login("oscar"), tx_id, "1000.00"))
    rr = _refund(db, rr_id)
    assert rr.status == "pending_approval"
    assert rr.required_permission == "refunds.approve_large"

    assert "requires refunds.approve_large" in _decide(login("april"), rr.approval_id)["error"][0]
    assert _refund(db, rr_id).status == "pending_approval"
    assert db.get(Approval, rr.approval_id).status == "pending"

    assert "error" not in _decide(login("adam"), rr.approval_id)
    assert _refund(db, rr_id).status == "executed"
    assert _tx(db, tx_id).refunded_cents == 100_000


def test_rejected_refund_is_not_executed(login, db, tx_id):
    rr_id = _request_id(_submit(login("oscar"), tx_id, "250.00"))
    approval_id = _refund(db, rr_id).approval_id
    assert "error" not in _decide(login("april"), approval_id, "reject")
    assert login("oscar").get(f"/refunds/requests/{rr_id}").status_code == 200
    assert _refund(db, rr_id).status == "rejected"
    assert _tx(db, tx_id).refunded_cents == 0


def test_repeated_idempotency_key_does_not_double_refund(login, db, tx_id):
    oscar = login("oscar")
    key = str(uuid.uuid4())
    first = _request_id(_submit(oscar, tx_id, "40.00", key=key))
    second = _submit(oscar, tx_id, "40.00", key=key)
    assert _request_id(second) == first
    assert "Duplicate submission" in parse_qs(urlparse(second.headers["location"]).query)["msg"][0]

    assert db.scalar(select(func.count()).select_from(RefundRequest)) == 1
    assert _tx(db, tx_id).refunded_cents == 4_000
    assert db.scalar(select(func.count()).select_from(OutboxEvent)
                     .where(OutboxEvent.type == "refund.executed.v1")) == 1


def test_amount_above_remaining_is_rejected(login, db, tx_id):
    oscar = login("oscar")
    _request_id(_submit(oscar, tx_id, "99.00"))  # executes, leaves $2,401.00
    resp = _submit(oscar, tx_id, "2401.01")
    assert resp.status_code == 400
    assert "remaining refundable" in resp.text
    assert db.scalar(select(func.count()).select_from(RefundRequest)) == 1
    assert _tx(db, tx_id).refunded_cents == 9_900


def test_pending_refunds_reserve_the_remaining_amount(login, db, tx_id):
    oscar = login("oscar")
    _request_id(_submit(oscar, tx_id, "2000.00"))  # pending large approval
    resp = _submit(oscar, tx_id, "600.00")
    assert resp.status_code == 400
    assert db.scalar(select(func.count()).select_from(RefundRequest)) == 1


def test_kyc_decision_event_creates_signal_and_tx_page_shows_hint(login, db, tx_id):
    events.publish(db, "kyc.decision.v1", {
        "customer_email": "Maya.Chen@example.com", "risk_band": "high", "outcome": "approved",
        "reviewed_manually": True, "at": datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc).isoformat(),
    })
    db.commit()
    assert events.dispatch_pending(db) == 1

    signal = db.scalar(select(KycSignal))
    assert signal is not None
    assert signal.customer_email == "maya.chen@example.com"
    assert signal.risk_band == "high"
    assert signal.reviewed_manually is True

    page = login("alice").get(f"/refunds/tx/{tx_id}")
    assert page.status_code == 200
    assert "KYC: manually approved high-risk on 2024-05-01" in page.text


def test_tx_page_has_no_kyc_hint_without_signal(login, tx_id):
    page = login("oscar").get(f"/refunds/tx/{tx_id}")
    assert page.status_code == 200
    assert "KYC:" not in page.text
