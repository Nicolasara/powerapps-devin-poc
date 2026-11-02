"""Feature flag admin: per-environment flags, prod changes via maker/checker, SDK snapshot endpoint."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.platform.audit import audit
from app.platform.models import User

from . import service
from .models import Flag

# key, description, owner_team, {env: (enabled, rollout_percent)}
_SEED = [
    ("instant_transfers", "Real-time P2P transfers over RTP/FedNow rails", "payments",
     {"dev": (True, 100), "staging": (True, 100), "prod": (True, 25)}),
    ("new_kyc_vendor_alloy", "Route new-customer KYC checks to Alloy instead of the legacy vendor", "onboarding",
     {"dev": (True, 100), "staging": (True, 50), "prod": (False, 0)}),
    ("refunds_v2_ui", "New refunds console for support agents", "support-tools",
     {"dev": (True, 100), "staging": (False, 0)}),
    ("crypto_trading_ny", "Enable crypto trading for New York residents (pending NYDFS BitLicense)", "crypto",
     {"dev": (True, 100), "prod": (False, 0)}),
    ("card_freeze_self_serve", "Let cardholders freeze and unfreeze cards in the mobile app", "cards",
     {"dev": (True, 100), "staging": (True, 100), "prod": (True, 100)}),
    ("dark_mode", "Dark theme in the customer web app", "web-platform",
     {"dev": (True, 100), "staging": (False, 0)}),
    ("ach_same_day", "Offer same-day ACH for outgoing transfers under $100k", "payments",
     {"dev": (True, 100), "staging": (True, 100), "prod": (True, 10)}),
    ("sanctions_rescreen_daily", "Daily OFAC rescreen of the full customer base", "compliance",
     {"dev": (False, 0), "staging": (True, 100), "prod": (True, 100)}),
]


def register() -> None:
    service.register_appliers()


def seed(db: Session) -> None:
    if db.scalar(select(func.count(Flag.id))):
        return
    owner = db.scalar(select(User).where(User.email == "oscar@corp.test"))
    for key, description, team, envs in _SEED:
        for env, (enabled, pct) in envs.items():
            flag = Flag(key=key, description=description, owner_team=team, environment=env, enabled=enabled,
                        rollout_percent=pct, updated_by_email=owner.email if owner else "system")
            db.add(flag)
            db.flush()
            audit(db, owner, "flag.created", "flag", flag.id,
                  after={"key": key, "environment": env, **flag.state()}, reason="seed")
    db.commit()
