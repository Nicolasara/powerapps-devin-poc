from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base
from app.platform.models import utcnow

KINDS = ("friction", "bug", "idea")
STATUSES = ("open", "filed", "done")


class FeedbackTicket(Base):
    """In-app feedback queue. Tickets can be promoted to GitHub issues for Devin to pick up."""

    __tablename__ = "feedback_tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    page_path: Mapped[str] = mapped_column(String(500), default="")
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="friction")  # friction|bug|idea
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|filed|done
    submitted_by_email: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    github_issue_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
