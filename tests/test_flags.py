from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.apps.flags.models import Flag
from app.platform.models import Approval, AuditEvent, OutboxEvent


@pytest.fixture
def flags(db) -> dict[str, int]:
    dev = Flag(key="instant_transfers", environment="dev", enabled=False, rollout_percent=100, owner_team="payments")
    prod = Flag(key="instant_transfers", environment="prod", enabled=False, rollout_percent=25, owner_team="payments")
    db.add_all([dev, prod])
    db.commit()
    return {"dev": dev.id, "prod": prod.id}


def _flag(db, flag_id: int) -> Flag:
    db.expire_all()
    return db.get(Flag, flag_id)


def _pending(db, flag_id: int) -> list[Approval]:
    db.expire_all()
    return db.scalars(select(Approval).where(Approval.kind == "flag.change", Approval.entity_id == str(flag_id),
                                             Approval.status == "pending")).all()


def _query(resp) -> dict[str, list[str]]:
    return parse_qs(urlparse(resp.headers["location"]).query)


def test_dev_toggle_applies_in_one_click(login, db, flags):
    oscar = login("oscar")
    listing = oscar.get("/flags")
    assert f'<form class="inline" method="post" action="/flags/{flags["dev"]}/toggle">' in listing.text
    assert oscar.get(f"/flags/{flags['dev']}/toggle/confirm").status_code == 404

    resp = oscar.post(f"/flags/{flags['dev']}/toggle", follow_redirects=False)
    assert resp.status_code == 303

    flag = _flag(db, flags["dev"])
    assert flag.enabled is True
    assert flag.updated_by_email == "oscar@corp.test"
    assert _pending(db, flags["dev"]) == []
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "flag.updated",
                                              AuditEvent.entity_id == str(flags["dev"]))) is not None


def test_viewer_cannot_toggle(login, db, flags):
    resp = login("alice").post(f"/flags/{flags['dev']}/toggle", follow_redirects=False)
    assert resp.status_code == 403
    assert _flag(db, flags["dev"]).enabled is False


def test_prod_toggle_creates_pending_approval_without_changing_flag(login, db, flags):
    resp = login("oscar").post(f"/flags/{flags['prod']}/toggle", follow_redirects=False)
    assert resp.status_code == 303
    assert "Sent for approval" in _query(resp)["notice"][0]

    assert _flag(db, flags["prod"]).enabled is False
    pending = _pending(db, flags["prod"])
    assert len(pending) == 1
    assert pending[0].payload == {"enabled": True}
    assert pending[0].required_permission == "flags.approve"


def test_prod_change_maker_cannot_approve_but_approver_can(login, db, flags, user_ids):
    oscar, april = login("oscar"), login("april")
    oscar.post(f"/flags/{flags['prod']}/toggle", follow_redirects=False)
    (approval,) = _pending(db, flags["prod"])

    # Maker tries to approve their own change.
    resp = oscar.post(f"/approvals/{approval.id}/decide", data={"decision": "approve"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "maker cannot be checker" in _query(resp)["error"][0]
    assert _flag(db, flags["prod"]).enabled is False
    assert len(_pending(db, flags["prod"])) == 1

    # A different user with flags.approve signs off.
    resp = april.post(f"/approvals/{approval.id}/decide", data={"decision": "approve", "reason": "LGTM"},
                      follow_redirects=False)
    assert resp.status_code == 303
    assert "error" not in _query(resp)

    flag = _flag(db, flags["prod"])
    assert flag.enabled is True
    assert flag.updated_by_email == "april@corp.test"
    assert _pending(db, flags["prod"]) == []
    decided = db.get(Approval, approval.id)
    assert decided.status == "approved"
    assert decided.decided_by == user_ids["april"]

    change = db.scalar(select(AuditEvent).where(AuditEvent.action == "flag.updated", AuditEvent.entity_type == "flag",
                                                AuditEvent.entity_id == str(flags["prod"])))
    assert change is not None
    assert change.actor_email == "april@corp.test"
    assert change.before == {"enabled": False, "rollout_percent": 25}
    assert change.after == {"enabled": True, "rollout_percent": 25}
    assert change.reason == f"approval #{approval.id}"
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "approval.approved",
                                              AuditEvent.entity_id == str(approval.id))) is not None

    outbox = db.scalars(select(OutboxEvent).where(OutboxEvent.type == "flag.changed.v1")).all()
    prod_events = [e for e in outbox if e.payload["environment"] == "prod"]
    assert len(prod_events) == 1
    assert prod_events[0].payload["key"] == "instant_transfers"
    assert prod_events[0].payload["enabled"] is True
    assert prod_events[0].dispatched_at is not None


def test_approver_cannot_approve_own_prod_change(login, db, flags):
    april = login("april")
    april.post(f"/flags/{flags['prod']}/toggle", follow_redirects=False)
    (approval,) = _pending(db, flags["prod"])
    resp = april.post(f"/approvals/{approval.id}/decide", data={"decision": "approve"}, follow_redirects=False)
    assert "maker cannot be checker" in _query(resp)["error"][0]
    assert _flag(db, flags["prod"]).enabled is False


def test_operator_cannot_approve_others_prod_change(login, db, flags):
    login("april").post(f"/flags/{flags['prod']}/toggle", follow_redirects=False)
    (approval,) = _pending(db, flags["prod"])
    resp = login("oscar").post(f"/approvals/{approval.id}/decide", data={"decision": "approve"},
                               follow_redirects=False)
    assert "requires flags.approve" in _query(resp)["error"][0]
    assert _flag(db, flags["prod"]).enabled is False


def test_snapshot_etag_and_304(anon, login, db, flags):
    resp = anon.get("/flags/api/snapshot", params={"environment": "prod"})
    assert resp.status_code == 200
    etag = resp.headers["etag"]
    assert etag.startswith('"')
    assert etag.endswith('"')
    assert resp.json()["flags"] == {"instant_transfers": {"enabled": False, "rollout_percent": 25}}

    cached = anon.get("/flags/api/snapshot", params={"environment": "prod"}, headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.headers["etag"] == etag
    assert cached.content == b""

    # A dev change does not touch the prod snapshot; a changed snapshot gets a new ETag.
    login("oscar").post(f"/flags/{flags['dev']}/toggle", follow_redirects=False)
    assert anon.get("/flags/api/snapshot", params={"environment": "prod"},
                    headers={"If-None-Match": etag}).status_code == 304
    dev = anon.get("/flags/api/snapshot", params={"environment": "dev"}, headers={"If-None-Match": etag})
    assert dev.status_code == 200
    assert dev.headers["etag"] != etag

    prod = _flag(db, flags["prod"])
    prod.rollout_percent = 50
    db.commit()
    fresh = anon.get("/flags/api/snapshot", params={"environment": "prod"}, headers={"If-None-Match": etag})
    assert fresh.status_code == 200
    assert fresh.headers["etag"] != etag
    assert fresh.json()["flags"]["instant_transfers"]["rollout_percent"] == 50


def test_snapshot_rejects_unknown_environment(anon):
    assert anon.get("/flags/api/snapshot", params={"environment": "qa"}).status_code == 400
