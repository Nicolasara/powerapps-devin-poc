import json

from sqlalchemy import select

from app.apps.kyc import service
from app.apps.kyc.models import KycCase
from app.apps.refunds.models import KycSignal
from app.platform.models import OutboxEvent


def _payload(ref="v1", email="a@example.com", fp="fp_x", decision="pass", address="1 main st", **extra):
    checks = extra.pop("checks", {"document": "pass", "selfie": "pass", "sanctions": "pass", "address": "pass"})
    return {"id": ref, "vendor": "alloy", "decision": decision, "checks": checks,
            "applicant": {"name": "Test Person", "email": email, "address": address},
            "device": {"fingerprint": fp, "ip": "203.0.113.1"}, **extra}


def _post(client, payload, sign=True):
    body = json.dumps(payload).encode()
    headers = {service.SIGNATURE_HEADER: service.sign(body)} if sign else {}
    return client.post("/kyc/webhook/vendor", content=body, headers={"content-type": "application/json", **headers})


def test_webhook_rejects_bad_signature(anon, db):
    body = json.dumps(_payload()).encode()
    assert anon.post("/kyc/webhook/vendor", content=body).status_code == 401
    assert anon.post("/kyc/webhook/vendor", content=body, headers={service.SIGNATURE_HEADER: "sha256=nope"}).status_code == 401
    assert db.scalar(select(KycCase)) is None


def test_clean_pass_auto_approves_and_notifies_refunds(anon, db):
    resp = _post(anon, _payload(email="clean@example.com"))
    assert resp.status_code == 201, resp.text
    case = db.scalar(select(KycCase))
    assert case.status == "approved" and case.auto_decided and case.risk_band == "low"
    ev = db.scalar(select(OutboxEvent).where(OutboxEvent.type == "kyc.decision.v1"))
    assert ev.payload["customer_email"] == "clean@example.com" and ev.payload["reviewed_manually"] is False
    assert db.scalar(select(KycSignal).where(KycSignal.customer_email == "clean@example.com")) is not None


def test_webhook_retry_is_idempotent(anon, db):
    assert _post(anon, _payload(ref="dup")).status_code == 201
    assert _post(anon, _payload(ref="dup")).status_code == 200
    assert len(db.scalars(select(KycCase)).all()) == 1


def test_shared_device_with_rejected_applicant_is_high_risk(anon, login, db):
    _post(anon, _payload(ref="bad", email="bad@example.com", fp="fp_ring", decision="fail",
                         checks={"document": "fail", "selfie": "pass", "sanctions": "pass", "address": "pass"}))
    bad = db.scalar(select(KycCase).where(KycCase.vendor_ref == "bad"))
    assert bad.status == "pending" and bad.risk_band == "high"
    login("april").post(f"/kyc/{bad.id}/decide", data={"decision": "reject", "reason": "forged document"},
                        follow_redirects=False)

    resp = _post(anon, _payload(ref="ring2", email="ring2@example.com", fp="fp_ring"))
    assert resp.json()["status"] == "pending" and resp.json()["risk_band"] == "high"
    ring2 = db.scalar(select(KycCase).where(KycCase.vendor_ref == "ring2"))
    assert any("rejected applicant" in r for r in ring2.risk_reasons)
    db.expire_all()
    page = login("april").get(f"/kyc/{ring2.id}")
    assert "bad@example.com" in page.text and "device" in page.text  # linked application shown


def test_rejecting_rescoring_pending_peers(anon, login, db):
    _post(anon, _payload(ref="p1", email="p1@example.com", fp="fp_same", decision="review",
                         checks={"document": "pass", "selfie": "review", "sanctions": "pass", "address": "pass"}))
    _post(anon, _payload(ref="p2", email="p2@example.com", fp="fp_same", decision="review",
                         checks={"document": "pass", "selfie": "review", "sanctions": "pass", "address": "pass"}))
    p1, p2 = (db.scalar(select(KycCase).where(KycCase.vendor_ref == r)) for r in ("p1", "p2"))
    assert p2.risk_band == "medium"
    login("adam").post(f"/kyc/{p1.id}/decide", data={"decision": "reject", "reason": "synthetic identity"},
                       follow_redirects=False)
    db.expire_all()
    assert db.get(KycCase, p2.id).risk_band == "high"


def test_manual_approval_of_high_risk_shows_up_as_refund_hint(anon, login, db):
    _post(anon, _payload(ref="h1", email="h1@example.com", decision="fail",
                         checks={"document": "fail", "selfie": "pass", "sanctions": "pass", "address": "pass"}))
    case = db.scalar(select(KycCase).where(KycCase.vendor_ref == "h1"))
    resp = login("april").post(f"/kyc/{case.id}/decide", data={"decision": "approve", "reason": "verified by phone"},
                               follow_redirects=False)
    assert resp.status_code == 303
    signal = db.scalar(select(KycSignal).where(KycSignal.customer_email == "h1@example.com"))
    assert signal.reviewed_manually and signal.risk_band == "high" and signal.outcome == "approved"


def test_reject_requires_reason_and_reviewer_permission(anon, login, db):
    _post(anon, _payload(ref="r1", email="r1@example.com", decision="review",
                         checks={"document": "review", "selfie": "pass", "sanctions": "pass", "address": "pass"}))
    case = db.scalar(select(KycCase).where(KycCase.vendor_ref == "r1"))
    assert login("oscar").post(f"/kyc/{case.id}/decide", data={"decision": "reject", "reason": "x"},
                               follow_redirects=False).status_code == 403
    resp = login("april").post(f"/kyc/{case.id}/decide", data={"decision": "reject", "reason": ""},
                               follow_redirects=False)
    assert "error=" in resp.headers["location"]
    db.expire_all()
    assert db.get(KycCase, case.id).status == "pending"


def test_simulate_ring_reuses_rejected_device(anon, login, db):
    _post(anon, _payload(ref="bad", email="bad@example.com", fp="fp_ring", decision="fail",
                         checks={"document": "fail", "selfie": "pass", "sanctions": "pass", "address": "pass"}))
    bad = db.scalar(select(KycCase).where(KycCase.vendor_ref == "bad"))
    april = login("april")
    april.post(f"/kyc/{bad.id}/decide", data={"decision": "reject", "reason": "forged"}, follow_redirects=False)
    resp = april.post("/kyc/simulate", data={"scenario": "ring"}, follow_redirects=False)
    assert resp.status_code == 303
    sim = db.scalar(select(KycCase).where(KycCase.vendor_ref.like("sim_%")))
    assert sim.device_fingerprint == "fp_ring" and sim.risk_band == "high" and sim.status == "pending"
