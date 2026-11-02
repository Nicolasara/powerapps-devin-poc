from sqlalchemy import select

from app.apps.flags.models import Flag
from app.platform import rbac


def test_role_permission_matrix():
    assert not rbac.has_permission("viewer", "flags.write")
    assert rbac.has_permission("operator", "flags.write")
    assert not rbac.has_permission("operator", "flags.approve")
    assert rbac.has_permission("approver", "refunds.approve")
    assert not rbac.has_permission("approver", "refunds.approve_large")
    assert rbac.has_permission("admin", "refunds.approve_large")


def test_anonymous_is_unauthorized(anon):
    resp = anon.get("/flags", headers={"accept": "application/json"})
    assert resp.status_code == 401


def test_anonymous_browser_is_sent_to_login(anon):
    resp = anon.get("/flags", headers={"accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_viewer_cannot_create_flag(login, db):
    alice = login("alice")
    resp = alice.post("/flags", data={"key": "viewer_flag", "environment": "dev"}, follow_redirects=False)
    assert resp.status_code == 403
    assert db.scalar(select(Flag).where(Flag.key == "viewer_flag")) is None


def test_operator_can_create_dev_flag(login, db):
    oscar = login("oscar")
    resp = oscar.post(
        "/flags",
        data={"key": "new_checkout", "environment": "dev", "description": "New checkout", "owner_team": "web",
              "enabled": "true", "rollout_percent": "50"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    flag = db.scalar(select(Flag).where(Flag.key == "new_checkout", Flag.environment == "dev"))
    assert flag is not None
    assert flag.enabled is True
    assert flag.rollout_percent == 50
    assert flag.updated_by_email == "oscar@corp.test"
    assert resp.headers["location"].startswith(f"/flags/{flag.id}")
