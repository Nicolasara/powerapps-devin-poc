from sqlalchemy.orm import Session

from .models import AuditEvent, User


def audit(db: Session, actor: User | None, action: str, entity_type: str, entity_id: str | int,
          before: dict | None = None, after: dict | None = None, reason: str | None = None) -> AuditEvent:
    ev = AuditEvent(
        actor_id=actor.id if actor else None,
        actor_email=actor.email if actor else "system",
        action=action, entity_type=entity_type, entity_id=str(entity_id),
        before=before, after=after, reason=reason,
    )
    db.add(ev)
    return ev
