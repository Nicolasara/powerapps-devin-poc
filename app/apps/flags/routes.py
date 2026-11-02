from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform import events
from app.platform.auth import require
from app.platform.db import get_db
from app.platform.models import AuditEvent, User
from app.platform.tables import sort_url_factory
from app.web import render

from . import service
from .models import ENVIRONMENTS, Flag

router = APIRouter(prefix="/flags", tags=["flags"])

DB = Annotated[Session, Depends(get_db)]
Reader = Annotated[User, Depends(require("flags.read"))]
Writer = Annotated[User, Depends(require("flags.write"))]


def _redirect(path: str, notice: str | None = None) -> RedirectResponse:
    url = f"{path}?{urlencode({'notice': notice})}" if notice else path
    return RedirectResponse(url, status_code=303)


def _get_flag(db: Session, flag_id: int) -> Flag:
    flag = db.get(Flag, flag_id)
    if flag is None:
        raise HTTPException(status_code=404, detail="flag not found")
    return flag


def _commit(db: Session) -> None:
    db.commit()
    events.dispatch_pending(db)


@router.get("/api/snapshot")
def snapshot(request: Request, db: DB, environment: Annotated[str, Query()] = "prod"):
    """Read-only flag snapshot polled by service SDKs.

    Open for the prototype; in production this would require a per-service token.
    """
    if environment not in ENVIRONMENTS:
        raise HTTPException(status_code=400, detail=f"environment must be one of {', '.join(ENVIRONMENTS)}")
    body, etag = service.snapshot(db, environment)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)


@router.get("")
def list_flags(request: Request, user: Reader, db: DB, environment: str | None = None,
               notice: str | None = None, sort: str = "", dir: str = ""):
    env = environment if environment in ENVIRONMENTS else None
    q = select(Flag).order_by(Flag.key, Flag.environment)
    if env:
        q = q.where(Flag.environment == env)
    flags = db.scalars(q).all()
    rows = service.by_key(db)
    if env:
        rows = [r for r in rows if r["envs"].get(env)]
    sort = sort if sort in ("flag", "owner", "stage") else "flag"
    dir = dir if dir in ("asc", "desc") else "asc"
    keys = {"flag": lambda r: r["key"], "owner": lambda r: (r["owner_team"] or "", r["key"]),
            "stage": lambda r: (sum(1 for f in r["envs"].values() if f), r["key"])}
    rows.sort(key=keys[sort], reverse=(dir == "desc"))
    sort_url = sort_url_factory("/flags", {"environment": env}, sort, dir)
    plans = {r["key"]: service.plan_state(db, r["key"], {e: f for e, f in r["envs"].items() if f}) for r in rows}
    if sort == "stage":
        rows.sort(key=lambda r: (plans[r["key"]]["position"], r["key"]), reverse=(dir == "desc"))
    # Every environment is shown on each row, so pending must cover all of them, not just the filtered env.
    pending = service.pending_by_flag(db, [f.id for r in rows for f in r["envs"].values() if f])
    pending_new = service.pending_new(db)
    return render(request, "flags/list.html", user, flags=flags, rows=rows, environment=env,
                  environments=ENVIRONMENTS, pending=pending, pending_new=pending_new, notice=notice,
                  sort=sort, dir=dir, sort_url=sort_url, plans=plans)


@router.get("/new")
def new_flag(request: Request, user: Writer):
    return render(request, "flags/new.html", user, environments=ENVIRONMENTS, form={}, error=None)


@router.post("")
def create_flag(request: Request, user: Writer, db: DB, key: Annotated[str, Form()],
                description: Annotated[str, Form()] = "",
                owner_team: Annotated[str, Form()] = "", enabled: Annotated[bool, Form()] = False,
                rollout_percent: Annotated[str, Form()] = "0"):
    try:
        flag = service.create_key(db, user, key, description, owner_team, enabled, rollout_percent)
    except service.FlagError as e:
        db.rollback()
        form = {"key": key, "description": description, "owner_team": owner_team,
                "enabled": enabled, "rollout_percent": rollout_percent}
        resp = render(request, "flags/new.html", user, environments=ENVIRONMENTS, form=form, error=str(e))
        resp.status_code = 400
        return resp
    _commit(db)
    return _redirect(f"/flags/{flag.id}", f"Flag created in {flag.environment}; promote it forward when ready.")


@router.post("/promote")
def promote_flag(key: Annotated[str, Form()], to_env: Annotated[str, Form()], user: Writer, db: DB):
    try:
        target, approval = service.promote(db, user, key, to_env)
    except service.FlagError as e:
        db.rollback()
        return _redirect("/flags", str(e))
    _commit(db)
    if approval is not None and approval.status == "pending":
        return _redirect("/flags", f"Sent for approval: promote {key} to prod.")
    return _redirect("/flags", f"{key} promoted to {target.environment}: {'ON' if target.enabled else 'OFF'} at "
                     f"{target.rollout_percent}%.")


@router.get("/plan/{key}/forward/confirm")
def confirm_roll_forward(request: Request, key: str, user: Writer, db: DB):
    """Non-prod roll forward asks "are you sure?" first (prod goes to approval anyway, so it skips this)."""
    state = _plan_or_404(db, key)
    if state["next"] is None or state["next"]["environment"] == "prod":
        return _redirect("/flags", "Nothing to confirm for this step.")
    first = db.scalar(select(Flag).where(Flag.key == key).order_by(Flag.id))
    return render(request, "flags/confirm_forward.html", user, key=key, plan=state, first=first)


