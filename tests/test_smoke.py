"""Full demo seed, then every page as every role."""
import pytest
from sqlalchemy import func, select

from app import seed
from app.apps.feedback.models import FeedbackTicket
from app.apps.flags.models import Flag
from app.apps.kyc.models import KycCase
from app.apps.refunds.models import RefundRequest, Transaction
from app.platform.models import Approval

PAGES = [
    "/", "/flags", "/flags?environment=prod", "/flags/{flag}", "/flags/api/snapshot?environment=dev",
    "/refunds", "/refunds?q=chen", "/refunds?page=2", "/refunds/tx/{tx}", "/refunds/requests/{rr}",
    "/approvals", "/audit?page=2", "/audit?entity_type=flag&page=1",
    "/feedback", "/feedback?status=open", "/feedback/new?page=/flags", "/feedback/{ticket}",
    "/audit", "/audit?entity_type=flag", "/notifications", "/login",
]
WRITER_PAGES = ["/flags/new", "/flags/{flag}/toggle/confirm"]
REVIEWER_PAGES = ["/kyc", "/kyc?status=all", "/kyc?status=pending&risk=high", "/kyc?q=reed", "/kyc/{case}"]


@pytest.fixture
def demo(db) -> dict[str, int]:
    seed.run()
    return {
        "flag": db.scalar(select(func.min(Flag.id))),
        "tx": db.scalar(select(func.min(Transaction.id))),
        "rr": db.scalar(select(func.min(RefundRequest.id))),
        "ticket": db.scalar(select(func.min(FeedbackTicket.id))),
        "case": db.scalar(select(func.min(KycCase.id))),
    }


def test_seed_is_idempotent(db, demo):
    counts = [db.scalar(select(func.count()).select_from(m)) for m in (Flag, Transaction, RefundRequest, Approval)]
    seed.run()
    db.expire_all()
    assert [db.scalar(select(func.count()).select_from(m)) for m in (Flag, Transaction, RefundRequest, Approval)] \
        == counts
    assert all(counts)
    assert db.scalar(select(func.count()).select_from(Approval).where(Approval.status == "pending")) >= 2


@pytest.mark.parametrize("name", ["alice", "oscar", "april", "adam"])
def test_every_page_renders_for_every_role(login, demo, name):
    client = login(name)
    for page in PAGES:
        resp = client.get(page.format(**demo), headers={"accept": "text/html"})
        assert resp.status_code == 200, (name, page, resp.status_code)
    for page in WRITER_PAGES:
        resp = client.get(page.format(**demo), headers={"accept": "text/html"})
        assert resp.status_code == (403 if name == "alice" else 200), (name, page, resp.status_code)
    for page in REVIEWER_PAGES:
        resp = client.get(page.format(**demo), headers={"accept": "text/html"})
        assert resp.status_code == (200 if name in ("april", "adam") else 403), (name, page, resp.status_code)


def test_feedback_link_is_in_the_footer(login, demo):
    page = login("oscar").get("/flags", headers={"accept": "text/html"})
    assert "/feedback/new?page=" in page.text
