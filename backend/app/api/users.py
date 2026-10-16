"""Users API — seeded demo users for the storefront 'Log In As' dropdown."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_pg_session
from ..models import schemas
from ..repositories import users_repo

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[schemas.UserOut])
async def list_users(session: AsyncSession = Depends(get_pg_session)):
    users = await users_repo.list_users(session)
    return [schemas.UserOut(id=u.id, name=u.name, email=u.email) for u in users]
