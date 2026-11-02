from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base
from app.platform.models import utcnow

STATUSES = ("pending", "approved", "rejected")


class KycCase(Base):
    """One review case per applicant, created from the KYC vendor's webhook."""

    __tablename__ = "kyc_cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    vendor: Mapped[str] = mapped_column(String(50), default="alloy")
    vendor_ref: Mapped[str] = mapped_column(String(100), unique=True)  # vendor's id; makes webhook retries idempotent
    applicant_name: Mapped[str] = mapped_column(String(200))
    applicant_email: Mapped[str] = mapped_column(String(200), index=True)
    address: Mapped[str] = mapped_column(Text, default="")
    device_fingerprint: Mapped[str] = mapped_column(String(100), default="", index=True)
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    vendor_decision: Mapped[str] = mapped_column(String(20))  # pass | review | fail
    vendor_checks: Mapped[dict] = mapped_column(JSON, default=dict)  # {"document": "pass", "selfie": "pass", ...}
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    risk_band: Mapped[str] = mapped_column(String(20), default="low")
    risk_reasons: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    auto_decided: Mapped[bool] = mapped_column(default=False)
    decided_by_email: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def summary(self) -> dict:
        return {"status": self.status, "risk_band": self.risk_band, "risk_score": self.risk_score}
