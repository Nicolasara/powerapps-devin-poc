from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base
from app.platform.models import utcnow

ENVIRONMENTS = ("dev", "staging", "prod")


class Flag(Base):
    """One row per (key, environment). The same key usually exists in every environment."""

    __tablename__ = "flags"
    __table_args__ = (UniqueConstraint("key", "environment", name="uq_flags_key_environment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    owner_team: Mapped[str] = mapped_column(String(100), default="")
    environment: Mapped[str] = mapped_column(String(20))  # dev|staging|prod
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rollout_percent: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by_email: Mapped[str] = mapped_column(String(200), default="system")

    def state(self) -> dict:
        return {"enabled": self.enabled, "rollout_percent": self.rollout_percent}
