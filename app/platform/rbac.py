"""Roles map to permissions. Enforced server-side in every mutating route."""

ROLES: dict[str, set[str]] = {
    "viewer": {"flags.read", "refunds.read", "audit.read"},
    "operator": {"flags.read", "flags.write", "refunds.read", "refunds.request", "audit.read"},
    "approver": {"flags.read", "flags.write", "flags.approve", "refunds.read", "refunds.request", "refunds.approve", "audit.read"},
    "admin": {"flags.read", "flags.write", "flags.approve", "refunds.read", "refunds.request", "refunds.approve", "refunds.approve_large", "audit.read", "admin"},
}


def permissions_for(role: str) -> set[str]:
    return ROLES.get(role, set())


def has_permission(role: str, permission: str) -> bool:
    return permission in permissions_for(role)
