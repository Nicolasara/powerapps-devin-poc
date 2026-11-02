"""Feedback: in-app ticket queue. Users report friction on any page; tickets become GitHub issues."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import FeedbackTicket


def register() -> None:
    """No approval appliers or event subscribers needed."""


def seed(db: Session) -> None:
    if db.scalar(select(func.count()).select_from(FeedbackTicket)):
        return
    db.add(FeedbackTicket(
        page_path="/refunds",
        title="Refunds search should also match last 4 digits of card",
        body="Customers usually quote the last 4 digits of their card when they call support. "
             "Today I have to ask for the order ID before I can find the refund.",
        kind="friction",
        status="open",
        submitted_by_email="oscar@corp.test",
    ))
    db.commit()
