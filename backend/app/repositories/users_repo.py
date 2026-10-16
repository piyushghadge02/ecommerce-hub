"""PostgreSQL repository: users."""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.tables import User


async def list_users(session: AsyncSession) -> List[User]:
    result = await session.execute(select(User).order_by(User.id))
    return list(result.scalars().all())


async def get_user(session: AsyncSession, user_id: int) -> Optional[User]:
    return await session.get(User, user_id)
