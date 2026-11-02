"""Outbox pattern. publish() inside the business transaction; dispatch_pending() after commit.

Apps subscribe by event type. They never read each other's tables.
"""
from collections import defaultdict
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import OutboxEvent, utcnow

Handler = Callable[[Session, dict], None]
_subscribers: dict[str, list[Handler]] = defaultdict(list)


def subscribe(event_type: str, handler: Handler) -> None:
    _subscribers[event_type].append(handler)


def publish(db: Session, event_type: str, payload: dict) -> OutboxEvent:
    ev = OutboxEvent(type=event_type, payload=payload)
    db.add(ev)
    return ev


def dispatch_pending(db: Session) -> int:
    pending = db.scalars(select(OutboxEvent).where(OutboxEvent.dispatched_at.is_(None)).order_by(OutboxEvent.id)).all()
    for ev in pending:
        ev.attempts += 1
        for handler in _subscribers.get(ev.type, []):
            handler(db, ev.payload)
        ev.dispatched_at = utcnow()
    db.commit()
    return len(pending)
