import json

from sqlalchemy import select

from app.apps.kyc import service
from app.apps.kyc.models import KycCase
from app.apps.refunds.models import KycSignal
from app.platform.models import AuditEvent
from tests.test_kyc import _payload, _post


def test_document_links_are_signed_short_lived_and_audited(anon, login, db):
    _post(anon, _payload(ref="d1", email="d1@example.com", decision="review",
                         checks={"document": "review", "selfie": "pass", "sanctions": "pass", "address": "pass"}))
    case = db.scalar(select(KycCase).where(KycCase.vendor_ref == "d1"))
    april = login("april")
    page = april.get(f"/kyc/{case.id}").text
    url = service.document_url(case, "document")
    assert f"/kyc/{case.id}/documents/document?exp=" in page
    assert april.get(url).status_code == 200
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "kyc.document_viewed")) is not None
    assert april.get(url.replace("sig=", "sig=0")).status_code == 403
    expired = service.document_url(case, "document", now=1)
    assert april.get(expired).status_code == 403
    assert login("oscar").get(url).status_code == 403  # not a reviewer


def test_refund_banner_links_to_kyc_case(anon, login, db):
    body = json.dumps(_payload(ref="h9", email="noah.garcia@example.com", decision="fail",
                               checks={"document": "fail", "selfie": "pass", "sanctions": "pass", "address": "pass"})).encode()
    _post(anon, json.loads(body))
    case = db.scalar(select(KycCase).where(KycCase.vendor_ref == "h9"))
    login("april").post(f"/kyc/{case.id}/decide", data={"decision": "approve", "reason": "verified"}, follow_redirects=False)
    assert db.scalar(select(KycSignal).where(KycSignal.customer_email == "noah.garcia@example.com")).case_id == case.id
