"""Create tables and demo data.

    python -m app.seed          create missing tables, insert demo rows where empty
    python -m app.seed reset    drop everything and start fresh
"""
import sys

from sqlalchemy import select

from app.apps import feedback, flags, refunds
from app.apps.feedback import models as feedback_models
from app.apps.flags import models as flags_models
from app.apps.refunds import models as refunds_models
from app.platform.db import Base, SessionLocal, engine
from app.platform.models import User

# Every app's models must be imported so Base.metadata knows all tables.
MODEL_MODULES = (flags_models, refunds_models, feedback_models)

USERS = [
    ("alice@corp.test", "Alice Viewer", "viewer"),
    ("oscar@corp.test", "Oscar Operator", "operator"),
    ("april@corp.test", "April Approver", "approver"),
    ("adam@corp.test", "Adam Admin", "admin"),
]


def seed_users(db) -> None:
    existing = set(db.scalars(select(User.email)).all())
    for email, name, role in USERS:
        if email not in existing:
            db.add(User(email=email, name=name, role=role))
    db.commit()


def run(reset: bool = False) -> None:
    if reset:
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    from app.main import register_apps  # once-per-process guard shared with the web app

    register_apps()
    db = SessionLocal()
    try:
        seed_users(db)
        flags.seed(db)
        refunds.seed(db)
        feedback.seed(db)
    finally:
        db.close()


def main(argv: list[str]) -> None:
    reset = "reset" in argv
    run(reset=reset)
    print(f"Database {'reset and ' if reset else ''}seeded: {engine.url}")


if __name__ == "__main__":
    main(sys.argv[1:])
