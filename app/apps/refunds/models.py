from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.platform.db import Base
from app.platform.models import utcnow

REASON_CODES = ("duplicate", "not_received", "fraud", "goodwill", "other")
STATUSES = ("requested", "pending_approval", "executed", "rejected", "failed")
OPEN_STATUSES = ("requested", "pending_approval")


class Transaction(Base):
    """A captured card charge. In production this is a read model fed by the payments ledger."""

    __tablename__ = "refunds_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_name: Mapped[str] = mapped_column(String(200), index=True)
    customer_email: Mapped[str] = mapped_column(String(200), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    merchant: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processor_charge_id: Mapped[str] = mapped_column(String(100), unique=True)
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)

    refunds: Mapped[list["RefundRequest"]] = relationship(
        back_populates="transaction", order_by="RefundRequest.id.desc()"
    )

    @property
    def refundable_cents(self) -> int:
        return self.amount_cents - self.refunded_cents


class RefundRequest(Base):
    __tablename__ = "refunds_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("refunds_transactions.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason_code: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="requested")
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    processor_refund_id: Mapped[str | None] = mapped_column(String(100))
    requested_by_email: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Bookkeeping beyond the minimal contract: link to the maker/checker record and outcome details.
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id"))
    required_permission: Mapped[str | None] = mapped_column(String(100))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)

    transaction: Mapped[Transaction] = relationship(back_populates="refunds")


class KycSignal(Base):
    """Populated ONLY by the kyc.decision.v1 subscriber. Refunds never reads KYC tables directly."""

    __tablename__ = "refunds_kyc_signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_email: Mapped[str] = mapped_column(String(200), index=True)
    risk_band: Mapped[str] = mapped_column(String(20))
    outcome: Mapped[str] = mapped_column(String(50))
    reviewed_manually: Mapped[bool] = mapped_column(Boolean, default=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
