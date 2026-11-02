from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .platform import rbac
from .platform.models import User

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def render(request: Request, name: str, user: User | None, **ctx):
    perms = rbac.permissions_for(user.role) if user else set()
    return templates.TemplateResponse(request, name, {"user": user, "perms": perms, **ctx})
