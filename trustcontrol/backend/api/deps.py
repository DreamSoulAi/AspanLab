"""Shared FastAPI dependencies for API-key authenticated endpoints."""

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.database import get_db
from backend.models.location import Location


async def get_location_by_api_key(
    api_key: str = Header(..., alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> Location:
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
