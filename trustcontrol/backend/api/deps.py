"""Shared FastAPI dependencies for API-key authenticated endpoints."""

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.models.location import Location


async def get_location_by_api_key(api_key: str, db: AsyncSession) -> Location:
    """Return an active Location for the given API key, or raise 401."""
    result = await db.execute(
        select(Location).where(
            Location.api_key   == api_key,
            Location.is_active == True,
        )
    )
    loc = result.scalar()
    if not loc:
        raise HTTPException(status_code=401, detail="Неверный API ключ точки")
    return loc
