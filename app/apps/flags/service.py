"""Flag business logic shared by routes and the approval applier."""
import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform import approvals, events
from app.platform.audit import audit
from app.platform.models import Approval, User, utcnow
from app.platform.notify import get_notifier

from .models import ENVIRONMENTS, Flag

CHANGE_KIND = "flag.change"
EVENT_TYPE = "flag.changed.v1"
KEY_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


class FlagError(ValueError):
    pass


def validate_key(key: str) -> str:
    key = (key or "").strip()
    if not KEY_RE.match(key) or len(key) > 100:
        raise FlagError("Key must be a lowercase slug, e.g. instant_transfers")
    return key


def validate_environment(env: str) -> str:
    if env not in ENVIRONMENTS:
        raise FlagError(f"Environment must be one of {', '.join(ENVIRONMENTS)}")
    return env


def validate_rollout(value) -> int:
    try:
        pct = int(value)
    except (TypeError, ValueError):
        raise FlagError("Rollout must be a whole number between 0 and 100") from None
    if not 0 <= pct <= 100:
        raise FlagError("Rollout must be between 0 and 100")
    return pct


def _announce(db: Session, flag: Flag, actor: User | None, verb: str) -> None:
    actor_email = actor.email if actor else "system"
    events.publish(db, EVENT_TYPE, {
        "key": flag.key, "environment": flag.environment, "enabled": flag.enabled,
        "rollout_percent": flag.rollout_percent, "actor": actor_email,
    })
    state = "ON" if flag.enabled else "OFF"
    get_notifier(db).send(
        f"#flags-{flag.environment}",
        f"{actor_email} {verb} {flag.key} in {flag.environment}: {state}, rollout {flag.rollout_percent}%",
    )


def apply_change(db: Session, flag: Flag, payload: dict, actor: User, reason: str | None = None) -> Flag:
    """Apply an allowed change (enabled and/or rollout_percent) and record it everywhere."""
    before = flag.state()
    if "enabled" in payload:
        flag.enabled = bool(payload["enabled"])
    if "rollout_percent" in payload:
        flag.rollout_percent = validate_rollout(payload["rollout_percent"])
    flag.updated_at = utcnow()
    flag.updated_by_email = actor.email
    audit(db, actor, "flag.updated", "flag", flag.id, before=before, after=flag.state(), reason=reason)
    _announce(db, flag, actor, "updated")
    return flag


def _applier(db: Session, approval: Approval, actor: User) -> None:
    flag = db.get(Flag, int(approval.entity_id))
    if flag is None:
        raise approvals.ApprovalError("flag no longer exists")
    reason = f"approval #{approval.id}" if approval.required_permission else None
    apply_change(db, flag, approval.payload, actor, reason=reason)


def register_appliers() -> None:
    approvals.register_applier(CHANGE_KIND, _applier)


def request_change(db: Session, actor: User, flag: Flag, payload: dict, summary: str) -> Approval | None:
    """Prod changes need a checker holding flags.approve; everything else applies now."""
    required = "flags.approve" if flag.environment == "prod" else None
    return approvals.request(db, actor, CHANGE_KIND, "flag", flag.id, payload, summary, required_permission=required)


def create_flag(db: Session, actor: User, key: str, description: str, owner_team: str, environment: str,
                enabled: bool, rollout_percent) -> Flag:
    key = validate_key(key)
    environment = validate_environment(environment)
    pct = validate_rollout(rollout_percent)
    if db.scalar(select(Flag.id).where(Flag.key == key, Flag.environment == environment)):
        raise FlagError(f"{key} already exists in {environment}")
    if environment == "prod":
        # New prod flags start dark; turning them on goes through approval.
        enabled, pct = False, 0
    flag = Flag(key=key, description=description.strip(), owner_team=owner_team.strip(), environment=environment,
                enabled=enabled, rollout_percent=pct, updated_by_email=actor.email)
    db.add(flag)
    db.flush()
    audit(db, actor, "flag.created", "flag", flag.id, after={"key": key, "environment": environment, **flag.state()})
    _announce(db, flag, actor, "created")
    return flag


def create_key(db: Session, actor: User, key: str, description: str, owner_team: str, enabled: bool,
               rollout_percent) -> Flag:
    """A flag starts life in the earliest environment and is promoted forward; later envs start dark."""
    first, *later = ENVIRONMENTS
    flag = create_flag(db, actor, key, description, owner_team, first, enabled, rollout_percent)
    for env in later:
        create_flag(db, actor, key, description, owner_team, env, False, 0)
    return flag


def next_environment(env: str) -> str | None:
    i = ENVIRONMENTS.index(env)
    return ENVIRONMENTS[i + 1] if i + 1 < len(ENVIRONMENTS) else None


def by_key(db: Session) -> list[dict]:
    """Rows for the pipeline view: one entry per key with its flag in each environment (None if absent)."""
    out: dict[str, dict] = {}
    for f in db.scalars(select(Flag).order_by(Flag.key, Flag.environment)):
        row = out.setdefault(f.key, {"key": f.key, "description": f.description, "owner_team": f.owner_team,
                                     "envs": dict.fromkeys(ENVIRONMENTS)})
        row["envs"][f.environment] = f
    return list(out.values())


def promote(db: Session, actor: User, key: str, to_env: str) -> tuple[Flag, Approval | None]:
    """Copy the state of the previous environment into to_env. Prod promotion goes through approval."""
    to_env = validate_environment(to_env)
    if to_env == ENVIRONMENTS[0]:
        raise FlagError(f"{to_env} is the first environment; nothing to promote from")
    from_env = ENVIRONMENTS[ENVIRONMENTS.index(to_env) - 1]
    source = db.scalar(select(Flag).where(Flag.key == key, Flag.environment == from_env))
    if source is None:
        raise FlagError(f"{key} does not exist in {from_env}")
    target = db.scalar(select(Flag).where(Flag.key == key, Flag.environment == to_env))
    if target is None:
        target = create_flag(db, actor, key, source.description, source.owner_team, to_env, False, 0)
    if target.state() == source.state():
        raise FlagError(f"{key} in {to_env} already matches {from_env}")
    if to_env == "prod" and pending_by_flag(db, [target.id]):
        raise FlagError(f"{key} in prod already has a change awaiting approval")
    state = f"{'ON' if source.enabled else 'OFF'} at {source.rollout_percent}%"
    summary = f"Promote {key} from {from_env} to {to_env} ({state})"
    approval = request_change(db, actor, target, source.state(), summary)
    return target, approval


def pending_by_flag(db: Session, flag_ids: list[int] | None = None) -> dict[int, list[Approval]]:
    q = select(Approval).where(Approval.kind == CHANGE_KIND, Approval.status == "pending").order_by(Approval.id)
    if flag_ids is not None:
        q = q.where(Approval.entity_id.in_([str(i) for i in flag_ids]))
    out: dict[int, list[Approval]] = {}
    for a in db.scalars(q):
        out.setdefault(int(a.entity_id), []).append(a)
    return out


def snapshot(db: Session, environment: str) -> tuple[dict, str]:
    flags = db.scalars(select(Flag).where(Flag.environment == environment).order_by(Flag.key)).all()
    body = {f.key: {"enabled": f.enabled, "rollout_percent": f.rollout_percent} for f in flags}
    digest = hashlib.sha256(json.dumps({"environment": environment, "flags": body}, sort_keys=True).encode()).hexdigest()
    return {"environment": environment, "flags": body, "version": digest[:12]}, f'"{digest}"'
