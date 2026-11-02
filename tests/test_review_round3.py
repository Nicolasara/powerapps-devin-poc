import json

from sqlalchemy import select, text

from app.apps.flags.models import Flag
from app.platform.models import Approval
from tests.test_kyc import _payload, _post


def test_duplicate_prod_promotion_is_rejected(login, db):
    oscar = login("oscar")
    oscar.post("/flags", data={"key": "dup", "enabled": "true", "rollout_percent": "100"}, follow_redirects=False)
    oscar.post("/flags/promote", data={"key": "dup", "to_env": "staging"}, follow_redirects=False)
    oscar.post("/flags/promote", data={"key": "dup", "to_env": "prod"}, follow_redirects=False)
    resp = oscar.post("/flags/promote", data={"key": "dup", "to_env": "prod"}, follow_redirects=False)
    assert "already awaiting approval" in resp.headers["location"].replace("+", " ")
    assert db.scalar(select(Flag).where(Flag.key == "dup", Flag.environment == "prod")) is None
    assert len(db.scalars(select(Approval).where(Approval.entity_id == "new:dup:prod")).all()) == 1


def test_webhook_rejects_malformed_content_length(anon):
    body = json.dumps(_payload(ref="cl1", email="cl1@example.com")).encode()
    resp = anon.post("/kyc/webhook/vendor", content=body, headers={"content-length": "abc"})
    assert resp.status_code == 400


def test_old_schema_gets_missing_nullable_columns(tmp_path):
    from sqlalchemy import create_engine

    from app.main import _add_missing_columns
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as conn:  # table as it existed before case_id was added
        conn.execute(text("CREATE TABLE refunds_kyc_signals (id INTEGER PRIMARY KEY, customer_email VARCHAR(200), "
                          "risk_band VARCHAR(20), outcome VARCHAR(50), reviewed_manually BOOLEAN, at DATETIME)"))
    _add_missing_columns(eng)
    with eng.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(refunds_kyc_signals)"))}
    assert "case_id" in cols


def test_duplicate_webhook_after_insert_returns_existing_case(anon, db):
    payload = _payload(ref="race1", email="race1@example.com")
    assert _post(anon, payload).status_code == 201
    assert _post(anon, payload).status_code == 200


def test_failed_intake_leaves_no_half_case_and_retry_processes(anon, db, monkeypatch):
    from app.apps.kyc import service
    from app.apps.kyc.models import KycCase
    real = service.rescore
    calls = {"n": 0}

    def boom(db_, case):
        calls["n"] += 1
        if calls["n"] == 1:
            raise service.KycError("scoring backend down")
        return real(db_, case)
    monkeypatch.setattr(service, "rescore", boom)
    payload = _payload(ref="half1", email="half1@example.com")
    assert _post(anon, payload).status_code == 400
    assert db.scalar(select(KycCase).where(KycCase.vendor_ref == "half1")) is None
    resp = _post(anon, payload)
    assert resp.status_code == 201
    db.expire_all()
    assert db.scalar(select(KycCase).where(KycCase.vendor_ref == "half1")).status in ("approved", "pending")
