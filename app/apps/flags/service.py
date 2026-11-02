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

from .models import ENVIRONMENTS, Flag, FlagPlan

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
    payload = dict(approval.payload)
    spec = payload.pop("create", None)
    reason = f"approval #{approval.id}" if approval.required_permission else None
    if spec:
        if db.scalar(select(Flag.id).where(Flag.key == spec["key"], Flag.environment == spec["environment"])):
            raise approvals.ApprovalError(f"{spec['key']} already exists in {spec['environment']}")
        flag = create_flag(db, actor, spec["key"], spec["description"], spec["owner_team"], spec["environment"], False, 0)
    else:
        flag = db.get(Flag, int(approval.entity_id))
        if flag is None:
            raise approvals.ApprovalError("flag no longer exists")
        expected = payload.pop("expected", None)
        if expected is not None and expected != flag.state():
            raise approvals.ApprovalError("the flag changed since this was requested; ask for it again")
    apply_change(db, flag, payload, actor, reason=reason)


def register_appliers() -> None:
    approvals.register_applier(CHANGE_KIND, _applier)


def request_change(db: Session, actor: User, flag: Flag, payload: dict, summary: str) -> Approval | None:
    """Prod changes need a checker holding flags.approve; everything else applies now. The request remembers the
    state it was made against, so it cannot be applied on top of a later change (e.g. a rollback)."""
    required = "flags.approve" if flag.environment == "prod" else None
    if required:
        payload = {**payload, "expected": flag.state()}
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
    """A flag starts life in the earliest environment only; later environments appear when it is promoted there,
    so SDK snapshots for staging/prod never see a key before promotion."""
    return create_flag(db, actor, key, description, owner_team, ENVIRONMENTS[0], enabled, rollout_percent)


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
    if target is None and to_env == "prod":
        # Don't create the prod row until a checker approves: the applier creates it (see _applier).
        if db.scalar(select(Approval.id).where(Approval.kind == CHANGE_KIND, Approval.status == "pending",
                                               Approval.entity_id == f"new:{key}:{to_env}")):
            raise FlagError(f"a promotion of {key} to {to_env} is already awaiting approval")
        state = f"{'ON' if source.enabled else 'OFF'} at {source.rollout_percent}%"
        summary = f"Promote {key} from {from_env} to {to_env} ({state})"
        payload = {**source.state(), "create": {"key": key, "description": source.description,
                                                 "owner_team": source.owner_team, "environment": to_env}}
        approval = approvals.request(db, actor, CHANGE_KIND, "flag", f"new:{key}:{to_env}", payload, summary,
                                     required_permission="flags.approve")
        return source, approval
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


def pending_new(db: Session) -> dict[tuple[str, str], Approval]:
    """Pending promotions that will create a row (keyed by (key, env)); they have no flag id yet."""
    q = select(Approval).where(Approval.kind == CHANGE_KIND, Approval.status == "pending",
                               Approval.entity_id.like("new:%"))
    out = {}
    for a in db.scalars(q):
        _, key, env = a.entity_id.split(":", 2)
        out[(key, env)] = a
    return out


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


# ---------------------------------------------------------------- rollout plans

DEFAULT_PLAN_STEPS = [
    {"environment": "dev", "rollout_percent": 100},
    {"environment": "staging", "rollout_percent": 100},
    {"environment": "prod", "rollout_percent": 10},
    {"environment": "prod", "rollout_percent": 50},
    {"environment": "prod", "rollout_percent": 100},
]


def validate_steps(steps: list[dict]) -> list[dict]:
    """Steps must walk forward through the environments and only ever increase rollout within one."""
    if not steps:
        raise FlagError("a plan needs at least one step")
    out: list[dict] = []
    for i, raw in enumerate(steps, start=1):
        try:
            env = validate_environment(str(raw["environment"]))
            pct = validate_rollout(raw["rollout_percent"])
        except (KeyError, TypeError):
            raise FlagError(f"step {i} must have an environment and a rollout percent") from None
        if pct == 0:
            raise FlagError(f"step {i}: a step must roll out to more than 0%")
        if out:
            prev = out[-1]
            if ENVIRONMENTS.index(env) < ENVIRONMENTS.index(prev["environment"]):
                raise FlagError(f"step {i}: {env} comes before {prev['environment']}; plans only move forward")
            if env == prev["environment"] and pct <= prev["rollout_percent"]:
                raise FlagError(f"step {i}: {env} must increase past {prev['rollout_percent']}%")
        out.append({"environment": env, "rollout_percent": pct})
    return out


def parse_plan(text: str) -> list[dict]:
    """'dev 100 → staging 100 → prod 10 → prod 50 → prod 100' (also accepts , > : @ as separators)."""
    tokens = [t.strip() for t in re.split(r"(?:→|->|>|,|;|\n)+", text or "") if t.strip()]
    steps = []
    for tok in tokens:
        m = re.fullmatch(r"([a-zA-Z]+)\s*[:@ ]\s*(\d+)\s*%?", tok)
        if not m:
            raise FlagError(f"could not read step '{tok}'; write it as 'prod 50'")
        steps.append({"environment": m.group(1).lower(), "rollout_percent": int(m.group(2))})
    return validate_steps(steps)


def format_plan(steps: list[dict]) -> str:
    return " → ".join(f"{s['environment']} {s['rollout_percent']}" for s in steps)


def get_plan(db: Session, key: str) -> list[dict]:
    row = db.scalar(select(FlagPlan).where(FlagPlan.key == key))
    return list(row.steps) if row and row.steps else [dict(s) for s in DEFAULT_PLAN_STEPS]


