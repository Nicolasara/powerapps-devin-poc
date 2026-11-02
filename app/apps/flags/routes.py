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
               notice: str | None = None):
    env = environment if environment in ENVIRONMENTS else None
    q = select(Flag).order_by(Flag.key, Flag.environment)
    if env:
        q = q.where(Flag.environment == env)
    flags = db.scalars(q).all()
    pending = service.pending_by_flag(db, [f.id for f in flags])
    rows = service.by_key(db)
    if env:
        rows = [r for r in rows if r["envs"].get(env)]
    return render(request, "flags/list.html", user, flags=flags, rows=rows, environment=env,
                  environments=ENVIRONMENTS, pending=pending, notice=notice)


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
    return render(request, "flags/detail.html", user, flag=flag, stages=stages, environments=ENVIRONMENTS,
                  history=history, env_of=env_of, pending=pending, notice=notice)


@router.get("/{flag_id}/toggle/confirm")
def confirm_toggle(request: Request, flag_id: int, user: Writer, db: DB):
    flag = _get_flag(db, flag_id)
    return render(request, "flags/confirm_toggle.html", user, flag=flag)


@router.post("/{flag_id}/toggle")
def toggle_flag(flag_id: int, user: Writer, db: DB):
    flag = _get_flag(db, flag_id)
    if flag.environment == "prod" and service.pending_by_flag(db, [flag.id]):
        return _redirect("/flags", f"{flag.key} in prod already has a change awaiting approval.")
    target = not flag.enabled
    summary = f"{'Enable' if target else 'Disable'} {flag.key} in {flag.environment}"
    approval = service.request_change(db, user, flag, {"enabled": target}, summary)
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
    if pct == flag.rollout_percent:
        return _redirect(f"/flags/{flag.id}", f"Rollout is already {pct}%.")
    if flag.environment == "prod" and service.pending_by_flag(db, [flag.id]):
        return _redirect(f"/flags/{flag.id}", "This flag already has a change awaiting approval.")
    summary = f"Set {flag.key} rollout in {flag.environment} from {flag.rollout_percent}% to {pct}%"
    approval = service.request_change(db, user, flag, {"rollout_percent": pct}, summary)
    _commit(db)
    if approval is not None and approval.status == "pending":
        return _redirect(f"/flags/{flag.id}", f"Sent for approval: {summary}.")
    return _redirect(f"/flags/{flag.id}", f"Rollout set to {pct}%.")
