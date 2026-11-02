"""Payment processor adapter.

Production swaps FakeProcessor for a Stripe/Adyen client that forwards `idempotency_key`
to the processor's own idempotency header. The contract is the same: one key, one refund.
"""
import uuid
from typing import Protocol


class ProcessorError(Exception):
    pass


class PaymentProcessor(Protocol):
    def refund(self, charge_id: str, amount_cents: int, idempotency_key: str) -> str: ...


# Module-level so the store survives across FakeProcessor instances within a process,
# the way a real processor remembers keys across API calls.
_REFUNDS: dict[str, dict] = {}


class FakeProcessor:
    """Deterministic stand-in. Charges whose id starts with 'ch_decline' are declined."""

    def refund(self, charge_id: str, amount_cents: int, idempotency_key: str) -> str:
        existing = _REFUNDS.get(idempotency_key)
        if existing is not None:
            if existing["charge_id"] != charge_id or existing["amount_cents"] != amount_cents:
                raise ProcessorError("idempotency key reused with different parameters")
            return existing["refund_id"]
        if amount_cents <= 0:
            raise ProcessorError("amount must be positive")
        if charge_id.startswith("ch_decline"):
            raise ProcessorError("processor declined refund: charge disputed")
        refund_id = "re_" + uuid.uuid4().hex[:20]
        _REFUNDS[idempotency_key] = {"refund_id": refund_id, "charge_id": charge_id, "amount_cents": amount_cents}
        return refund_id


def get_processor() -> PaymentProcessor:
    return FakeProcessor()
