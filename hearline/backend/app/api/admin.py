"""Admin endpoints: the KPI dashboard. Admins are the Clerk user ids in ADMIN_USER_IDS
(the login's own id, not the email, which users can edit). In dev mode everyone is admin."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.routes import SessionDep, UserDep
from app.config import get_settings
from app.database.models import User
from app.plans import set_plan
from app.tracking.dashboard import Filters, dashboard

router = APIRouter(prefix="/api/admin")


def is_admin(user: User) -> bool:
    s = get_settings()
    return s.auth_mode == "dev" or (user.external_id or "") in s.admin_user_ids


def require_admin(user: UserDep) -> User:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admins only.")
    return user


@router.get("/access")
def access(user: UserDep) -> dict[str, bool]:
    """Whether the current user may open the dashboard (the menu shows the link if so)."""
    return {"admin": is_admin(user)}


@router.get("/dashboard", dependencies=[Depends(require_admin)])
def get_dashboard(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    language: str | None = None,
    voice: str | None = None,
    topic: str | None = None,
) -> dict[str, Any]:
    """KPIs for the last `days`, optionally for one podcast language, voice or topic."""
    return dashboard(session, Filters(days=days, language=language or None,
                                      voice=voice or None, topic=topic or None))


class PlanChange(BaseModel):
    plan: str


@router.put("/users/{user_id}/plan", dependencies=[Depends(require_admin)])
def change_user_plan(user_id: int, data: PlanChange, session: SessionDep) -> dict[str, Any]:
    """Move a user to another plan (until billing exists, this is how paid plans are given).
    A downgrade trims their Settings to fit."""
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    try:
        set_plan(session, user, data.plan, "admin")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"user_id": user.id, "plan": user.plan}
