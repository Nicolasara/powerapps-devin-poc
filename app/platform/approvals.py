"""Maker/checker state machine shared by every app.

request() either applies immediately (no permission required) or parks the change as pending.
decide() enforces: checker holds the required permission and is not the maker.
"""
from collections.abc import Callable

from sqlalchemy.orm import Session

from . import rbac
from .audit import audit
from .models import Approval, User, utcnow

Applier = Callable[[Session, Approval, User], None]
_appliers: dict[str, Applier] = {}


class ApprovalError(Exception):
    pass


def register_applier(kind: str, fn: Applier) -> None:
    _appliers[kind] = fn


def request(db: Session, actor: User, kind: str, entity_type: str, entity_id: str | int, payload: dict,
            summary: str, required_permission: str | None) -> Approval | None:
    if required_permission is None:
        a = Approval(kind=kind, entity_type=entity_type, entity_id=str(entity_id), payload=payload, summary=summary,
                     required_permission="", status="approved", requested_by=actor.id, decided_by=None,
                     decided_at=utcnow(), decision_reason="auto: below threshold")
        db.add(a)
        db.flush()
        _appliers[kind](db, a, actor)
        return a
    a = Approval(kind=kind, entity_type=entity_type, entity_id=str(entity_id), payload=payload, summary=summary,
                 required_permission=required_permission, requested_by=actor.id)
    db.add(a)
    db.flush()
    audit(db, actor, "approval.requested", "approval", a.id, after={"kind": kind, "summary": summary})
    return a


def decide(db: Session, actor: User, approval: Approval, approve: bool, reason: str | None = None) -> Approval:
    if approval.status != "pending":
        raise ApprovalError("already decided")
    if approval.requested_by == actor.id:
        raise ApprovalError("maker cannot be checker")
    if not rbac.has_permission(actor.role, approval.required_permission):
        raise ApprovalError(f"requires {approval.required_permission}")
    approval.status = "approved" if approve else "rejected"
    approval.decided_by = actor.id
    approval.decided_at = utcnow()
    approval.decision_reason = reason
    audit(db, actor, f"approval.{approval.status}", "approval", approval.id, after={"summary": approval.summary}, reason=reason)
    if approve:
        _appliers[approval.kind](db, approval, actor)
    return approval