def set_plan(db: Session, actor: User, key: str, steps: list[dict]) -> FlagPlan:
    steps = validate_steps(steps)
    if not db.scalar(select(Flag.id).where(Flag.key == key)):
        raise FlagError(f"{key} does not exist")
    row = db.scalar(select(FlagPlan).where(FlagPlan.key == key))
    before = {"plan": format_plan(row.steps if row and row.steps else DEFAULT_PLAN_STEPS)}
    if row is None:
        row = FlagPlan(key=key)
        db.add(row)
    row.steps = steps
    row.updated_at = utcnow()
    row.updated_by_email = actor.email
    db.flush()
    audit(db, actor, "flag.plan_updated", "flag_plan", key, before=before, after={"plan": format_plan(steps)})
    return row


def _step_reached(stages: dict[str, Flag], step: dict) -> bool:
    f = stages.get(step["environment"])
    return bool(f and f.enabled and f.rollout_percent >= step["rollout_percent"])


def plan_state(db: Session, key: str, stages: dict[str, Flag] | None = None) -> dict:
    """Where a key sits in its plan: position = number of leading steps already reached."""
    if stages is None:
        stages = {f.environment: f for f in db.scalars(select(Flag).where(Flag.key == key))}
    steps = get_plan(db, key)
    position = 0
    for step in steps:
        if not _step_reached(stages, step):
            break
        position += 1
    current = steps[position - 1] if position else None
    nxt = steps[position] if position < len(steps) else None
    pending_next = False
    if nxt:
        target = stages.get(nxt["environment"])
        pending_next = bool(target and pending_by_flag(db, [target.id])) or (nxt["environment"], ) in {
            (k_env[1],) for k_env in pending_new(db) if k_env[0] == key and k_env[1] == nxt["environment"]}
    return {"steps": steps, "position": position, "total": len(steps), "current": current, "next": nxt,
            "pending_next": pending_next, "text": format_plan(steps)}


def roll_forward(db: Session, actor: User, key: str) -> tuple[dict, Approval | None]:
    """Push the flag to the next step of its plan. Prod steps go through approval; the rest apply now."""
    stages = {f.environment: f for f in db.scalars(select(Flag).where(Flag.key == key))}
    if not stages:
        raise FlagError(f"{key} does not exist")
    state = plan_state(db, key, stages)
    step = state["next"]
    if step is None:
        raise FlagError(f"{key} is already at the last step of its plan")
    env, pct = step["environment"], step["rollout_percent"]
    payload = {"enabled": True, "rollout_percent": pct}
    summary = f"Roll {key} forward to {env} {pct}% (step {state['position'] + 1} of {state['total']})"
    target = stages.get(env)
    source = next((stages[e] for e in reversed(ENVIRONMENTS) if e in stages), None)
    if env == "prod":
        if target is None:
            if db.scalar(select(Approval.id).where(Approval.kind == CHANGE_KIND, Approval.status == "pending",
                                                   Approval.entity_id == f"new:{key}:{env}")):
                raise FlagError(f"{key} already has a prod step awaiting approval")
            payload["create"] = {"key": key, "description": source.description, "owner_team": source.owner_team,
                                 "environment": env}
            approval = approvals.request(db, actor, CHANGE_KIND, "flag", f"new:{key}:{env}", payload, summary,
                                         required_permission="flags.approve")
            return step, approval
        if pending_by_flag(db, [target.id]):
            raise FlagError(f"{key} in prod already has a change awaiting approval")
        return step, request_change(db, actor, target, payload, summary)
    if target is None:
        target = create_flag(db, actor, key, source.description, source.owner_team, env, False, 0)
    apply_change(db, target, payload, actor, reason=summary)
    return step, None


def pending_for_key(db: Session, key: str, stages: dict[str, Flag]) -> list[Approval]:
    ids = [str(f.id) for f in stages.values()]
    q = select(Approval).where(Approval.kind == CHANGE_KIND, Approval.status == "pending",
                               (Approval.entity_id.in_(ids)) | (Approval.entity_id.like(f"new:{key}:%")))
    return list(db.scalars(q.order_by(Approval.id)))


def roll_back(db: Session, actor: User, key: str) -> dict:
    """Reduce exposure one plan step: take the highest stage that is live and drop it to the plan's next-lower
    percentage for that stage, or off. Works from the real state, not the plan prefix, so a prod flag whose dev stage
    was switched off can still be pulled back. Applies immediately in every stage, prod included (the break-glass
    path); audited and announced. Pending approvals for the key are withdrawn so a stale increase cannot land later."""
    stages = {f.environment: f for f in db.scalars(select(Flag).where(Flag.key == key))}
    if not stages:
        raise FlagError(f"{key} does not exist")
    steps = get_plan(db, key)
    live_env = next((e for e in reversed(ENVIRONMENTS)
                     if e in stages and stages[e].enabled and stages[e].rollout_percent > 0), None)
    if live_env is None:
        raise FlagError(f"{key} is not live in any stage; nothing to roll back")
    flag = stages[live_env]
    lower = [s["rollout_percent"] for s in steps
             if s["environment"] == live_env and s["rollout_percent"] < flag.rollout_percent]
    if lower:
        payload = {"enabled": True, "rollout_percent": max(lower)}
        target_text = f"{live_env} {max(lower)}%"
    else:
        payload = {"enabled": False, "rollout_percent": 0}
        target_text = f"{live_env} off"
    cancelled = pending_for_key(db, key, stages)
    for a in cancelled:
        approvals.cancel(db, actor, a, reason=f"withdrawn: {key} was rolled back to {target_text}")
    apply_change(db, flag, payload, actor,
                 reason=f"Roll {key} back to {target_text} (from {live_env} {flag.state()['rollout_percent']}%)")
    return {"target": target_text, "flag": flag, "cancelled": len(cancelled)}
