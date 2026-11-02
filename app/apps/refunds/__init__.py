"""Refunds: look up a charge, request a refund, route it through maker/checker, execute idempotently.

Replaces a Power Apps canvas app + Power Automate flow. Thresholds, idempotency and audit are code.
"""
import random
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.platform import approvals, events
from app.platform.models import Approval, User, utcnow

from . import models, service

_registered = False


def register() -> None:
    global _registered
    approvals.register_applier(service.KIND, service.apply_refund)
    if _registered:
        return
    events.subscribe("kyc.decision.v1", service.on_kyc_decision)
    _registered = True


_FIRST = ["Maya", "Liam", "Sofia", "Noah", "Ava", "Ethan", "Isla", "Lucas", "Chloe", "Mateo", "Zara", "Owen",
          "Priya", "Jonas", "Hana", "Diego", "Leah", "Kofi", "Nina", "Ravi", "Elena", "Tomas", "Aisha", "Felix", "Grace"]
_LAST = ["Chen", "Patel", "Okafor", "Garcia", "Nguyen", "Kowalski", "Haddad", "Silva", "Morgan", "Ivanova", "Brooks",
         "Tanaka", "Mensah", "Larsen", "Romero", "Fischer", "Ahmed", "Dubois", "Kim", "Rossi", "Novak", "Walsh",
         "Costa", "Berg", "Reyes"]
_MERCHANTS = ["Northwind Grocers", "Contoso Travel", "Fabrikam Electronics", "Tailspin Air", "Adventure Works Outdoor",
              "Litware Software", "Wingtip Toys", "Proseware Fitness", "Coho Winery", "Lamna Healthcare"]


def seed(db: Session) -> None:
    register()
    if db.scalar(select(func.count()).select_from(models.Transaction)):
        return
    rng = random.Random(20240611)
    now = utcnow()

    customers = [(f"{f} {lname}", f"{f.lower()}.{lname.lower()}@example.com") for f, lname in zip(_FIRST, _LAST)]
    txs: list[models.Transaction] = []
    for i in range(40):
        name, email = customers[i % len(customers)] if i < len(customers) else rng.choice(customers)
        amount = rng.choice([rng.randint(500, 9_900), rng.randint(10_000, 99_900), rng.randint(100_000, 250_000)])
        tx = models.Transaction(
            customer_name=name, customer_email=email, amount_cents=amount, currency="USD",
            merchant=rng.choice(_MERCHANTS), created_at=now - timedelta(days=rng.randint(0, 45), minutes=rng.randint(0, 1440)),
            processor_charge_id=("ch_decline_" if i == 7 else "ch_") + uuid.uuid4().hex[:20], refunded_cents=0,
        )
        db.add(tx)
        txs.append(tx)
    # Make sure the demo has charges in every threshold band.
    txs[0].amount_cents, txs[1].amount_cents, txs[2].amount_cents = 4_250, 38_900, 245_000
    for i, cents in {3: 5_000, 4: 7_999, 5: 52_000, 6: 12_500, 7: 6_400}.items():
        txs[i].amount_cents = cents
    db.flush()

    users = {u.email: u for u in db.scalars(select(User)).all()}
    oscar, april = users.get("oscar@corp.test"), users.get("april@corp.test")
    adam = users.get("adam@corp.test")
    if oscar and april:
        def req(actor, tx, cents, reason, note):
            return service.create_request(db, actor, tx, cents, reason, note, str(uuid.uuid4()))

        req(oscar, txs[3], 2_500, "duplicate", "Customer charged twice at checkout")
        req(oscar, txs[4], 1_999, "goodwill", "Late delivery, goodwill credit")
        req(oscar, txs[7], 6_400, "not_received", "Item never arrived")  # processor declines -> failed
        approved = req(oscar, txs[1], 15_000, "not_received", "Partial: one of two items missing")
        decided = db.get(Approval, approved.approval_id)
        approvals.decide(db, april, decided, True, "Tracking confirms one parcel lost")
        req(oscar, txs[5], 18_000, "fraud", "Customer reports card not present")
        req(april, txs[2], 120_000, "fraud", "Account takeover confirmed by fraud team")
        if adam:
            req(adam, txs[6], 2_000, "other", "Price-match adjustment")

    kyc = [
        (txs[5].customer_email, "medium", "approved", False, now - timedelta(days=30)),
        (txs[9].customer_email, "low", "approved", False, now - timedelta(days=4)),
    ]
    for email, band, outcome, manual, at in kyc:
        events.publish(db, "kyc.decision.v1", {"customer_email": email, "risk_band": band, "outcome": outcome,
                                               "reviewed_manually": manual, "at": at.isoformat()})
    db.commit()
    events.dispatch_pending(db)
