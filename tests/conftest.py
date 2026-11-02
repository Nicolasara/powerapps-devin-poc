"""Test harness: a throwaway SQLite file, fresh tables per test, the four demo users.

DATABASE_URL is set before anything under `app` is imported, because app.platform.db builds its engine at import.
"""
import os
import shutil
import sys
import tempfile
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_DIR = tempfile.mkdtemp(prefix="poc-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP_DIR) / 'test.db'}"

with warnings.catch_warnings():
    # Starlette warns about its httpx-based TestClient; irrelevant here.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app, register_apps
from app.platform.db import Base, SessionLocal, engine
from app.platform.models import User
from app.seed import seed_users


def pytest_sessionfinish(session, exitstatus) -> None:
    engine.dispose()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def fresh_db() -> Iterator[None]:
    """Every test starts from empty tables plus the four users."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    register_apps()
    with SessionLocal() as s:
        seed_users(s)
    yield


@pytest.fixture
def db() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def login() -> Callable[[str], TestClient]:
    """login("oscar") -> a TestClient holding that user's session cookie (alice/oscar/april/adam)."""

    def _login(name: str) -> TestClient:
        with SessionLocal() as s:
            user = s.scalar(select(User).where(User.email == f"{name}@corp.test"))
            assert user is not None, name
            user_id = user.id
        client = TestClient(app)
        resp = client.post("/login", data={"user_id": str(user_id), "next": "/"}, follow_redirects=False)
        assert resp.status_code == 303
        return client

    return _login


@pytest.fixture
def anon() -> TestClient:
    return TestClient(app)


@pytest.fixture
def user_ids() -> dict[str, int]:
    """{"alice": id, "oscar": id, "april": id, "adam": id}"""
    with SessionLocal() as s:
        return {u.email.split("@")[0]: u.id for u in s.scalars(select(User))}
