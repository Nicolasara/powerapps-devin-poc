from typing import Protocol

from sqlalchemy.orm import Session

from .models import Notification


class Notifier(Protocol):
    def send(self, channel: str, text: str) -> None: ...


class FakeNotifier:
    """Stores messages in the DB so the UI can show what Teams/Slack would have received."""

    def __init__(self, db: Session):
        self.db = db

    def send(self, channel: str, text: str) -> None:
        self.db.add(Notification(channel=channel, text=text))


def get_notifier(db: Session) -> Notifier:
    return FakeNotifier(db)
