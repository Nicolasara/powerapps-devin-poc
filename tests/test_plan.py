from sqlalchemy import select

from app.apps.flags.models import Flag, FlagPlan
from app.platform.models import Approval, AuditEvent


def _flag(db, key, env):
    return db.scalar(select(Flag).where(Flag.key == key, Flag.environment == env))


def _new(client, key):
    return client.post("/flags", data={"key": key, "enabled": "true", "rollout_percent": "100"}, follow_redirects=False)


def test_default_plan_and_position(login, db):
    oscar = login("oscar")
    _new(oscar, "plan_a")
    assert "Step 1 of 5" in oscar.get("/flags").text
    detail = oscar.get(f"/flags/{_flag(db, 'plan_a', 'dev').id}").text
    assert "dev 100 → staging 100 → prod 10 → prod 50 → prod 100" in detail


def test_roll_forward_non_prod_applies_in_one_click(login, db):
    oscar = login("oscar")
    _new(oscar, "plan_b")
    assert oscar.get("/flags/plan/plan_b/forward/confirm").status_code == 404  # no "are you sure?" detour
    assert 'action="/flags/plan/plan_b/forward"' in oscar.get("/flags").text
    resp = oscar.post("/flags/plan/plan_b/forward", follow_redirects=False)
    assert resp.status_code == 303 and "rolled+forward+to+staging+100" in resp.headers["location"]
    db.expire_all()
    staging = _flag(db, "plan_b", "staging")
    assert staging is not None and staging.enabled and staging.rollout_percent == 100
    assert _flag(db, "plan_b", "prod") is None


def test_roll_forward_into_prod_waits_for_approval_then_steps_through(login, db):
    oscar, april = login("oscar"), login("april")
    _new(oscar, "plan_c")
    oscar.post("/flags/plan/plan_c/forward", follow_redirects=False)  # staging
    resp = oscar.post("/flags/plan/plan_c/forward", follow_redirects=False)  # prod 10 -> approval
    assert "Sent+for+approval" in resp.headers["location"]
    db.expire_all()
    assert _flag(db, "plan_c", "prod") is None
    dup = oscar.post("/flags/plan/plan_c/forward", follow_redirects=False)
    assert "awaiting+approval" in dup.headers["location"]
    approval = db.scalar(select(Approval).where(Approval.entity_id == "new:plan_c:prod"))
    april.post(f"/approvals/{approval.id}/decide", data={"decision": "approve"}, follow_redirects=False)
    db.expire_all()
    prod = _flag(db, "plan_c", "prod")
    assert prod.enabled and prod.rollout_percent == 10
    assert "Step 3 of 5" in oscar.get("/flags").text

    resp = oscar.post("/flags/plan/plan_c/forward", follow_redirects=False)  # prod 50 -> approval on existing row
    assert "Sent+for+approval" in resp.headers["location"]
    approval = db.scalars(select(Approval).where(Approval.entity_id == str(prod.id)).order_by(Approval.id.desc())).first()
    april.post(f"/approvals/{approval.id}/decide", data={"decision": "approve"}, follow_redirects=False)
    db.expire_all()
    assert _flag(db, "plan_c", "prod").rollout_percent == 50


def test_roll_back_applies_immediately_even_in_prod(login, db):
    oscar = login("oscar")
    _new(oscar, "plan_d")
    oscar.post("/flags/plan/plan_d/forward", follow_redirects=False)
    db.add(Flag(key="plan_d", environment="prod", enabled=True, rollout_percent=50, updated_by_email="seed"))
    db.commit()
    assert "Step 4 of 5" in oscar.get("/flags").text
    resp = oscar.post("/flags/plan/plan_d/back", follow_redirects=False)
    assert "rolled+back+to+prod+10" in resp.headers["location"]
    db.expire_all()
    assert _flag(db, "plan_d", "prod").rollout_percent == 10
    oscar.post("/flags/plan/plan_d/back", follow_redirects=False)
    db.expire_all()
    prod = _flag(db, "plan_d", "prod")
    assert not prod.enabled and prod.rollout_percent == 0
    assert db.scalar(select(Approval).where(Approval.entity_id == str(prod.id))) is None
    assert db.scalar(select(AuditEvent).where(AuditEvent.entity_id == str(prod.id), AuditEvent.action == "flag.updated"))


def test_roll_back_at_start_is_rejected(login, db):
    oscar = login("oscar")
    oscar.post("/flags", data={"key": "plan_e", "enabled": "false", "rollout_percent": "0"}, follow_redirects=False)
    resp = oscar.post("/flags/plan/plan_e/back", follow_redirects=False)
    assert "nothing+to+roll+back" in resp.headers["location"]


def test_custom_plan_saved_and_validated(login, db):
    oscar = login("oscar")
    _new(oscar, "plan_f")
    resp = oscar.post("/flags/plan/plan_f", data={"plan": "dev 100 → staging 50 → staging 100 → prod 5 → prod 100"},
                      follow_redirects=False)
    assert "saved" in resp.headers["location"]
    db.expire_all()
    row = db.scalar(select(FlagPlan).where(FlagPlan.key == "plan_f"))
    assert [s["rollout_percent"] for s in row.steps] == [100, 50, 100, 5, 100]
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "flag.plan_updated", AuditEvent.entity_id == "plan_f"))
    bad = oscar.post("/flags/plan/plan_f", data={"plan": "prod 50 → dev 100"}, follow_redirects=False)
    assert "Plan+not+saved" in bad.headers["location"]
    bad = oscar.post("/flags/plan/plan_f", data={"plan": "prod 50 → prod 20"}, follow_redirects=False)
    assert "Plan+not+saved" in bad.headers["location"]
    assert "does+not+exist" in oscar.post("/flags/plan/nope", data={"plan": "dev 100"}, follow_redirects=False).headers["location"]


def test_viewer_cannot_roll(login, db):
    oscar = login("oscar")
    _new(oscar, "plan_g")
    assert login("alice").post("/flags/plan/plan_g/forward", follow_redirects=False).status_code == 403
