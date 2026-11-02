"""Dev login standing in for Entra ID / OIDC.

In production: swap `login` for an OIDC callback that maps Entra group claims to a role.
Everything downstream (current_user, require) stays the same.
"""
import os

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.orm import Session

from . import rbac
from .db import get_db
from .models import User

_signer = URLSafeSerializer(os.environ.get("SESSION_SECRET", "dev-only-secret"))
COOKIE = "poc_session"


def set_session(response, user: User) -> None:
    response.set_cookie(COOKIE, _signer.dumps({"uid": user.id}), httponly=True, samesite="lax")


def clear_session(response) -> None:
    response.delete_cookie(COOKIE)


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        data = _signer.loads(raw)
    except BadSignature:
        return None
    return db.get(User, data.get("uid"))


def current_user(user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="login required")
    return user


def require(permission: str):
    def dep(user: User = Depends(current_user)) -> User:
        if not rbac.has_permission(user.role, permission):
            raise HTTPException(status_code=403, detail=f"requires {permission}")
        return user
    return dep