@router.post("/plan/{key}/forward")
def roll_forward(key: str, user: Writer, db: DB, back_to: Annotated[str, Form()] = "/flags"):
    try:
        step, approval = service.roll_forward(db, user, key)
    except service.FlagError as e:
        db.rollback()
        return _redirect(back_to, str(e))
    _commit(db)
    where = f"{step['environment']} {step['rollout_percent']}%"
    if approval is not None and approval.status == "pending":
        return _redirect(back_to, f"Sent for approval: roll {key} forward to {where}.")
    return _redirect(back_to, f"{key} rolled forward to {where}.")


@router.post("/plan/{key}/back")
def roll_back(key: str, user: Writer, db: DB, back_to: Annotated[str, Form()] = "/flags"):
    try:
        result = service.roll_back(db, user, key)
    except service.FlagError as e:
        db.rollback()
        return _redirect(back_to, str(e))
    _commit(db)
    return _redirect(back_to, f"{key} rolled back to {result['target']}.")


@router.post("/plan/{key}")
def update_plan(key: str, plan: Annotated[str, Form()], user: Writer, db: DB, back_to: Annotated[str, Form()] = "/flags"):
    try:
        service.set_plan(db, user, key, service.parse_plan(plan))
    except service.FlagError as e:
        db.rollback()
        return _redirect(back_to, f"Plan not saved: {e}")
    _commit(db)
    return _redirect(back_to, f"Rollout plan for {key} saved.")


def _plan_or_404(db: Session, key: str) -> dict:
    if not db.scalar(select(Flag.id).where(Flag.key == key)):
        raise HTTPException(status_code=404, detail="flag not found")
    return service.plan_state(db, key)


@router.get("/{flag_id}")
def flag_detail(request: Request, flag_id: int, user: Reader, db: DB, notice: str | None = None):
    """One page per key: every environment is a stage on the same page."""
    flag = _get_flag(db, flag_id)
    stages = {f.environment: f for f in db.scalars(select(Flag).where(Flag.key == flag.key))}
    ids = [str(f.id) for f in stages.values()]
    history = db.scalars(
        select(AuditEvent).where(AuditEvent.entity_type == "flag", AuditEvent.entity_id.in_(ids))
        .order_by(AuditEvent.at.desc(), AuditEvent.id.desc())
    ).all()
    env_of = {str(f.id): f.environment for f in stages.values()}
    pending = service.pending_by_flag(db, [f.id for f in stages.values()])
    pending_new = {env: a for (k, env), a in service.pending_new(db).items() if k == flag.key}
    plan = service.plan_state(db, flag.key, stages)
    return render(request, "flags/detail.html", user, flag=flag, stages=stages, environments=ENVIRONMENTS,
                  history=history, env_of=env_of, pending=pending, pending_new=pending_new, notice=notice, plan=plan)


@router.get("/{flag_id}/toggle/confirm")
def confirm_toggle(request: Request, flag_id: int, user: Writer, db: DB):
    flag = _get_flag(db, flag_id)
    return render(request, "flags/confirm_toggle.html", user, flag=flag)


@router.post("/{flag_id}/toggle")
def toggle_flag(flag_id: int, user: Writer, db: DB):
    flag = _get_flag(db, flag_id)
    if flag.environment == "prod" and service.pending_by_flag(db, [flag.id]):
        return _redirect("/flags", f"{flag.key} in prod already has a change awaiting approval.")
    live = flag.enabled and flag.rollout_percent > 0  # same notion of "on" the switch displays
    target = not live
    payload: dict = {"enabled": target}
    if target and flag.rollout_percent == 0:
        payload["rollout_percent"] = 100  # a switch flipped on means "live", not "on at 0%"
    summary = f"{'Enable' if target else 'Disable'} {flag.key} in {flag.environment}"
    approval = service.request_change(db, user, flag, payload, summary)
    _commit(db)
    if approval is not None and approval.status == "pending":
        return _redirect("/flags", f"Sent for approval: {summary}.")
    return _redirect("/flags", f"{flag.key} in {flag.environment} is now {'ON' if flag.enabled else 'OFF'}.")


@router.post("/{flag_id}/rollout")
def set_rollout(flag_id: int, rollout_percent: Annotated[str, Form()], user: Writer, db: DB):
    flag = _get_flag(db, flag_id)
    try:
        pct = service.validate_rollout(rollout_percent)
    except service.FlagError as e:
        return _redirect(f"/flags/{flag.id}", str(e))
    if pct == flag.rollout_percent and flag.enabled == (pct > 0):
        return _redirect(f"/flags/{flag.id}", f"Rollout is already {pct}%.")
    if flag.environment == "prod" and service.pending_by_flag(db, [flag.id]):
        return _redirect(f"/flags/{flag.id}", "This flag already has a change awaiting approval.")
    summary = f"Set {flag.key} rollout in {flag.environment} from {flag.rollout_percent}% to {pct}%"
    payload: dict = {"rollout_percent": pct, "enabled": pct > 0}  # 0% is off; any rollout is live
    approval = service.request_change(db, user, flag, payload, summary)
    _commit(db)
    if approval is not None and approval.status == "pending":
        return _redirect(f"/flags/{flag.id}", f"Sent for approval: {summary}.")
    return _redirect(f"/flags/{flag.id}", f"Rollout set to {pct}%.")
